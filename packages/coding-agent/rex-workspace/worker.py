#!/usr/bin/env python3
"""Private JSON transport and asynchronous OS/project work for Rex Lua.

Only ``start`` invokes Rex. No worker command owns panes or selects an agent.
"""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

from project import ProjectError, ProjectState

FRAME_LIMIT = 1024 * 1024
PUBLISH_NAMES = frozenset(("view.json", "workspace.json", "preview.json"))
ROOT_PATTERN = re.compile(r"rsv-(\d+)-(\d+)-([a-f0-9]{10})\Z")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def private_directory(path, create=False):
    path = Path(path).absolute()
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("directory must be a private owned 0700 directory: " + str(path))
    # /tmp itself is a system symlink on macOS. Resolve parents, never the leaf.
    return path.parent.resolve() / path.name


def open_private(path, flags=os.O_RDONLY):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("file must be a private owned 0600 regular file: " + str(path))
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_json(path):
    fd = open_private(path)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(FRAME_LIMIT + 1)
    if len(raw) > FRAME_LIMIT:
        raise ValueError("JSON exceeds 1MiB")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON must be an object")
    return value


def encode(value):
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "backslashreplace")
    if len(data) > FRAME_LIMIT:
        raise ValueError("protocol frame exceeds 1MiB")
    return data


def atomic_json(root, name, data):
    if name not in PUBLISH_NAMES or not isinstance(data, dict):
        raise ValueError("invalid publish name or data")
    root = private_directory(root)
    raw = encode(data)
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        os.replace(temporary, root / name)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def text(value, limit=2000):
    # Keep responses within framing limits, without presenting a prefix as complete.
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "\n[显示已截断]"


def validate_source(root, live=True):
    root = private_directory(root)
    match = ROOT_PATTERN.fullmatch(root.name)
    if not match or int(match[1]) != os.getuid():
        raise ValueError("invalid root identity")
    source = read_json(root / "root.json")
    pid = source.get("pid")
    # Synced legacy extensions publish v2, but their OMP cannot run rex-view.
    if source.get("v") != 3 or type(pid) is not int or pid <= 0 or pid != int(match[2]):
        raise ValueError("not a native v3 source or invalid pid")
    for field in ("session", "anchor", "sessionId"):
        if not isinstance(source.get(field), str) or not source[field] or len(source[field]) > 1024:
            raise ValueError("invalid source " + field)
    if digest(source["anchor"])[:10] != match[3]:
        raise ValueError("root anchor identity mismatch")
    for field in ("sessionFile", "ompExecutable"):
        value = source.get(field)
        if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
            raise ValueError("source " + field + " must be absolute")
    executable = Path(source["ompExecutable"])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("source OMP executable is no longer available")
    if not isinstance(source.get("taskId"), str) or not source["taskId"]:
        raise ValueError("missing current task identity")
    command_path = source.get("commandPath")
    if command_path is not None and (not isinstance(command_path, str) or "\0" in command_path
            or any(not os.path.isabs(item) for item in command_path.split(os.pathsep))):
        raise ValueError("command PATH must contain only absolute directories")
    main = source.get("main")
    if not isinstance(main, dict) or not isinstance(main.get("cwd"), str) or not os.path.isabs(main["cwd"]) or "\0" in main["cwd"]:
        raise ValueError("source cwd must be absolute")
    display = source.get("display")
    if (not isinstance(display, dict) or display.get("action") not in ("show", "hide")
            or type(display.get("seq")) is not int or display["seq"] < 0
            or type(display.get("width")) is not int or not 15 <= display["width"] <= 35):
        raise ValueError("invalid display request")
    if not isinstance(source.get("agents"), list) or type(source.get("closed")) is not bool:
        raise ValueError("invalid source snapshot")
    keys = set()
    agents = []
    for agent in source["agents"]:
        if (not isinstance(agent, dict) or not isinstance(agent.get("id"), str)
                or type(agent.get("generation")) is not int or agent["generation"] < 0
                or agent.get("key") != "%s:%s" % (agent["id"], agent["generation"])
                or agent["key"] in keys):
            raise ValueError("invalid agent identity")
        keys.add(agent["key"])
        session_file = agent.get("sessionFile")
        if session_file is not None and (not isinstance(session_file, str)
                or not os.path.isabs(session_file) or "\0" in session_file):
            raise ValueError("agent sessionFile must be absolute")
        if agent.get("taskId") != source["taskId"]:
            raise ValueError("agent task ownership does not match current task")
        call_id = agent.get("parentToolCallId")
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise ValueError("invalid agent task-call ownership")
        item = dict(agent)
        agents.append(item)
    source["agents"] = agents
    if live and (source["closed"] or not alive(pid)):
        raise ValueError("source has ended")
    return root, source




def state_directory(module, create=True):
    override = os.environ.get("REX_WORKSPACE_STATE_DIR")
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(config):
        config = str(Path.home() / ".config")
    path = Path(override) if override else Path(config) / "rex-agent-workspace" / "state" / digest(str(Path(module).resolve()))[:12]
    return private_directory(path, create=create)


def lock_file(state, name="watch.lock"):
    return open_private(state / name, os.O_RDWR | os.O_CREAT)


def locked(state):
    fd = lock_file(state)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def remove_socket(path, inode=None):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("refusing to remove unsafe socket: " + str(path))
    if inode is not None and info.st_ino != inode:
        raise ValueError("control socket ownership changed")
    path.unlink()


def connect_request(path, request):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("unsafe control socket")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(encode(request))
        raw = bytearray()
        while b"\n" not in raw:
            part = connection.recv(4096)
            if not part:
                raise ValueError("control socket closed before acknowledgment")
            raw.extend(part)
            if len(raw) > FRAME_LIMIT:
                raise ValueError("oversized acknowledgment")
        ack = json.loads(raw.partition(b"\n")[0])
        if not isinstance(ack, dict) or ack.get("ok") is not True:
            raise ValueError(ack.get("error", "request rejected") if isinstance(ack, dict) else "invalid acknowledgment")
        return ack


class ProjectSlot:
    """One serial work stream per source; generations discard retired results."""

    def __init__(self, source, root, generation):
        self.identity = (source["pid"], source["sessionId"], source["main"]["cwd"])
        directory = private_directory(root / "project", create=True)
        self.project = ProjectState(self.identity[2], self.identity[0], directory,
                                    omp_executable=source["ompExecutable"],
                                    command_path=source.get("commandPath"), agent_dir=source.get("agentDir"))
        self.generation = generation
        self.snapshot = {}
        self.pending = deque()
        self.future = None
        self.kind = None
        self.refresh_at = 0
        self.retired = False

    def enqueue(self, kind, request=None):
        if kind == "refresh" and (self.kind == kind or any(item[0] == kind for item in self.pending)):
            return
        if len(self.pending) >= 8:
            if kind == "refresh":
                return
            raise ValueError("project action queue is full")
        self.pending.append((kind, request))

    def execute(self, kind, request):
        if self.retired:
            return None
        project = self.project
        if kind == "refresh":
            return project.refresh()
        if kind == "diff":
            return project.diff()
        if kind == "commit":
            return project.commit(request.get("message"))
        if kind == "add-script":
            return project.add_script(request.get("name"), request.get("command"))
        if kind == "run-script":
            return project.run_script(request.get("id"))
        if kind == "open-editor":
            program = shutil.which("cursor", path=project._env.get("PATH"))
            if not program:
                raise ProjectError("未找到 Cursor 命令")
            argument = project.cwd
        elif kind == "open-url":
            server = next((row for row in self.snapshot.get("servers", []) if row.get("id") == request.get("id")), None)
            if not server:
                raise ProjectError("服务已停止，请刷新")
            parsed = urlparse(server.get("url", ""))
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port != server.get("port"):
                raise ProjectError("服务地址无效")
            program = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
            if not program:
                raise ProjectError("无法打开系统浏览器")
            argument = server["url"]
        else:
            raise ValueError("unknown project action")
        subprocess.Popen([program, argument], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=project._env, start_new_session=True, close_fds=True)
        return "已打开"


class Watcher:
    def __init__(self, module, scan_dir=None, emit=None):
        self.module = Path(module).resolve()
        self.scan_dir = Path(scan_dir or os.environ.get("REX_WORKSPACE_SCAN_DIR", "/tmp")).resolve()
        self.state = state_directory(self.module)
        self.selector = selectors.DefaultSelector()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rex-project")
        self.emit = emit or self.stdout
        self.roots = {}
        self.clients = {}
        self.servers = {}
        self.retired = []
        self.generation = 0
        self.stopping = False
        self.explicit_stop = False
        self.shutdown_at = None
        self.input = bytearray()
        self.discard_input = False
        self.lock_fd = None
        self.stdin_flags = None

    @staticmethod
    def stdout(event):
        sys.stdout.buffer.write(encode(event))
        sys.stdout.buffer.flush()

    def listen(self, path, root=None):
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("unsafe existing control socket")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(0.1)
                try:
                    probe.connect(str(path))
                except (ConnectionRefusedError, FileNotFoundError):
                    remove_socket(path, info.st_ino)
                else:
                    raise ValueError("source already has a live observer")
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(16)
            server.setblocking(False)
            self.selector.register(server, selectors.EVENT_READ, ("server", root))
            self.servers[root] = (server, path, path.lstat().st_ino)
        except BaseException:
            server.close()
            raise

    def acquire(self):
        self.lock_fd = lock_file(self.state)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            self.lock_fd = None
            return False
        self.listen(self.state / "control.sock")
        return True

    def release_server(self, root):
        item = self.servers.pop(root, None)
        if item:
            server, path, inode = item
            self.selector.unregister(server)
            server.close()
            remove_socket(path, inode)

    def retire_project(self, entry):
        slot = entry.get("project")
        if slot:
            slot.retired = True
            slot.pending.clear()
            self.retired.append(slot)
            entry["project"] = None

    def scan(self):
        found = set()
        try:
            paths = self.scan_dir.iterdir()
            for path in paths:
                if not ROOT_PATTERN.fullmatch(path.name):
                    continue
                try:
                    root, source = validate_source(path)
                    key = str(root)
                    found.add(key)
                    entry = self.roots.get(key)
                    identity = (source["pid"], source["sessionId"], source["main"]["cwd"])
                    if entry is None:
                        # A module-level singleton is insufficient when two Rex
                        # profiles scan the same OMP source. Claim the root first.
                        root_lock = lock_file(root, "workspace.lock")
                        try:
                            fcntl.flock(root_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            self.listen(root / "control.sock", key)
                        except BaseException:
                            os.close(root_lock)
                            raise
                        try:
                            layout = read_json(root / "workspace.json")
                        except (OSError, ValueError, UnicodeError, RecursionError):
                            layout = None
                        entry = {"root": root, "source": None, "project": None, "active": True,
                                 "identity": identity, "lock_fd": root_lock}
                        self.roots[key] = entry
                    else:
                        layout = None
                        if not entry["active"]:
                            self.listen(root / "control.sock", key)
                            try:
                                layout = read_json(root / "workspace.json")
                            except (OSError, ValueError, UnicodeError, RecursionError):
                                pass
                        if entry["identity"] != identity:
                            self.retire_project(entry)
                        entry["identity"] = identity
                    if source != entry["source"] or not entry["active"]:
                        entry["source"] = source
                        entry["active"] = True
                        self.emit({"type": "source", "root": key, "source": source, "layout": layout})
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    continue  # One corrupt/private legacy source never hides other sessions.
        except OSError:
            pass
        for key, entry in list(self.roots.items()):
            if entry["active"] and key not in found:
                entry["active"] = False
                self.retire_project(entry)
                self.emit({"type": "gone", "root": key})
                self.release_server(key)

    def root_entry(self, value, active=True):
        if not isinstance(value, str):
            raise ValueError("missing source root")
        root = private_directory(value)
        entry = self.roots.get(str(root))
        if entry is None or (active and not entry["active"]):
            raise ValueError("unknown or inactive source")
        if active:
            _, source = validate_source(root)
            identity = (source["pid"], source["sessionId"], source["main"]["cwd"])
            if identity != entry["identity"]:
                raise ValueError("source changed; wait for fresh snapshot")
        return entry

    def command(self, command):
        if not isinstance(command, dict):
            raise ValueError("command must be an object")
        kind = command.get("type")
        if kind == "stop":
            self.stopping = self.explicit_stop = True
            return
        if self.shutdown_at is not None and kind not in ("publish", "forget"):
            raise ValueError("watcher is shutting down")
        entry = self.root_entry(command.get("root"), active=kind not in ("publish", "forget"))
        root = str(entry["root"])
        if kind == "publish":
            atomic_json(entry["root"], command.get("name"), command.get("data"))
        elif kind == "forget":
            self.retire_project(entry)
            if not entry["active"]:
                self.roots.pop(root, None)
                os.close(entry["lock_fd"])
        elif kind == "observe":
            if entry["project"] is None:
                self.generation += 1
                entry["project"] = ProjectSlot(entry["source"], entry["root"], self.generation)
            entry["project"].enqueue("refresh")
        elif kind == "project":
            slot = entry.get("project")
            request = command.get("request")
            if not slot or not isinstance(request, dict):
                raise ValueError("project is not observed")
            action = request.get("type")
            if action not in ("diff", "commit", "run-script", "add-script", "open-editor", "open-url"):
                raise ValueError("unknown project action")
            if action in ("run-script", "open-url"):
                rows = slot.snapshot.get("scripts" if action == "run-script" else "servers", [])
                if not isinstance(request.get("id"), str) or not any(row.get("id") == request["id"] for row in rows):
                    raise ValueError("item is not in current project snapshot")
            slot.enqueue(action, dict(request))
        else:
            raise ValueError("unknown worker command")

    def project_tick(self):
        now = time.monotonic()
        for key, entry in list(self.roots.items()):
            slot = entry.get("project")
            if not slot:
                continue
            if slot.future and slot.future.done():
                future, kind = slot.future, slot.kind
                slot.future, slot.kind = None, None
                try:
                    result = future.result()
                    if kind == "refresh":
                        slot.snapshot = result
                        self.emit({"type": "project", "root": key, "project": result})
                    else:
                        self.emit({"type": "response", "root": key, "event": {
                            "type": "diff" if kind == "diff" else "notice", "text": text(result, 120000 if kind == "diff" else 2000)}})
                except Exception as error:
                    self.emit({"type": "response", "root": key, "event": {"type": "notice", "error": True, "text": text(error)}})
                if kind != "refresh":
                    slot.refresh_at = 0
            if now - slot.refresh_at >= 2.5:
                slot.enqueue("refresh")
                slot.refresh_at = now
            if slot.future is None and slot.pending:
                slot.kind, request = slot.pending.popleft()
                slot.future = self.pool.submit(slot.execute, slot.kind, request)
        remaining = []
        for slot in self.retired:
            if slot.future and not slot.future.done():
                remaining.append(slot)
            else:
                slot.project.close()
        self.retired = remaining

    def target_request(self, message, socket_root):
        if not isinstance(message, dict):
            raise ValueError("request envelope must be an object")
        if message.get("type") == "stop" and socket_root is None:
            if self.shutdown_at is None:
                self.explicit_stop = True
                self.shutdown_at = time.monotonic() + 12
                for entry in self.roots.values():
                    self.retire_project(entry)
                self.emit({"type": "shutdown"})
            return
        if message.get("type") != "request" or message.get("view") not in ("panel", "detail", "preview") or not isinstance(message.get("request"), dict):
            raise ValueError("invalid UI request envelope")
        if socket_root is None:
            session, anchor = message.get("session"), message.get("anchor")
            if not isinstance(session, str) or not isinstance(anchor, str):
                raise ValueError("session and anchor required")
            matches = []
            for key, entry in self.roots.items():
                if not entry["active"] or entry["source"]["session"] != session:
                    continue
                blocks = [entry["source"]["anchor"]]
                try:
                    layout = read_json(entry["root"] / "workspace.json")
                    if (layout.get("v") == 2 and layout.get("controller") == "lua"
                            and layout.get("ompPid") == entry["source"]["pid"]
                            and layout.get("session") == session and layout.get("anchor") == entry["source"]["anchor"]
                            and layout.get("sessionId") == entry["source"]["sessionId"]):
                        blocks.extend(layout.get(name) for name in ("panel", "detail", "preview"))
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    pass
                if anchor in blocks:
                    matches.append(key)
            if len(matches) != 1:
                raise ValueError("no unique workspace matches this session/block")
            socket_root = matches[0]
        self.root_entry(socket_root)
        self.emit({"type": "request", "root": socket_root, "view": message["view"], "request": message["request"]})

    def disconnect(self, client):
        connection = client["socket"]
        self.clients.pop(connection.fileno(), None)
        self.selector.unregister(connection)
        connection.close()

    def receive(self, client, mask):
        connection = client["socket"]
        if mask & selectors.EVENT_WRITE:
            sent = connection.send(client["output"])
            del client["output"][:sent]
            if not client["output"]:
                self.disconnect(client)
            return
        data = connection.recv(65536)
        if not data:
            self.disconnect(client)
            return
        client["input"].extend(data)
        if len(client["input"]) > FRAME_LIMIT:
            error = "request exceeds 1MiB"
        elif b"\n" not in client["input"]:
            return
        else:
            try:
                message = json.loads(client["input"].partition(b"\n")[0])
                self.target_request(message, client["root"])
                client["output"] = bytearray(encode({"ok": True}))
                self.selector.modify(connection, selectors.EVENT_WRITE, ("client", client))
                return
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exception:
                error = text(exception)
        client["output"] = bytearray(encode({"ok": False, "error": error}))
        self.selector.modify(connection, selectors.EVENT_WRITE, ("client", client))

    def stdin(self):
        data = os.read(sys.stdin.fileno(), 65536)
        if not data:
            self.stopping = True
            return
        for part in data.splitlines(keepends=True):
            complete = part.endswith(b"\n")
            if not self.discard_input:
                self.input.extend(part)
                if len(self.input) > FRAME_LIMIT:
                    self.input.clear()
                    self.discard_input = True
            if complete:
                if not self.discard_input:
                    command = None
                    try:
                        command = json.loads(self.input)
                        self.command(command)
                    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
                        root = command.get("root") if isinstance(command, dict) else None
                        self.emit({"type": "response", "root": root, "event": {"type": "notice", "error": True, "text": text(error)}})
                self.input.clear()
                self.discard_input = False

    def run(self):
        try:
            if not self.acquire():
                return 0
            self.stdin_flags = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
            os.set_blocking(sys.stdin.fileno(), False)
            self.selector.register(sys.stdin.fileno(), selectors.EVENT_READ, ("stdin", None))
            self.emit({"type": "ready", "pid": os.getpid()})
            next_scan = 0
            while not self.stopping:
                now = time.monotonic()
                if self.shutdown_at is not None and now >= self.shutdown_at:
                    break
                if self.shutdown_at is None and now >= next_scan:
                    self.scan()
                    next_scan = now + 0.25
                self.project_tick()
                for key, mask in self.selector.select(0.05):
                    kind, payload = key.data
                    try:
                        if kind == "stdin":
                            self.stdin()
                        elif kind == "server":
                            connection, _ = key.fileobj.accept()
                            connection.setblocking(False)
                            if len(self.clients) >= 64:
                                connection.close()
                                continue
                            client = {"socket": connection, "root": payload, "input": bytearray(), "output": bytearray(), "at": time.monotonic()}
                            self.clients[connection.fileno()] = client
                            self.selector.register(connection, selectors.EVENT_READ, ("client", client))
                        else:
                            self.receive(payload, mask)
                    except (OSError, ValueError) as error:
                        if kind == "client" and payload["socket"].fileno() >= 0:
                            self.disconnect(payload)
                        print(text(error), file=sys.stderr, flush=True)
                for client in list(self.clients.values()):
                    if time.monotonic() - client["at"] > 3:
                        self.disconnect(client)
            # A stop ack must be delivered before closing sockets. This does not
            # wait for Lua to handle shutdown and cannot deadlock its event loop.
            for client in list(self.clients.values()):
                if client["output"]:
                    try:
                        client["socket"].send(client["output"])
                    except OSError:
                        pass
            return 0
        finally:
            self.close()

    def close(self):
        for entry in self.roots.values():
            self.retire_project(entry)
        for slot in self.retired:
            slot.retired = True
        for client in list(self.clients.values()):
            self.disconnect(client)
        for root in list(self.servers):
            self.release_server(root)
        for entry in self.roots.values():
            os.close(entry["lock_fd"])
        self.roots.clear()
        self.selector.close()
        if self.stdin_flags is not None:
            fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, self.stdin_flags)
        # Never close ProjectState while a serial action holds its lock; drain
        # queued OS work before releasing observers, without signaling services.
        self.pool.shutdown(wait=True, cancel_futures=True)
        for slot in self.retired:
            slot.project.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None




def start(module, rex):
    module = Path(module).resolve()
    state = state_directory(module)
    fd = lock_file(state, "start.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if locked(state):
            return 0
        log_fd = open_private(state / "watcher.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            child = subprocess.Popen([rex, "--autostart=false", "do", str(module / "lua" / "watcher.lua"),
                                      "--all", "module=" + str(module), "state_dir=" + str(state),
                                      "scan_dir=" + str(Path(os.environ.get("REX_WORKSPACE_SCAN_DIR", "/tmp")).resolve()),
                                      "rex_binary=" + rex], stdin=subprocess.DEVNULL,
                                     stdout=log_fd, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", OMP_CONFIG_SYNC="0",
                                              REX_WORKSPACE_REX=os.environ.get("REX_WORKSPACE_REX") or rex))
        finally:
            os.close(log_fd)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if locked(state):
                return 0
            if child.poll() is not None:
                raise ValueError("Rex watcher exited before ready; see " + str(state / "watcher.log"))
            time.sleep(0.05)
        raise ValueError("Rex watcher did not acquire singleton; see " + str(state / "watcher.log"))
    finally:
        os.close(fd)


def stop(module):
    try:
        state = state_directory(module, create=False)
        if not locked(state):
            return 0
        connect_request(state / "control.sock", {"type": "stop"})
        # Installer can safely replace files after this command returns. The
        # daemon releases its lock only after all own cleanup has completed.
        deadline = time.monotonic() + 15
        while locked(state):
            if time.monotonic() >= deadline:
                raise ValueError("watcher is still shutting down")
            time.sleep(0.05)
        return 0
    except FileNotFoundError:
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("watch", "start", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--module", required=True)
        if name == "watch":
            command.add_argument("--scan-dir")
        if name == "start":
            command.add_argument("--rex", required=True)
    command = commands.add_parser("request")
    command.add_argument("--root")
    command.add_argument("--module", default=str(Path(__file__).resolve().parent))
    command.add_argument("--session")
    command.add_argument("--anchor")
    command.add_argument("--view", choices=("panel", "detail", "preview"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            return Watcher(args.module, args.scan_dir).run()
        if args.command == "start":
            return start(args.module, args.rex)
        if args.command == "stop":
            return stop(args.module)
        raw = sys.stdin.buffer.read(FRAME_LIMIT + 1)
        if len(raw) > FRAME_LIMIT:
            raise ValueError("request exceeds 1MiB")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        envelope = {"type": "request", "view": args.view, "request": request}
        if args.root:
            if args.session or args.anchor:
                raise ValueError("use root or session/anchor, not both")
            root, _ = validate_source(args.root)
            path = root / "control.sock"
        else:
            if not args.session or not args.anchor:
                raise ValueError("root or session and anchor required")
            envelope.update(session=args.session, anchor=args.anchor)
            path = state_directory(args.module, create=False) / "control.sock"
        connect_request(path, envelope)
        return 0
    except BrokenPipeError:
        # Avoid a second BrokenPipe during interpreter stdout finalization.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        print(text(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
