"""Read-only project observations and explicit, user-initiated project actions.

No process is launched by refresh except bounded inspection commands. Script
processes outlive this observer; close deliberately never signals them.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata


class ProjectError(RuntimeError):
    pass


def _text(value):
    """Project metadata is untrusted terminal text, including filenames."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else "�"
                   for c in str(value))


def _identity(*parts):
    return hashlib.sha256("\0".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[:24]


def _within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except (ValueError, TypeError):
        return False


def _numstat(data):
    """Parse -z numstat, including its extra old/new pathname rename fields."""
    fields = data.split(b"\0")
    entries = []
    index = 0
    while index < len(fields) and fields[index]:
        added, removed, path = fields[index].split(b"\t", 2)
        index += 1
        old = None
        if not path:
            old, path = fields[index:index + 2]
            index += 2
        binary = added == b"-" or removed == b"-"
        entry = {"path": os.fsdecode(path), "added": 0 if binary else int(added),
                 "removed": 0 if binary else int(removed), "binary": binary,
                 "untracked": False}
        if old is not None:
            entry["oldPath"] = os.fsdecode(old)
        entries.append(entry)
    return entries


def _status(data):
    """Porcelain v1 -z has destination first for renames/copies."""
    fields, result, index = data.split(b"\0"), {}, 0
    while index < len(fields) and fields[index]:
        item = fields[index]
        index += 1
        status_code, path = item[:2].decode("ascii"), os.fsdecode(item[3:])
        # A staged deletion and an untracked replacement can share a pathname.
        # Preserve both facts instead of dropping the staged record.
        previous = result.get(path)
        if previous and status_code == "??":
            result[path] = previous[0] + "?"
        elif previous == "??":
            result[path] = status_code[0] + "?"
        else:
            result[path] = status_code
        if "R" in status_code or "C" in status_code:
            index += 1
    return result


class ProjectState:
    COMMAND_TIMEOUT = 4
    SUPERVISOR_INTERVAL = 15
    DIFF_LIMIT = 512 * 1024

    def __init__(self, cwd, omp_pid, state_dir, *, omp_executable="omp", command_path=None, agent_dir=None):
        self.cwd = os.path.realpath(cwd)
        self.omp_pid = int(omp_pid)
        self.state_dir = Path(state_dir)
        self.omp_executable = omp_executable
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.root = self.cwd
        config_home = os.environ.get("XDG_CONFIG_HOME", "")
        if not os.path.isabs(config_home):
            config_home = str(Path.home() / ".config")
        self.config = Path(config_home) / "rex-subagent-viewer" / "projects" / (_identity(self.cwd) + ".json")
        self._lock = threading.RLock()
        self._scripts = {}
        self._children = []
        self._owned_pids = {}
        self._file_counts = {}
        self._supervisor_at = None
        self._supervisor = []
        self._supervisor_error = None
        self._closed = False
        self._env = dict(os.environ, OMP_CONFIG_SYNC="0", GIT_OPTIONAL_LOCKS="0",
                         GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", NO_COLOR="1")
        if command_path is not None:
            self._env["PATH"] = command_path
        if agent_dir is not None:
            self._env["PI_CODING_AGENT_DIR"] = agent_dir

    def _run(self, args, cwd=None, timeout=None, allowed=(0,), input=None):
        try:
            result = subprocess.run(args, cwd=cwd or self.cwd, env=self._env,
                                    input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=self.COMMAND_TIMEOUT if timeout is None else timeout)
        except subprocess.TimeoutExpired as error:
            raise ProjectError("%s 采集超时（%ss）" % (args[0], error.timeout)) from error
        except OSError as error:
            raise ProjectError("%s: %s" % (args[0], error)) from error
        if result.returncode not in allowed:
            detail = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
            raise ProjectError(_text(detail) or "%s 退出码 %s" % (args[0], result.returncode))
        return result

    def _git(self, *args, **kwargs):
        return self._run(["git", "--no-pager", *args], **kwargs)

    def _repository(self):
        result = self._git("rev-parse", "--show-toplevel", allowed=(0, 128))
        if result.returncode:
            # Only the ordinary non-repository condition is absence, not permission,
            # dubious-ownership or repository corruption errors.
            message = result.stderr.decode("utf-8", "replace")
            if "not a git repository" in message.lower():
                self.root = self.cwd
                return False
            raise ProjectError(_text(message.strip()))
        self.root = os.path.realpath(os.fsdecode(result.stdout.rstrip(b"\n")))
        return True

    def _baseline(self):
        result = self._git("rev-parse", "--verify", "HEAD", allowed=(0, 128))
        if result.returncode == 0:
            return result.stdout.decode("ascii").strip(), True
        # A missing HEAD is valid only on an unborn symbolic branch.
        branch = self._git("symbolic-ref", "--quiet", "HEAD", allowed=(0, 1, 128))
        if branch.returncode != 0:
            raise ProjectError("无法解析 HEAD: " + _text(result.stderr.decode("utf-8", "replace").strip()))
        reference = branch.stdout.decode("utf-8", "replace").strip()
        exists = self._git("show-ref", "--verify", "--quiet", reference, allowed=(0, 1))
        if exists.returncode == 0:
            raise ProjectError("HEAD 引用无法读取")
        # hash-object without -w computes the empty-tree ID without modifying Git.
        empty = self._git("hash-object", "-t", "tree", "--stdin", input=b"")
        return empty.stdout.decode("ascii").strip(), False

    def _untracked_count(self, relative, deadline):
        path = Path(self.root) / relative
        info = path.lstat()
        stamp = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_mode)
        cached = self._file_counts.get(relative)
        if cached and cached[0] == stamp:
            return cached[1]
        if stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(path))
            counts = (target.count(b"\n") + int(bool(target) and not target.endswith(b"\n")), False)
        elif stat.S_ISREG(info.st_mode):
            count, last, binary = 0, b"", False
            with path.open("rb") as stream:
                first = True
                while True:
                    if time.monotonic() > deadline:
                        raise ProjectError("未跟踪文件统计超时；行数仅含已完成统计的文件")
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if first and b"\0" in chunk[:8000]:
                        binary = True
                        break
                    first = False
                    count += chunk.count(b"\n")
                    last = chunk[-1:]
            counts = (0 if binary else count + int(bool(last) and last != b"\n"), binary)
        else:
            raise ProjectError("无法统计非普通未跟踪文件: " + _text(relative))
        self._file_counts[relative] = (stamp, counts)
        return counts

    def _changes(self):
        baseline, exists = self._baseline()
        # Run from the root so every pathname has a single, unambiguous base.
        status = _status(self._git("status", "--porcelain=v1", "-z", "--untracked-files=all",
                                   cwd=self.root).stdout)
        raw = self._git("diff", "--numstat", "-z", "--find-renames", "--no-ext-diff",
                        "--no-textconv", baseline, "--", cwd=self.root).stdout
        entries = _numstat(raw)
        errors = []
        deadline = time.monotonic() + self.COMMAND_TIMEOUT
        for path, code in status.items():
            if "?" not in code:
                continue
            entry = {"path": path, "added": 0, "removed": 0, "untracked": True,
                     "binary": False, "status": "??"}
            try:
                entry["added"], entry["binary"] = self._untracked_count(path, deadline)
            except (OSError, ProjectError) as error:
                entry["incomplete"] = True
                errors.append(_text(error))
            entries.append(entry)
        for entry in entries:
            entry["status"] = "??" if entry["untracked"] else status.get(entry["path"], "  ")
            entry["staged"] = not entry["untracked"] and entry["status"][0] not in (" ", "?")
        live_untracked = {p for p, code in status.items() if "?" in code}
        self._file_counts = {p: value for p, value in self._file_counts.items() if p in live_untracked}
        changes = {"added": sum(e["added"] for e in entries),
                   "removed": sum(e["removed"] for e in entries), "files": len({e["path"] for e in entries}),
                   "staged": sum(code[0] not in (" ", "?") for code in status.values()),
                   "reference": "HEAD", "headExists": exists,
                   "entries": [{k: _text(v) if isinstance(v, str) else v for k, v in e.items()} for e in entries]}
        if errors:
            changes["incomplete"] = True
        return baseline if exists else "", changes, errors

    def _package_directory(self):
        directory = Path(self.cwd)
        while True:
            if (directory / "package.json").is_file():
                return directory
            if str(directory) == self.root or directory.parent == directory or not _within(directory, self.root):
                return None
            directory = directory.parent

    @staticmethod
    def _read_json(path):
        # Configuration is small; refuse accidentally reading a generated giant file.
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ProjectError("配置超过 1MiB: " + str(path))
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise ProjectError("配置 JSON 无效: " + str(path)) from error

    def _custom_scripts(self):
        if not self.config.exists():
            return []
        data = self._read_json(self.config)
        if not isinstance(data, dict) or data.get("v") != 1 or data.get("cwd") != self.cwd or not isinstance(data.get("scripts"), list):
            raise ProjectError("项目命令配置格式无效: " + str(self.config))
        scripts = []
        for item in data["scripts"]:
            if not isinstance(item, dict):
                raise ProjectError("项目命令配置条目无效")
            name, command = self._validate_script(item.get("name"), item.get("command"))
            scripts.append({"name": name, "command": command})
        return scripts

    @staticmethod
    def _validate_script(name, command):
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(unicodedata.category(c).startswith("C") for c in name):
            raise ProjectError("命令名称不能为空或含控制字符，最多 120 字符")
        if (not isinstance(command, str) or not command.strip() or len(command) > 16384
                or "\0" in command or any(unicodedata.category(c) == "Cs" for c in command)):
            raise ProjectError("命令不能为空、含 NUL/无效 Unicode 或超过 16384 字符")
        return name.strip(), command

    def _load_scripts(self):
        scripts, errors = {}, []
        package = self._package_directory()
        if package:
            try:
                data = self._read_json(package / "package.json")
                if not isinstance(data, dict) or not isinstance(data.get("scripts", {}), dict):
                    raise ProjectError("package.json scripts 必须为对象")
                # Prefer the nearest package/workspace lockfile, not installed tools.
                manager, scan = "npm", package
                while True:
                    locks = (("bun.lock", "bun"), ("bun.lockb", "bun"), ("pnpm-lock.yaml", "pnpm"),
                             ("yarn.lock", "yarn"), ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"))
                    found = [runner for filename, runner in locks if (scan / filename).is_file()]
                    if found:
                        if len(set(found)) > 1:
                            raise ProjectError("同目录存在不同执行器的 lockfile，请先消除歧义: " + str(scan))
                        manager = found[0]
                        break
                    if str(scan) == self.root or scan.parent == scan or not _within(scan.parent, self.root):
                        break
                    scan = scan.parent
                for name, command in data.get("scripts", {}).items():
                    _, command = self._validate_script(name, command)
                    argv = [manager, "run", *(["--"] if name.startswith("-") else []), name]
                    key = "package-" + _identity(str(package), manager, name, command)
                    scripts[key] = {"id": key, "label": name, "command": command, "source": "package.json",
                                    "_cwd": str(package), "_argv": argv}
            except (OSError, ProjectError) as error:
                errors.append(_text(error))
        try:
            for item in self._custom_scripts():
                name, command = item["name"], item["command"]
                key = "user-" + _identity(self.cwd, name, command)
                scripts[key] = {"id": key, "label": name, "command": command, "source": "user",
                                "_cwd": self.cwd, "_argv": ["/bin/sh", "-c", command]}
        except (OSError, ProjectError) as error:
            errors.append(_text(error))
        return scripts, errors

    def add_script(self, name, command):
        name, command = self._validate_script(name, command)
        with self._lock:
            if self._closed:
                raise ProjectError("项目观察已关闭")
            self.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.config.parent, 0o700)
            os.chmod(self.config.parent.parent, 0o700)
            lock_path = self.config.with_suffix(".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a+b") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                scripts = self._custom_scripts()
                if any(item["name"] == name for item in scripts):
                    raise ProjectError("已有同名项目命令: " + name)
                scripts.append({"name": name, "command": command})
                fd, temporary = tempfile.mkstemp(prefix=".project-", dir=self.config.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        json.dump({"v": 1, "cwd": self.cwd, "scripts": scripts}, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.config)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        return "已添加 " + _text(name)

    def run_script(self, id):
        with self._lock:
            if self._closed:
                raise ProjectError("项目观察已关闭")
            if not isinstance(id, str) or id not in self._scripts:
                raise ProjectError("命令不在当前项目快照中")
            current, errors = self._load_scripts()
            if id not in current:
                raise ProjectError("命令已变化，请刷新后重试" + (": " + "; ".join(errors) if errors else ""))
            script = current[id]
            fd, path = tempfile.mkstemp(prefix="script-", suffix=".log", dir=self.state_dir)
            try:
                with os.fdopen(fd, "ab", buffering=0) as log:
                    os.fchmod(log.fileno(), 0o600)
                    child = subprocess.Popen(script["_argv"], cwd=script["_cwd"], env=self._env,
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True, close_fds=True)
            except OSError as error:
                raise ProjectError("启动失败: " + _text(error) + "；日志: " + path) from error
            self._children.append(child)
        return "已启动 %s · PID %d · %s" % (_text(script["label"]), child.pid, path)

    def commit(self, message):
        if not isinstance(message, str) or not message.strip() or "\0" in message or len(message) > 65536:
            raise ProjectError("提交信息不能为空、含 NUL 或超过 65536 字符")
        with self._lock:
            if self._closed:
                raise ProjectError("项目观察已关闭")
            if not self._repository():
                raise ProjectError("当前目录不是 Git 仓库")
            baseline, _ = self._baseline()
            staged = self._git("diff", "--cached", "--quiet", "--no-ext-diff", baseline, "--", allowed=(0, 1))
            if staged.returncode == 0:
                raise ProjectError("没有已暂存的改动")
            # No add, -a, pathspec, hook bypass, or generated message. Let Git
            # retain its native staged-content and hook/error behavior.
            result = self._git("commit", "--file=-", input=message.encode("utf-8"), timeout=120)
            return _text(result.stdout.decode("utf-8", "replace").strip()) or "提交完成"

    def diff(self):
        if not self._repository():
            raise ProjectError("当前目录不是 Git 仓库")
        baseline, exists = self._baseline()
        # Spool instead of buffering potentially huge patches. The display has a
        # documented cap; numeric refresh never copies patch contents.
        with tempfile.TemporaryFile() as output:
            args = ["git", "--no-pager", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
                    "--find-renames", baseline, "--"]
            try:
                result = subprocess.run(args, cwd=self.root, env=self._env, stdout=output,
                                        stderr=subprocess.PIPE, timeout=10)
            except subprocess.TimeoutExpired as error:
                raise ProjectError("Git diff 超时（10s）") from error
            except OSError as error:
                raise ProjectError(_text(error)) from error
            if result.returncode:
                raise ProjectError(_text(result.stderr.decode("utf-8", "replace")))
            output.seek(0)
            raw = output.read(self.DIFF_LIMIT + 1)
        text = ("相对 HEAD（暂存 + 未暂存）\n" if exists else "HEAD 尚不存在；相对空树（暂存 + 未暂存）\n")
        text += raw[:self.DIFF_LIMIT].decode("utf-8", "replace")
        if len(raw) > self.DIFF_LIMIT:
            text += "\n[Diff 超过 512KiB，显示已截断]\n"
        status = _status(self._git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=self.root).stdout)
        untracked = [path for path, code in status.items() if "?" in code]
        if untracked:
            text += "\n未跟踪文件（计入 Changes 数量；不展开文件正文）:\n"
            budget = self.DIFF_LIMIT
            for path in untracked:
                line = "  " + _text(path) + "\n"
                budget -= len(line.encode("utf-8", "replace"))
                if budget < 0:
                    text += "[未跟踪文件列表已截断]\n"
                    break
                text += line
        return _text(text)

    def _supervisor_rows(self):
        now = time.monotonic()
        if self._supervisor_at is not None and now - self._supervisor_at < self.SUPERVISOR_INTERVAL:
            return self._supervisor, self._supervisor_error
        self._supervisor_at = now
        try:
            raw = self._run([self.omp_executable, "ps", "--plain", "--json", "--dir", self.cwd]).stdout
            reports = json.loads(raw)
            if not isinstance(reports, list):
                raise ProjectError("omp ps JSON 顶层不是列表")
            rows = []
            for report in reports:
                if not isinstance(report, dict):
                    raise ProjectError("omp ps JSON scope 格式无效")
                if report.get("kind") != "project" or os.path.realpath(report.get("projectDir", "")) != self.cwd:
                    continue
                if not isinstance(report.get("daemons"), list):
                    raise ProjectError("omp ps JSON daemons 格式无效")
                for row in report["daemons"]:
                    if not isinstance(row, dict):
                        raise ProjectError("omp ps JSON daemon 格式无效")
                    pid = row.get("pid")
                    if type(pid) is int and pid > 0 and row.get("state") not in ("exited", "failed"):
                        # The scope is authoritative; cwd further prevents a
                        # service launched for another project from leaking in.
                        if isinstance(row.get("cwd"), str) and _within(row["cwd"], self.root):
                            rows.append(row)
            self._supervisor, self._supervisor_error = rows, None
        except (ProjectError, ValueError, TypeError) as error:
            # Never call a failed refresh an empty successful observation, and
            # never retain a stale row as if it were a current listener.
            self._supervisor, self._supervisor_error = [], "服务采集: " + _text(error)
        return self._supervisor, self._supervisor_error

    @staticmethod
    def _process_table(raw):
        processes = {}
        for line in raw.decode("utf-8", "replace").splitlines():
            fields = line.split(None, 7)
            if len(fields) != 8:
                continue
            try:
                pid, parent = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            processes[pid] = {"parent": parent, "start": " ".join(fields[2:7]), "command": fields[7]}
        return processes

    @staticmethod
    def _descendants(processes, roots):
        children = {}
        for pid, info in processes.items():
            children.setdefault(info["parent"], []).append(pid)
        result, pending = set(roots), list(roots)
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in result:
                    result.add(child)
                    pending.append(child)
        return result

    def _lsof(self, pids, *arguments):
        result = self._run(["lsof", "-nP", "-a", "-p", ",".join(map(str, sorted(pids))), *arguments], allowed=(0, 1))
        if result.returncode == 1 and result.stderr.strip():
            raise ProjectError(_text(result.stderr.decode("utf-8", "replace").strip()))
        return result.stdout.decode("utf-8", "replace")

    def _servers(self):
        rows, supervisor_error = self._supervisor_rows()
        errors = [supervisor_error] if supervisor_error else []
        processes = self._process_table(self._run(["ps", "-axo", "pid=,ppid=,lstart=,comm="]).stdout)
        roots = {self.omp_pid}
        labels = {}
        for row in rows:
            if row["pid"] in processes:
                roots.add(row["pid"])
                for pid in self._descendants(processes, {row["pid"]}):
                    labels[pid] = row.get("name") or processes[pid]["command"]
        with self._lock:
            self._children = [child for child in self._children if child.poll() is None]
            roots.update(child.pid for child in self._children)
        roots.update(pid for pid, start in self._owned_pids.items()
                     if pid in processes and processes[pid]["start"] == start)
        candidates = sorted(self._descendants(processes, roots) - {self.omp_pid})
        candidates = [pid for pid in candidates if pid in processes]
        owned, servers = {}, []
        for offset in range(0, len(candidates), 128):
            batch = candidates[offset:offset + 128]
            # cwd output is also field-oriented; spaces in paths stay intact.
            cwd_text = self._lsof(batch, "-d", "cwd", "-Fpfn")
            pid, allowed = None, set()
            for line in cwd_text.splitlines():
                if line.startswith("p") and line[1:].isdigit():
                    pid = int(line[1:])
                elif line.startswith("n") and pid is not None and _within(line[1:], self.root):
                    allowed.add(pid)
            for pid in allowed:
                owned[pid] = processes[pid]["start"]
            if not allowed:
                continue
            listeners = self._lsof(allowed, "-iTCP", "-sTCP:LISTEN", "-FpnT")
            pid = None
            seen = set()
            for line in listeners.splitlines():
                if line.startswith("p") and line[1:].isdigit():
                    pid = int(line[1:])
                elif line.startswith("n") and pid in allowed:
                    address = line[1:]
                    host, separator, port_text = address.rpartition(":")
                    if not separator or not port_text.isdigit():
                        raise ProjectError("lsof listener 地址无法解析: " + _text(address))
                    port = int(port_text)
                    if not 1 <= port <= 65535 or (pid, port, host) in seen:
                        continue
                    seen.add((pid, port, host))
                    # Use an actual local interface, not an invented external
                    # hostname; wildcard listeners are reachable via loopback.
                    url_host = "127.0.0.1" if host in ("*", "0.0.0.0") else host
                    if url_host in ("[::]", "::"):
                        url_host = "[::1]"
                    if ":" in url_host and not url_host.startswith("["):
                        url_host = "[" + url_host + "]"
                    servers.append({"id": "server-" + _identity(str(pid), processes[pid]["start"], host, str(port)),
                                    "label": _text(labels.get(pid) or os.path.basename(processes[pid]["command"])),
                                    "pid": pid, "port": port, "url": "http://%s:%d" % (url_host, port),
                                    "status": "listening"})
        self._owned_pids = owned
        return sorted(servers, key=lambda item: (item["port"], item["pid"], item["url"])), errors

    def refresh(self):
        snapshot = {"host": _text(socket.gethostname()), "cwd": _text(self.cwd), "root": _text(self.cwd),
                    "git": False, "branch": "", "head": "",
                    "changes": {"added": 0, "removed": 0, "files": 0, "staged": 0,
                                "reference": "HEAD", "entries": []}, "scripts": [], "servers": []}
        errors = []
        try:
            snapshot["git"] = self._repository()
            snapshot["root"] = _text(self.root)
            if snapshot["git"]:
                branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
                snapshot["branch"] = _text(branch.stdout.decode("utf-8", "replace").strip()) if branch.returncode == 0 else "detached"
                snapshot["head"], snapshot["changes"], git_errors = self._changes()
                errors.extend(git_errors)
                if not snapshot["changes"]["headExists"]:
                    errors.append("HEAD 尚不存在；Changes 相对空树")
        except (OSError, ValueError, ProjectError) as error:
            errors.append("Git: " + _text(error))
            snapshot["changes"]["incomplete"] = True
        scripts, script_errors = self._load_scripts()
        errors.extend(script_errors)
        with self._lock:
            self._scripts = scripts
        snapshot["scripts"] = [{k: _text(v) if isinstance(v, str) else v for k, v in script.items() if not k.startswith("_")}
                               for script in scripts.values()]
        try:
            snapshot["servers"], server_errors = self._servers()
            errors.extend(server_errors)
        except (OSError, ValueError, ProjectError) as error:
            errors.append("Servers: " + _text(error))
        if errors:
            snapshot["error"] = "; ".join(dict.fromkeys(errors))
        return snapshot

    def close(self):
        with self._lock:
            self._closed = True
            self._scripts = {}
            # poll reaps finished children only. Live services keep their session,
            # inherited log descriptor and user-owned lifetime after UI exit.
            for child in self._children:
                child.poll()
