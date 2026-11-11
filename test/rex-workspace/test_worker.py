"""Private transport/project behavior, without Rex UI or model requests.

The parent runs these tests only after integrating the Lua cutover.
"""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[2] / "packages/coding-agent/rex-workspace"
sys.path.insert(0, str(HERE))
try:
    SPEC = importlib.util.spec_from_file_location("rex_workspace_worker", HERE / "worker.py")
    worker = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(worker)
finally:
    sys.path.pop(0)


def private_write(path, value):
    raw = json.dumps(value).encode() if not isinstance(value, bytes) else value
    with open(path, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)


class Fixture(unittest.TestCase):
    def setUp(self):
        # Keep AF_UNIX paths below Darwin's sun_path limit.
        temporary = tempfile.TemporaryDirectory(prefix="rww-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.scan = self.base / "scan"
        self.scan.mkdir(mode=0o700)
        self.repo = self.base / "repo"
        self.repo.mkdir(mode=0o700)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(REX_WORKSPACE_SCAN_DIR=str(self.scan), REX_WORKSPACE_STATE_DIR=str(self.base / "state"),
                           XDG_CONFIG_HOME=str(self.base / "config"), OMP_CONFIG_SYNC="0", PYTHONDONTWRITEBYTECODE="1",
                           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Worker test",
                           GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_NAME="Worker test",
                           GIT_COMMITTER_EMAIL="test@example.invalid")
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.source = self.make_source()
        self.root = self.source[0]
        self.events = []
        self.watcher = worker.Watcher(HERE, emit=self.events.append)
        self.addCleanup(self.watcher.close)

    def make_source(self, anchor="anchor", cwd=None, agents=None):
        root = self.scan / ("rsv-%s-%s-%s" % (os.getuid(), os.getpid(), worker.digest(anchor)[:10]))
        root.mkdir(mode=0o700)
        source = {"v": 3, "pid": os.getpid(), "session": "session", "anchor": anchor, "sessionId": "main-session",
                  "sessionFile": str(self.base / "main.jsonl"), "ompExecutable": sys.executable,
                  "taskId": "current-task",
                  "main": {"state": "working", "intent": "", "cwd": str(cwd or self.repo), "startedAt": 1},
                  "agents": agents or [], "closed": False, "display": {"action": "show", "seq": 1, "width": 25}}
        private_write(root / "root.json", source)
        return root, source

    def save(self, source=None, root=None):
        private_write((root or self.root) / "root.json", source or self.source[1])


    def pump(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.watcher.project_tick()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("asynchronous project result timed out")

    def observe(self):
        self.watcher.scan()
        self.watcher.command({"type": "observe", "root": str(self.root)})
        slot = self.watcher.roots[str(self.root)]["project"]
        patch = mock.patch.object(slot.project, "_servers", return_value=([], []))
        patch.start()
        self.addCleanup(patch.stop)
        self.pump(lambda: bool(slot.snapshot))
        return slot

    def action(self, request):
        start = len(self.events)
        self.watcher.command({"type": "project", "root": str(self.root), "request": request})
        self.pump(lambda: any(event["type"] == "response" for event in self.events[start:]))
        return next(event["event"] for event in self.events[start:] if event["type"] == "response")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def repository(self, cwd=None):
        cwd = cwd or self.repo
        self.git("init", "--initial-branch=topic", cwd=cwd)
        (cwd / "tracked").write_text("baseline\n")
        self.git("add", "tracked", cwd=cwd)
        self.git("commit", "-m", "initial", cwd=cwd)


class SecurityTests(Fixture):
    def test_legacy_and_identity_mismatch_ignored_while_native_source_recovers(self):
        bad, source = self.make_source("legacy")
        source["v"] = 1
        self.save(source, bad)
        legacy, source = self.make_source("standalone")
        source["v"] = 2
        self.save(source, legacy)
        other, source = self.make_source("wrong")
        source["anchor"] = "another"
        self.save(source, other)
        self.watcher.scan()
        self.assertEqual(list(self.watcher.roots), [str(self.root)])
        self.assertEqual(self.events[0]["type"], "source")
        self.assertIsNone(self.events[0]["layout"])

    def test_missing_or_nonexecutable_runtime_is_not_adopted(self):
        executable = self.base / "retired-runtime"
        self.source[1]["ompExecutable"] = str(executable)
        self.save()
        self.watcher.scan()
        self.assertNotIn(str(self.root), self.watcher.roots)
        executable.write_text("not executable")
        executable.chmod(0o600)
        self.watcher.scan()
        self.assertNotIn(str(self.root), self.watcher.roots)
        executable.unlink()
        executable.symlink_to(self.base / "missing-target")
        self.watcher.scan()
        self.assertNotIn(str(self.root), self.watcher.roots)

    def test_root_symlink_and_permissive_directory_rejected(self):
        alias = self.scan / ("rsv-%s-%s-%s" % (os.getuid(), os.getpid(), worker.digest("alias")[:10]))
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            worker.validate_source(alias)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            worker.validate_source(self.root)

    def test_source_symlink_fifo_and_permissions_rejected_without_blocking(self):
        path = self.root / "root.json"
        original = path.read_bytes()
        path.unlink()
        target = self.base / "source"
        private_write(target, original)
        path.symlink_to(target)
        with self.assertRaises(OSError):
            worker.validate_source(self.root)
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with self.assertRaises(ValueError):
            worker.validate_source(self.root)
        self.assertLess(time.monotonic() - before, 0.5)
        path.unlink()
        private_write(path, original)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            worker.validate_source(self.root)

    def test_hardlinked_and_oversized_json_rejected(self):
        extra = self.root / "hardlink"
        os.link(self.root / "root.json", extra)
        with self.assertRaises(ValueError):
            worker.validate_source(self.root)
        extra.unlink()
        private_write(self.root / "root.json", b" " * (worker.FRAME_LIMIT + 1))
        with self.assertRaises(ValueError):
            worker.validate_source(self.root)

    def test_agent_from_a_different_task_is_rejected(self):
        self.source[1]["agents"] = [{
            "key": "agent:3", "id": "agent", "generation": 3,
            "sessionFile": str(self.base / "child.jsonl"), "taskId": "previous-task",
            "parentToolCallId": "call",
        }]
        self.save()
        with self.assertRaisesRegex(ValueError, "task ownership"):
            worker.validate_source(self.root)

    def test_source_without_native_session_metadata_is_not_adopted(self):
        del self.source[1]["sessionFile"]
        self.save()
        self.watcher.scan()
        self.assertNotIn(str(self.root), self.watcher.roots)
        self.assertFalse((self.root / "control.sock").exists())


    def test_publish_names_private_atomic_replacement_and_unknown_root(self):
        self.watcher.scan()
        outside = self.base / "outside"
        private_write(outside, b"untouched")
        (self.root / "view.json").symlink_to(outside)
        self.watcher.command({"type": "publish", "root": str(self.root), "name": "view.json", "data": {"v": 1}})
        self.assertEqual(outside.read_bytes(), b"untouched")
        self.assertFalse((self.root / "view.json").is_symlink())
        self.assertEqual(stat.S_IMODE((self.root / "view.json").stat().st_mode), 0o600)
        for name in ("../other", "root.json", "control.sock"):
            with self.assertRaises(ValueError):
                self.watcher.command({"type": "publish", "root": str(self.root), "name": name, "data": {}})
        with self.assertRaises(ValueError):
            self.watcher.command({"type": "publish", "root": str(self.base), "name": "view.json", "data": {}})

    def test_root_lock_competition_precedes_socket_creation_and_releases_on_gone_forget(self):
        fd = worker.lock_file(self.root, "workspace.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.watcher.scan()
            self.assertNotIn(str(self.root), self.watcher.roots)
            self.assertFalse((self.root / "control.sock").exists())
        finally:
            os.close(fd)
        self.watcher.scan()
        self.assertIn(str(self.root), self.watcher.roots)
        self.assertEqual(stat.S_IMODE((self.root / "workspace.lock").stat().st_mode), 0o600)
        contender = worker.lock_file(self.root, "workspace.lock")
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.watcher.command({"type": "forget", "root": str(self.root)})
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.source[1]["closed"] = True
            self.save()
            self.watcher.scan()
            self.watcher.command({"type": "forget", "root": str(self.root)})
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)

    def test_gone_retains_final_publish_permission_until_forget(self):
        self.watcher.scan()
        self.source[1]["closed"] = True
        self.save()
        self.watcher.scan()
        self.assertEqual(self.events[-1]["type"], "gone")
        self.assertFalse((self.root / "control.sock").exists())
        self.watcher.command({"type": "publish", "root": str(self.root), "name": "workspace.json", "data": {"hidden": True}})
        self.assertTrue(worker.read_json(self.root / "workspace.json")["hidden"])
        self.watcher.command({"type": "forget", "root": str(self.root)})
        with self.assertRaises(ValueError):
            self.watcher.command({"type": "publish", "root": str(self.root), "name": "view.json", "data": {}})

    def test_live_foreign_socket_never_unlinked(self):
        self.watcher.scan()
        path = self.root / "control.sock"
        inode = path.stat().st_ino
        other = worker.Watcher(HERE, emit=lambda value: None)
        self.addCleanup(other.close)
        other.scan()
        self.assertNotIn(str(self.root), other.roots)
        self.assertEqual(path.stat().st_ino, inode)

    def test_global_request_routes_only_explicit_identity_or_persisted_owned_block(self):
        self.watcher.scan()
        layout = {"v": 2, "controller": "lua", "ompPid": os.getpid(), "session": "session", "anchor": "anchor",
                  "sessionId": "main-session", "panel": "panel", "detail": "detail", "preview": "preview"}
        private_write(self.root / "workspace.json", layout)
        for block in ("anchor", "panel", "detail", "preview"):
            self.watcher.target_request({"type": "request", "session": "session", "anchor": block,
                                         "view": "panel", "request": {"type": "show"}}, None)
            self.assertEqual(self.events[-1]["root"], str(self.root))
        for session, block in (("wrong", "panel"), ("session", "focused-guess")):
            with self.assertRaises(ValueError):
                self.watcher.target_request({"type": "request", "session": session, "anchor": block,
                                             "view": "panel", "request": {"type": "hide"}}, None)
        layout["sessionId"] = "stale"
        private_write(self.root / "workspace.json", layout)
        with self.assertRaises(ValueError):
            self.watcher.target_request({"type": "request", "session": "session", "anchor": "panel",
                                         "view": "panel", "request": {"type": "show"}}, None)

    def test_startup_recovery_includes_private_layout(self):
        private_write(self.root / "workspace.json", {"v": 2, "controller": "lua", "panel": "previous"})
        self.watcher.scan()
        self.assertEqual(self.events[0]["layout"]["panel"], "previous")


class ProjectWorkerTests(Fixture):
    def test_large_real_diff_explicitly_marks_display_truncation(self):
        self.repository()
        (self.repo / "tracked").write_text("x" * 140000 + "\nUNSEEN-TAIL\n")
        slot = self.observe()
        complete = slot.project.diff()
        self.assertGreater(len(complete), 120000)
        response = self.action({"type": "diff"})
        self.assertEqual(response["type"], "diff")
        self.assertTrue(response["text"].startswith(complete[:120000]))
        self.assertTrue(response["text"][120000:].strip())
        self.assertNotIn("UNSEEN-TAIL", response["text"])

    def test_real_diff_and_staged_only_commit_are_visible_async_responses(self):
        self.repository()
        (self.repo / "tracked").write_text("staged\n")
        self.git("add", "tracked")
        (self.repo / "tracked").write_text("working\n")
        slot = self.observe()
        self.assertTrue(slot.snapshot["git"])
        self.assertEqual(slot.snapshot["branch"], "topic")
        self.assertEqual(slot.snapshot["changes"]["staged"], 1)
        response = self.action({"type": "diff"})
        self.assertEqual(response["type"], "diff")
        self.assertIn("+working", response["text"])
        response = self.action({"type": "commit", "message": "explicit message"})
        self.assertNotIn("error", response)
        self.assertEqual(self.git("show", "HEAD:tracked"), b"staged\n")
        self.assertEqual((self.repo / "tracked").read_text(), "working\n")
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), b"explicit message")

    def test_add_and_run_script_are_explicit_validated_and_survive_forget(self):
        slot = self.observe()
        marker = self.repo / "ran"
        command = "printf explicit > " + shlex.quote(str(marker))
        self.assertNotIn("error", self.action({"type": "add-script", "name": "Write", "command": command}))
        self.assertFalse(marker.exists())
        self.pump(lambda: bool(slot.snapshot.get("scripts")))
        identity = slot.snapshot["scripts"][0]["id"]
        with self.assertRaises(ValueError):
            self.watcher.command({"type": "project", "root": str(self.root), "request": {"type": "run-script", "id": "forged"}})
        self.assertNotIn("error", self.action({"type": "run-script", "id": identity}))
        self.pump(marker.exists)
        self.assertEqual(marker.read_text(), "explicit")
        self.watcher.command({"type": "forget", "root": str(self.root)})
        self.watcher.project_tick()
        self.assertEqual(marker.read_text(), "explicit")
        self.assertTrue(slot.project.config.exists())
        self.assertFalse((self.repo / "package.json").exists())

    def test_invalid_project_action_returns_error_without_worker_failure(self):
        self.observe()
        response = self.action({"type": "commit", "message": ""})
        self.assertTrue(response["error"])
        response = self.action({"type": "add-script", "name": "", "command": "true"})
        self.assertTrue(response["error"])
        self.watcher.command({"type": "publish", "root": str(self.root), "name": "view.json", "data": {"alive": True}})
        self.assertTrue(worker.read_json(self.root / "view.json")["alive"])

    def test_blocked_real_git_hook_does_not_block_transport_and_stale_generation_is_discarded(self):
        self.repository()
        (self.repo / "tracked").write_text("next\n")
        self.git("add", "tracked")
        entered, release = self.base / "entered", self.base / "release"
        self.addCleanup(lambda: release.touch())
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\n: > %s\nwhile [ ! -f %s ]; do sleep 0.02; done\n" %
                        (shlex.quote(str(entered)), shlex.quote(str(release))))
        hook.chmod(0o700)
        old = self.observe()
        self.events.clear()
        self.watcher.command({"type": "project", "root": str(self.root), "request": {"type": "commit", "message": "old generation"}})
        self.pump(entered.exists)
        before = time.monotonic()
        self.watcher.command({"type": "publish", "root": str(self.root), "name": "view.json", "data": {"responsive": True}})
        replacement = self.base / "other-repo"
        replacement.mkdir(mode=0o700)
        self.source[1]["main"]["cwd"] = str(replacement)
        self.source[1]["sessionId"] = "new-session"
        self.save()
        self.watcher.scan()
        self.watcher.command({"type": "observe", "root": str(self.root)})
        new = self.watcher.roots[str(self.root)]["project"]
        with mock.patch.object(new.project, "_servers", return_value=([], [])):
            self.pump(lambda: bool(new.snapshot))
        self.assertLess(time.monotonic() - before, 3)
        self.assertNotEqual(old.generation, new.generation)
        release.touch()
        self.pump(lambda: not self.watcher.retired)
        self.assertFalse(any(event["type"] == "response" and "old generation" in event["event"].get("text", "") for event in self.events))
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), b"old generation")

    @unittest.skipUnless(shutil.which("lsof"), "real listener discovery requires lsof")
    def test_real_script_listener_is_reported_and_observer_close_does_not_kill_it(self):
        self.watcher.scan()
        self.watcher.command({"type": "observe", "root": str(self.root)})
        slot = self.watcher.roots[str(self.root)]["project"]
        marker = self.repo / "listener.json"
        code = ("import os,socket,time,json; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                "open(%r,'w').write(json.dumps([os.getpid(),s.getsockname()[1]])); time.sleep(30)" % str(marker))
        with mock.patch.object(slot.project, "_supervisor_rows", return_value=([], None)):
            self.pump(lambda: bool(slot.snapshot))
            command = "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code)
            self.action({"type": "add-script", "name": "Listener", "command": command})
            self.pump(lambda: bool(slot.snapshot.get("scripts")))
            identity = slot.snapshot["scripts"][0]["id"]
            self.action({"type": "run-script", "id": identity})
            children = list(slot.project._children)
            self.addCleanup(lambda: [(child.terminate(), child.wait(timeout=3)) for child in children if child.poll() is None])
            self.pump(marker.exists)
            pid, port = json.loads(marker.read_text())
            slot.refresh_at = 0
            self.pump(lambda: any(row["port"] == port and row["pid"] == pid for row in slot.snapshot.get("servers", [])))
            row = next(row for row in slot.snapshot["servers"] if row["port"] == port)
            self.assertEqual(row["status"], "listening")
            self.assertEqual(row["url"], "http://127.0.0.1:%s" % port)
            self.watcher.command({"type": "forget", "root": str(self.root)})
            self.watcher.project_tick()
            os.kill(pid, 0)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass


class Daemon:
    def __init__(self, case):
        self.process = subprocess.Popen([sys.executable, str(HERE / "worker.py"), "watch", "--module", str(HERE)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=dict(os.environ))
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.events = []
        case.addCleanup(self.close)
        self.next("ready")

    def send(self, value):
        self.process.stdin.write(worker.encode(value))
        self.process.stdin.flush()

    def next(self, kind, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, event in enumerate(self.events):
                if event["type"] == kind:
                    return self.events.pop(index)
            for key, _ in self.selector.select(0.05):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    raise AssertionError("daemon exited: " + self.process.stderr.read().decode())
                self.buffer.extend(data)
                while b"\n" in self.buffer:
                    line, _, rest = self.buffer.partition(b"\n")
                    self.buffer = bytearray(rest)
                    self.events.append(json.loads(line))
        raise AssertionError("daemon event timeout: " + kind)

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdout.close()
        self.process.stderr.close()


class CliTests(Fixture):
    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, str(HERE / "worker.py"), *args], input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)

    def test_malformed_and_oversized_stdin_do_not_poison_following_commands(self):
        daemon = Daemon(self)
        daemon.next("source")
        daemon.process.stdin.write(b"not-json\n" + b"x" * (worker.FRAME_LIMIT + 1) + b"\n")
        daemon.process.stdin.flush()
        daemon.send({"type": "publish", "root": str(self.root), "name": "view.json", "data": {"recovered": True}})
        event = daemon.next("response")
        self.assertTrue(event["event"]["error"])
        deadline = time.monotonic() + 3
        while not (self.root / "view.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(worker.read_json(self.root / "view.json")["recovered"])

    def test_ready_singleton_duplicate_exits_without_ready_and_leaves_owner_alive(self):
        daemon = Daemon(self)
        self.assertEqual(daemon.next("source")["root"], str(self.root))
        result = self.cli("watch", "--module", str(HERE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIsNone(daemon.process.poll())
        state = worker.state_directory(HERE)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((state / "watch.lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((state / "control.sock").stat().st_mode), 0o600)

    def test_request_ack_routes_root_and_global_session_without_waiting_for_ui(self):
        daemon = Daemon(self)
        daemon.next("source")
        result = self.cli("request", "--root", str(self.root), "--view", "panel", input=b'{"type":"select","key":"agent:1"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        event = daemon.next("request")
        self.assertEqual(event["request"]["key"], "agent:1")  # Worker deliberately makes no selection decision.
        result = self.cli("request", "--session", "session", "--anchor", "anchor", "--view", "panel", input=b'{"type":"hide"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(daemon.next("request")["request"]["type"], "hide")
        result = self.cli("request", "--session", "session", "--anchor", "unknown", "--view", "panel", input=b'{"type":"show"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"no unique workspace", result.stderr)

    def test_stop_requests_shutdown_allows_final_publish_then_waits_for_release(self):
        daemon = Daemon(self)
        daemon.next("source")
        stop = subprocess.Popen([sys.executable, str(HERE / "worker.py"), "stop", "--module", str(HERE)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: stop.communicate(timeout=8))
        daemon.next("shutdown")
        self.assertIsNone(stop.poll())
        daemon.send({"type": "publish", "root": str(self.root), "name": "workspace.json", "data": {"hidden": True}})
        daemon.send({"type": "stop"})
        stdout, stderr = stop.communicate(timeout=8)
        self.assertEqual(stop.returncode, 0, stderr)
        self.assertTrue(worker.read_json(self.root / "workspace.json")["hidden"])
        self.assertEqual(daemon.process.wait(timeout=5), 0)
        self.assertFalse((self.root / "control.sock").exists())
        self.assertFalse((worker.state_directory(HERE) / "control.sock").exists())
        self.assertTrue(worker.alive(os.getpid()))

    def test_stdin_eof_releases_transport_without_marking_source_closed(self):
        daemon = Daemon(self)
        daemon.next("source")
        daemon.process.stdin.close()
        self.assertEqual(daemon.process.wait(timeout=5), 0)
        self.assertFalse(worker.read_json(self.root / "root.json")["closed"])
        self.assertFalse((self.root / "control.sock").exists())

    def test_stop_absent_is_noop_and_unsafe_state_is_not_chmodded(self):
        # Fixture owns another state directory; this CLI has never started.
        state = self.base / "not-started"
        with mock.patch.dict(os.environ, REX_WORKSPACE_STATE_DIR=str(state)):
            self.assertEqual(self.cli("stop", "--module", str(HERE)).returncode, 0)
            self.assertFalse(state.exists())
            state.mkdir(mode=0o755)
            result = self.cli("stop", "--module", str(HERE))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)

    def test_start_preserves_private_singleton_lifecycle(self):
        # A tiny host-process fixture stands in for Rex's Lua process lifecycle,
        # but launches the real worker and completes the real shutdown handshake.
        executable = self.base / "rex-host"
        executable.write_text("#!%s\n" % sys.executable +
            "import json,subprocess,sys\n" +
            "p=subprocess.Popen([sys.executable,%r,'watch','--module',%r],stdin=subprocess.PIPE,stdout=subprocess.PIPE)\n" % (str(HERE / "worker.py"), str(HERE)) +
            "for line in p.stdout:\n" +
            " event=json.loads(line)\n" +
            " if event.get('type')=='shutdown':\n" +
            "  p.stdin.write(b'{\\\"type\\\":\\\"stop\\\"}\\n'); p.stdin.flush()\n" +
            "p.wait()\n")
        executable.chmod(0o700)
        self.addCleanup(lambda: self.cli("stop", "--module", str(HERE)))
        result = self.cli("start", "--module", str(HERE), "--rex", str(executable))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE((worker.state_directory(HERE) / "watcher.log").stat().st_mode), 0o600)
        self.assertEqual(self.cli("start", "--module", str(HERE), "--rex", "/definitely/missing").returncode, 0)
        self.assertEqual(self.cli("stop", "--module", str(HERE)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
