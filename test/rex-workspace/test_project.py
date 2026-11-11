"""Isolated project behavior tests; no network, real OMP, or business server.

Run after integration: python3 -m unittest discover -s test/rex-workspace -p 'test_project.py'
"""

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("rex_workspace_project", Path(__file__).resolve().parents[2] / "packages/coding-agent/rex-workspace/project.py")
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update({
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Project test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Project test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        })
        self.environment = mock.patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.state = project.ProjectState(str(self.repo), os.getpid(), str(self.base / "state"))
        self.addCleanup(self.state.close)
        self.observers = mock.patch.object(self.state, "_servers", return_value=([], []))
        self.observers.start()
        self.addCleanup(self.observers.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, OMP_CONFIG_SYNC="0")).stdout

    def repository(self):
        self.git("init", "--initial-branch=topic")
        (self.repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "-m", "initial")

    def test_non_repository_does_not_invent_branch(self):
        snapshot = self.state.refresh()
        self.assertFalse(snapshot["git"])
        self.assertEqual(snapshot["branch"], "")
        self.assertEqual(snapshot["head"], "")
        self.assertNotIn("error", snapshot)

    def test_head_counts_staging_rename_binary_and_untracked(self):
        self.repository()
        self.git("mv", "tracked.txt", "renamed\tfile.txt")
        (self.repo / "renamed\tfile.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (self.repo / "new.txt").write_bytes(b"a\nb")
        (self.repo / "binary.dat").write_bytes(b"\x00binary\xff")
        (self.repo / "link").symlink_to("new.txt")
        snapshot = self.state.refresh()
        changes = snapshot["changes"]
        self.assertEqual(snapshot["branch"], "topic")
        self.assertEqual(changes["reference"], "HEAD")
        self.assertEqual((changes["added"], changes["removed"], changes["files"], changes["staged"]), (4, 0, 4, 1))
        entries = {entry["path"]: entry for entry in changes["entries"]}
        self.assertEqual(entries["renamed\tfile.txt"]["oldPath"], "tracked.txt")
        self.assertTrue(entries["binary.dat"]["binary"])
        self.assertEqual(entries["binary.dat"]["added"], 0)
        self.assertEqual(entries["link"]["added"], 1)
        self.assertEqual(entries["new.txt"]["added"], 2)

    def test_staged_change_cancelled_in_worktree_is_not_head_change(self):
        self.repository()
        path = self.repo / "tracked.txt"
        path.write_text("staged\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        path.write_text("one\ntwo\n", encoding="utf-8")
        changes = self.state.refresh()["changes"]
        self.assertEqual(changes["files"], 0)
        self.assertEqual(changes["added"], 0)
        self.assertEqual(changes["staged"], 1)

    def test_staged_deletion_with_untracked_replacement_preserves_both(self):
        self.repository()
        self.git("rm", "tracked.txt")
        (self.repo / "tracked.txt").write_text("replacement\n", encoding="utf-8")
        changes = self.state.refresh()["changes"]
        self.assertEqual(changes["files"], 1)
        self.assertEqual(changes["staged"], 1)
        self.assertEqual(changes["added"], 1)
        self.assertEqual(changes["removed"], 2)
        self.assertTrue(any(entry["untracked"] for entry in changes["entries"]))

    def test_unborn_branch_uses_empty_tree_and_explains_reference(self):
        self.git("init", "--initial-branch=new-project")
        (self.repo / "first.txt").write_text("first\n", encoding="utf-8")
        self.git("add", "first.txt")
        snapshot = self.state.refresh()
        self.assertEqual(snapshot["branch"], "new-project")
        self.assertEqual(snapshot["head"], "")
        self.assertFalse(snapshot["changes"]["headExists"])
        self.assertEqual(snapshot["changes"]["added"], 1)
        self.assertIn("HEAD 尚不存在", snapshot["error"])
        self.assertIn("first", self.state.diff())

    def test_diff_is_head_based_and_identifies_untracked_without_reading_contents(self):
        self.repository()
        (self.repo / "tracked.txt").write_text("one\nchanged\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("DO_NOT_EXPAND_PRIVATE_CONTENT", encoding="utf-8")
        text = self.state.diff()
        self.assertIn("HEAD", text)
        self.assertIn("+changed", text)
        self.assertIn("untracked.txt", text)
        self.assertNotIn("DO_NOT_EXPAND_PRIVATE_CONTENT", text)

    def test_custom_command_is_private_persistent_and_cannot_bypass_snapshot(self):
        self.state.add_script("Print", "printf '%s' \"$OMP_CONFIG_SYNC\"")
        self.assertFalse((self.repo / "package.json").exists())
        self.assertEqual(stat.S_IMODE(self.state.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.config.parent.stat().st_mode), 0o700)
        with self.assertRaises(project.ProjectError):
            self.state.run_script("/bin/sh -c anything")
        snapshot = self.state.refresh()
        script = snapshot["scripts"][0]
        result = self.state.run_script(script["id"])
        self.state._children[-1].wait(timeout=5)
        logs = list(self.state.state_dir.glob("script-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].read_text(), "0")
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        self.assertIn("PID", result)
        another = project.ProjectState(str(self.repo), os.getpid(), str(self.base / "other-state"))
        self.addCleanup(another.close)
        self.assertEqual(another._custom_scripts(), [{"name": "Print", "command": "printf '%s' \"$OMP_CONFIG_SYNC\""}])

    def test_lockfile_selects_runner_and_changed_command_invalidates_id(self):
        package = self.repo / "package.json"
        package.write_text(json.dumps({"scripts": {"dev": "original"}}), encoding="utf-8")
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        first = self.state.refresh()["scripts"][0]
        self.assertEqual(self.state._scripts[first["id"]]["_argv"], ["pnpm", "run", "dev"])
        package.write_text(json.dumps({"scripts": {"dev": "changed"}}), encoding="utf-8")
        with mock.patch.object(project.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(project.ProjectError, "已变化"):
                self.state.run_script(first["id"])
            launch.assert_not_called()
        (self.repo / "yarn.lock").write_text("", encoding="utf-8")
        failed = self.state.refresh()
        self.assertEqual(failed["scripts"], [])
        self.assertIn("lockfile", failed["error"])

    def test_commit_includes_only_index_and_retains_hook_failures(self):
        self.repository()
        path = self.repo / "tracked.txt"
        path.write_text("staged\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        path.write_text("unstaged\n", encoding="utf-8")
        hooks = self.repo / ".git" / "hooks"
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook rejection' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o700)
        with self.assertRaisesRegex(project.ProjectError, "hook rejection"):
            self.state.commit("explicit user message")
        self.assertEqual(self.git("show", "HEAD:tracked.txt"), b"one\ntwo\n")
        hook.unlink()
        self.state.commit("explicit user message")
        self.assertEqual(self.git("show", "HEAD:tracked.txt"), b"staged\n")
        self.assertEqual(path.read_text(), "unstaged\n")
        with self.assertRaisesRegex(project.ProjectError, "没有已暂存"):
            self.state.commit("no changes")

    def test_close_never_signals_live_script(self):
        self.state.add_script("Wait", "sleep 30")
        key = self.state.refresh()["scripts"][0]["id"]
        self.state.run_script(key)
        child = self.state._children[-1]
        try:
            self.state.close()
            self.assertIsNone(child.poll())
            with self.assertRaisesRegex(project.ProjectError, "已关闭"):
                self.state.run_script(key)
        finally:
            import signal
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)

    def test_collection_timeout_is_an_error_not_empty_success(self):
        with mock.patch.object(project.subprocess, "run", side_effect=subprocess.TimeoutExpired(["git"], 4)):
            snapshot = self.state.refresh()
        self.assertIn("超时", snapshot["error"])
        self.assertTrue(snapshot["changes"]["incomplete"])


class ServerOwnershipTests(unittest.TestCase):
    def test_absolute_omp_executable_works_without_omp_on_server_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / "custom bin" / "omp"
            binary.parent.mkdir()
            reports = [{"kind": "project", "projectDir": str(root), "daemons": [
                {"pid": 200, "cwd": str(root), "state": "running", "name": "web"},
            ]}]
            profile = root / "profile"
            profile.mkdir()
            (profile / "services.json").write_text(json.dumps(reports))
            wrong = root / "other-profile"
            wrong.mkdir()
            (wrong / "services.json").write_text("[]")
            binary.write_text('#!/bin/sh\n/bin/cat "$PI_CODING_AGENT_DIR/services.json"\n')
            binary.chmod(0o700)
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(wrong)}):
                state = project.ProjectState(str(root), 100, root / "state",
                                             omp_executable=str(binary), command_path="/usr/bin:/bin",
                                             agent_dir=str(profile))
            self.addCleanup(state.close)
            services, error = state._supervisor_rows()
            self.assertIsNone(error)
            self.assertEqual([row["pid"] for row in services], [200])

    def test_supervisor_excludes_global_and_foreign_scopes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = str(Path(temporary).resolve())
            state = project.ProjectState(root, 100, str(Path(root) / "state"))
            self.addCleanup(state.close)
            rows = [
                {"kind": "global", "daemons": [{"pid": 900, "cwd": root, "state": "running"}]},
                {"kind": "project", "projectDir": "/elsewhere", "daemons": [{"pid": 901, "cwd": root, "state": "running"}]},
                {"kind": "project", "projectDir": root, "daemons": [
                    {"pid": 200, "cwd": root, "state": "running", "name": "web"},
                    {"pid": 201, "cwd": "/elsewhere", "state": "running"},
                    {"pid": 202, "cwd": root, "state": "exited"},
                ]},
            ]
            with mock.patch.object(state, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(rows).encode(), b"")):
                services, error = state._supervisor_rows()
            self.assertIsNone(error)
            self.assertEqual([row["pid"] for row in services], [200])

    def test_listener_requires_both_owned_ancestry_and_project_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = str(Path(temporary).resolve())
            state = project.ProjectState(root, 100, str(Path(root) / "state"))
            self.addCleanup(state.close)
            process_table = (
                "100 1 Mon Nov 10 12:00:00 2026 /bin/omp\n"
                "101 100 Mon Nov 10 12:00:01 2026 /bin/node\n"
                "102 100 Mon Nov 10 12:00:02 2026 /bin/foreign\n"
                "200 1 Mon Nov 10 12:00:03 2026 /bin/service\n"
                "999 1 Mon Nov 10 12:00:04 2026 /bin/unrelated\n"
            ).encode()
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                if args[0] == "ps":
                    return subprocess.CompletedProcess(args, 0, process_table, b"")
                pids = set(args[args.index("-p") + 1].split(","))
                self.assertNotIn("999", pids)
                if "-d" in args:
                    output = "p101\nfcwd\nn%s\np102\nfcwd\nn/elsewhere\np200\nfcwd\nn%s\n" % (root, root)
                else:
                    self.assertEqual(pids, {"101", "200"})
                    output = "p101\nn*:3000\nTST=LISTEN\np200\nn[::1]:4000\nTST=LISTEN\n"
                return subprocess.CompletedProcess(args, 0, output.encode(), b"")

            with mock.patch.object(state, "_supervisor_rows", return_value=([{"pid": 200, "name": "web"}], None)), mock.patch.object(state, "_run", side_effect=command):
                servers, errors = state._servers()
            self.assertEqual(errors, [])
            self.assertEqual({row["pid"] for row in servers}, {101, 200})
            self.assertEqual({row["url"] for row in servers}, {"http://127.0.0.1:3000", "http://[::1]:4000"})
            self.assertEqual({row["status"] for row in servers}, {"listening"})
            self.assertNotIn(102, state._owned_pids)


if __name__ == "__main__":
    unittest.main()
