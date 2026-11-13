"""Read-only project observations and explicit, user-initiated project actions.

No process is launched by refresh except bounded inspection commands. Script
processes outlive this observer; close deliberately never signals them.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
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
        self.root = next((str(path) for path in (Path(self.cwd), *Path(self.cwd).parents)
                          if (path / ".git").exists()), self.cwd)
        config_home = os.environ.get("XDG_CONFIG_HOME", "")
        if not os.path.isabs(config_home):
            config_home = str(Path.home() / ".config")
        self.config = Path(config_home) / "rex-subagent-viewer" / "projects" / (_identity(self.cwd) + ".json")
        self._lock = threading.RLock()
        self._scripts = {}
        self._children = []
        self._script_runs = {}
        self._script_failures = {}
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
    def _read_text(path):
        # Discovery never evaluates project configuration or invokes its tools.
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ProjectError("配置超过 1MiB: " + str(path))
        try:
            return raw.decode("utf-8-sig")
        except UnicodeError as error:
            raise ProjectError("配置 UTF-8 无效: " + str(path)) from error

    @classmethod
    def _read_json(cls, path, jsonc=False):
        raw = cls._read_text(path)
        if jsonc:
            # Match strings first so comment markers and commas inside argv/env
            # remain literal. JSONC supports comments and trailing commas only.
            string = r'"(?:\\.|[^"\\])*"'
            raw = re.sub(string + r"|//[^\r\n]*|/\*[\s\S]*?\*/",
                         lambda match: match[0] if match[0].startswith('"') else " ", raw)
            raw = re.sub(string + r"|,\s*(?=[}\]])",
                         lambda match: match[0] if match[0].startswith('"') else "", raw)
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

    def _nearest_file(self, names):
        directory = Path(self.cwd)
        while _within(directory, self.root):
            for name in names:
                path = directory / name
                if path.is_file():
                    if not _within(path, self.root):
                        raise ProjectError("项目配置指向项目外: " + str(path))
                    return path
            if str(directory) == self.root or directory.parent == directory:
                break
            directory = directory.parent
        return None

    @staticmethod
    def _script_kind(name):
        words = set(re.split(r"[^a-z0-9]+", name.lower()))
        for kind, labels in (("dev", {"dev", "start", "serve", "watch"}),
                             ("build", {"build", "compile", "bundle"}),
                             ("test", {"test", "spec"}),
                             ("check", {"check", "lint", "typecheck", "format", "verify"})):
            if words & labels:
                return kind
        return "task"

    def _register_script(self, scripts, prefix, name, command, source, cwd, argv, stamp, env=None):
        name, command = self._validate_script(name, command)
        key = prefix + "-" + _identity(str(cwd), name, command, stamp)
        scripts[key] = {"id": key, "label": name, "command": command, "source": source,
                        "kind": self._script_kind(name), "_cwd": str(cwd), "_argv": argv}
        if env is not None:
            scripts[key]["_env"] = env

    def _package_scripts(self, scripts):
        package = self._package_directory()
        if not package:
            return
        data = self._read_json(package / "package.json")
        if not isinstance(data, dict) or not isinstance(data.get("scripts", {}), dict):
            raise ProjectError("package.json scripts 必须为对象")
        declared, lock_manager, scan, stamps = None, None, package, []
        # Installed tools never decide which package manager owns a project.
        while True:
            manifest = scan / "package.json"
            if manifest.is_file():
                manifest_data = data if scan == package else self._read_json(manifest)
                stamps.append(self._read_text(manifest))
                if not isinstance(manifest_data, dict):
                    raise ProjectError("package.json 必须为对象: " + str(manifest))
                value = manifest_data.get("packageManager")
                if declared is None and value is not None:
                    if not isinstance(value, str) or not re.fullmatch(r"(npm|pnpm|yarn|bun)@[^\s\x00]+", value):
                        raise ProjectError("不支持的 packageManager: " + _text(value))
                    declared = value.split("@", 1)[0]
            if lock_manager is None:
                locks = (("bun.lock", "bun"), ("bun.lockb", "bun"), ("pnpm-lock.yaml", "pnpm"),
                         ("yarn.lock", "yarn"), ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"))
                found = [(scan / filename, runner) for filename, runner in locks if (scan / filename).is_file()]
                if len({runner for _, runner in found}) > 1:
                    raise ProjectError("同目录存在不同执行器的 lockfile，请先消除歧义: " + str(scan))
                if found:
                    lock_manager = found[0][1]
                    stamps.extend(str(path) for path, _ in found)
            if str(scan) == self.root or scan.parent == scan or not _within(scan.parent, self.root):
                break
            scan = scan.parent
        if declared and lock_manager and declared != lock_manager:
            raise ProjectError("packageManager 与最近 lockfile 执行器冲突")
        manager = declared or lock_manager or "npm"
        stamp = _identity(manager, *stamps)
        for name, command in data.get("scripts", {}).items():
            self._validate_script(name, command)
            argv = [manager, "run", *(["--"] if name.startswith("-") else []), name]
            self._register_script(scripts, "package", name, command, "package.json", package, argv, stamp)

    def _launch_scripts(self, scripts, errors):
        path = self._nearest_file((".claude/launch.json",))
        if path is None:
            return
        data = self._read_json(path, jsonc=True)
        if not isinstance(data, dict) or not isinstance(data.get("configurations"), list):
            raise ProjectError(str(path) + ": configurations 必须为数组")
        unknown = set(data) - {"version", "configurations"}
        if unknown:
            raise ProjectError(str(path) + ": 不支持的字段 " + ", ".join(sorted(unknown)))
        workspace = path.parent.parent.resolve()
        stamp = _identity(str(path), self._read_text(path))
        names = set()
        for index, item in enumerate(data["configurations"]):
            try:
                if not isinstance(item, dict):
                    raise ProjectError("配置必须为对象")
                unknown = set(item) - {"name", "runtimeExecutable", "runtimeArgs", "program", "args", "cwd", "env", "port", "autoPort"}
                if unknown:
                    raise ProjectError("不支持的启动字段: " + ", ".join(sorted(unknown)))
                if "autoPort" in item and item["autoPort"] is not False:
                    raise ProjectError("不支持 Desktop autoPort；请使用明确端口与命令")
                if "port" in item and (type(item["port"]) is not int or not 1 <= item["port"] <= 65535):
                    raise ProjectError("port 必须是 1–65535 的整数")
                name = item.get("name")
                self._validate_script(name, "launch")
                if name.strip() in names:
                    raise ProjectError("启动配置名称重复: " + name)
                names.add(name.strip())

                def literal(value):
                    if not isinstance(value, str) or "\0" in value or any(unicodedata.category(c) == "Cs" for c in value):
                        raise ProjectError("启动参数、cwd 和 env 必须为不含 NUL 的字符串")
                    value = value.replace("${workspaceFolder}", str(workspace))
                    if "${" in value:
                        raise ProjectError("不支持的启动变量: " + _text(value))
                    return value

                def arguments(field):
                    values = item.get(field, [])
                    if not isinstance(values, list):
                        raise ProjectError(field + " 必须为字符串数组")
                    return [literal(value) for value in values]

                executable, program = item.get("runtimeExecutable"), item.get("program")
                if executable is None and program is None:
                    raise ProjectError("缺少 runtimeExecutable 或 program")
                if executable is None and item.get("runtimeArgs"):
                    raise ProjectError("runtimeArgs 需要 runtimeExecutable")
                argv = ([literal(executable)] + arguments("runtimeArgs")) if executable is not None else []
                if program is not None:
                    argv.append(literal(program))
                argv.extend(arguments("args"))
                if not argv[0].strip():
                    raise ProjectError("启动程序不能为空")
                directory = (workspace / Path(literal(item.get("cwd", str(workspace))))).resolve()
                if not _within(directory, self.root) or not directory.is_dir():
                    raise ProjectError("启动 cwd 必须是项目内已有目录: " + str(directory))
                environment = item.get("env", {})
                if not isinstance(environment, dict) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in environment):
                    raise ProjectError("env 必须为环境变量名称到字符串的对象")
                environment = {key: literal(value) for key, value in environment.items()}
                self._register_script(scripts, "launch", name, shlex.join(argv),
                                      str(path.relative_to(self.root)), directory, argv, stamp, environment)
            except ProjectError as error:
                errors.append(_text("%s configurations[%d]: %s" % (path, index, error)))

    def _taskfile_scripts(self, scripts, errors, just=False):
        path = self._nearest_file(("justfile", "Justfile", ".justfile") if just else ("GNUmakefile", "makefile", "Makefile"))
        if path is None:
            return
        raw = self._read_text(path)
        stamp = _identity(str(path), raw)
        names, blocked, private = set(), 0, False
        multiline = None
        for line in re.sub(r"\\\r?\n", " ", raw).splitlines():
            if just:
                if multiline:
                    if multiline in line:
                        multiline = None
                    continue
                delimiters = re.findall(r"'''|\"\"\"", line.split("#", 1)[0])
                if delimiters:
                    if delimiters.count(delimiters[0]) % 2:
                        multiline = delimiters[0]
                    continue
            if not line or line[0].isspace() or line.startswith("#"):
                continue
            if just:
                if line.startswith("["):
                    private = private or bool(re.search(r"\bprivate\b", line))
                    continue
                if re.match(r"(import|mod)\s", line):
                    errors.append(str(path) + ": 静态发现不展开 just import/mod")
                match = re.match(r"@?([A-Za-z_][A-Za-z0-9_-]*)([^:]*):(?![=])", line)
                if not match:
                    private = False
                    continue
                try:
                    parameters = shlex.split(match[2], comments=True)
                except ValueError:
                    errors.append(str(path) + ": 无法静态解析 recipe 参数: " + match[1])
                    private = False
                    continue
                required = any("=" not in parameter and not parameter.startswith("*") for parameter in parameters)
                if not private and not required:
                    names.add(match[1])
                private = False
            else:
                if re.match(r"(define|ifdef|ifndef|ifeq|ifneq)\b", line):
                    blocked += 1
                elif re.match(r"(endef|endif)\b", line):
                    blocked = max(0, blocked - 1)
                if blocked:
                    continue
                if re.match(r"\.RECIPEPREFIX\s*[:?+]?=", line):
                    errors.append(str(path) + ": 自定义 recipe 前缀无法安全静态发现")
                    return
                if re.match(r"-?(include|sinclude)\s", line):
                    errors.append(str(path) + ": 静态发现不展开 include")
                match = re.match(r"([^:=]+?)\s*::?(?![=])\s*(.*)", line)
                if not match or re.match(r"(?:override\s+|private\s+|export\s+)?[\w.-]+\s*[:?+!]?=", match[2]):
                    continue
                for name in match[1].split():
                    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", name):
                        names.add(name)
        for name in sorted(names):
            argv = ["just", "--justfile", str(path), "--", name] if just else ["make", "-f", str(path), "--", name]
            self._register_script(scripts, "just" if just else "make", name, shlex.join(argv),
                                  str(path.relative_to(self.root)), path.parent, argv, stamp)

    def _load_scripts(self):
        scripts, errors = {}, []
        for discover in (lambda: self._package_scripts(scripts),
                         lambda: self._launch_scripts(scripts, errors),
                         lambda: self._taskfile_scripts(scripts, errors),
                         lambda: self._taskfile_scripts(scripts, errors, just=True)):
            try:
                discover()
            except (OSError, ProjectError) as error:
                errors.append(_text(error))
        try:
            for item in self._custom_scripts():
                name, command = item["name"], item["command"]
                self._register_script(scripts, "user", name, command, "user", self.cwd,
                                      ["/bin/sh", "-c", command], command)
        except (OSError, ProjectError) as error:
            errors.append(_text(error))
        return scripts, list(dict.fromkeys(errors))

    def script_snapshot(self):
        with self._lock:
            scripts, errors = self._load_scripts()
            self._scripts = scripts
            errors.extend(message for key, message in self._script_failures.items() if key in scripts)
            public = []
            for key, script in scripts.items():
                # Confirmation needs the entire command, never a display-width
                # preview. _text sanitizes controls without truncating content.
                item = {key: _text(script[key]) for key in ("id", "label", "command", "source", "kind")}
                item["cwd"] = _text(script["_cwd"])
                run = self._script_runs.get(key)
                if run:
                    child = run["child"]
                    code = child.poll()
                    item.update(state="running" if code is None else "done" if code == 0 else "failed",
                                pid=child.pid, exitCode=code, logFile=_text(run["logFile"]))
                public.append(item)
            return {"scripts": public, "errors": errors}

    def add_script(self, name, command, replace=False):
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
                existing = next((item for item in scripts if item["name"] == name), None)
                if existing:
                    if existing["command"] == command:
                        return "已保存 " + _text(name)
                    if not replace:
                        raise ProjectError("已有同名项目命令；使用 replace 明确替换: " + name)
                    existing["command"] = command
                else:
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
            running = self._script_runs.get(id)
            if running and running["child"].poll() is None:
                return "仍在运行 %s · PID %d · %s" % (_text(script["label"]), running["child"].pid, running["logFile"])
            fd, path = tempfile.mkstemp(prefix="script-", suffix=".log", dir=self.state_dir)
            try:
                with os.fdopen(fd, "ab", buffering=0) as log:
                    os.fchmod(log.fileno(), 0o600)
                    environment = dict(self._env, **script.get("_env", {}))
                    environment["OMP_CONFIG_SYNC"] = "0"
                    child = subprocess.Popen(script["_argv"], cwd=script["_cwd"], env=environment,
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True, close_fds=True)
            except OSError as error:
                with open(path, "ab") as log:
                    log.write((_text(error) + "\n").encode("utf-8", "replace"))
                message = "启动失败: " + _text(error) + "；日志: " + path
                self._script_runs.pop(id, None)
                self._script_failures[id] = message
                raise ProjectError(message) from error
            self._children.append(child)
            self._script_runs[id] = {"child": child, "logFile": path}
            self._script_failures.pop(id, None)
        return "已启动 %s · PID %d · %s" % (_text(script["label"]), child.pid, path)

    def _editor(self):
        for label, executable, bundle, binary in (
                ("Cursor", "cursor", "Cursor.app", "Cursor"),
                ("VS Code", "code", "Visual Studio Code.app", "Electron"),
                ("Zed", "zed", "Zed.app", "zed")):
            command = shutil.which(executable, path=self._env.get("PATH", ""))
            if command:
                return label, [command, self.cwd]
            for directory in (Path("/Applications"), Path.home() / "Applications"):
                app = directory / bundle
                if (app / "Contents" / "MacOS" / binary).is_file() and os.access("/usr/bin/open", os.X_OK):
                    return label, ["/usr/bin/open", "-a", str(app), self.cwd]
        return None

    def editor_info(self):
        editor = self._editor()
        return {"label": editor[0], "available": True} if editor else None

    def open_editor(self):
        with self._lock:
            if self._closed:
                raise ProjectError("项目观察已关闭")
            editor = self._editor()
            if editor is None:
                raise ProjectError("未检测到 Cursor、VS Code 或 Zed")
            self._run(editor[1])
            return "已打开 " + editor[0]

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
        commands = self.script_snapshot()
        errors.extend(commands["errors"])
        snapshot["scripts"] = commands["scripts"]
        snapshot["editor"] = self.editor_info()
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
