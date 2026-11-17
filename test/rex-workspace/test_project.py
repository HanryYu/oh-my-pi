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
import threading
import unittest
from unittest import mock
from contextlib import contextmanager


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

    def curate(self, candidates, state=None):
        state = state or self.state
        revision = state.script_snapshot()["project"]["revision"]
        return state.set_scripts([{"name": row["label"], "command": row["command"], "cwd": row["cwd"]}
                                  for row in candidates], revision)

    def observer(self, cwd, name="another"):
        state = project.ProjectState(str(cwd), os.getpid(), self.base / name)
        self.addCleanup(state.close)
        return state

    def worktree(self):
        path = self.base / "other checkout"
        self.git("worktree", "add", "-b", "other", str(path))
        return path

    @contextmanager
    def script_launcher(self, child=None, error=None):
        # subprocess.run uses Popen too: leave real read-only Git inspection
        # intact while observing only explicit project command launches.
        original = project.subprocess.Popen
        launch = mock.Mock(return_value=child, side_effect=error)

        def dispatch(args, *positional, **kwargs):
            if args[0] == "git":
                return original(args, *positional, **kwargs)
            return launch(args, *positional, **kwargs)

        with mock.patch.object(project.subprocess, "Popen", side_effect=dispatch):
            yield launch

    def test_curated_command_is_private_persistent_and_cannot_bypass_snapshot(self):
        saved = self.state.set_scripts([{"name": "Print", "command": "printf '%s' \"$OMP_CONFIG_SYNC\""}], 0)
        self.assertEqual(saved["project"]["revision"], 1)
        self.assertFalse((self.repo / "package.json").exists())
        self.assertEqual(stat.S_IMODE(self.state.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.config.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state.config.with_suffix(".lock").stat().st_mode), 0o600)
        with self.assertRaises(project.ProjectError):
            self.state.run_script("/bin/sh -c anything")
        script = saved["scripts"][0]
        result = self.state.run_script(script["id"])
        self.state._children[-1].wait(timeout=5)
        logs = list(self.state.state_dir.glob("script-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].read_text(), "0")
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        self.assertIn("PID", result)
        another = self.observer(self.repo)
        self.assertEqual(another.script_snapshot()["scripts"], saved["scripts"])
        self.assertEqual(json.loads(self.state.config.read_text()), {
            "v": 2, "project": saved["project"]["key"], "revision": 1,
            "scripts": [{"name": "Print", "command": "printf '%s' \"$OMP_CONFIG_SYNC\"", "cwd": "."}]})

    def test_candidates_never_automatically_enter_panel_or_run(self):
        (self.repo / "package.json").write_text('{"scripts":{"build":"true","dev":"true"}}')
        (self.repo / "compile.sh").write_text("touch should-not-exist\n")
        with self.script_launcher() as launch:
            snapshot = self.state.candidate_snapshot()
            self.assertEqual(snapshot["scripts"], [])
            self.assertEqual({row["label"] for row in snapshot["candidates"]}, {"build", "dev", "compile.sh"})
            for candidate in snapshot["candidates"]:
                with self.assertRaises(project.ProjectError):
                    self.state.run_script(candidate["id"])
            launch.assert_not_called()
        self.assertEqual(self.state.script_snapshot()["scripts"], [])
        self.assertFalse(self.state.config.exists())
        self.assertFalse((self.repo / "should-not-exist").exists())
        self.curate([snapshot["candidates"][0]])
        self.assertEqual(len(self.state.script_snapshot()["scripts"]), 1)

    def test_worktrees_share_config_but_execute_only_in_current_checkout(self):
        self.repository()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "build.sh").write_text('printf "%s" "$PWD" > proof\n')
        self.git("add", "scripts")
        self.git("commit", "-m", "add script")
        other = self.worktree()
        second = self.observer(other)
        original = self.state.candidate_snapshot()
        saved = self.curate(original["candidates"])
        shared = second.script_snapshot()
        self.assertEqual(saved["project"]["key"], shared["project"]["key"])
        self.assertEqual(saved["project"]["config"], shared["project"]["config"])
        self.assertEqual(shared["project"]["root"], str(other))
        self.assertNotIn(str(self.repo), self.state.config.read_text())
        self.assertEqual(shared["scripts"][0]["executionCwd"], str(other))
        second.run_script(shared["scripts"][0]["id"])
        second._children[-1].wait(timeout=5)
        self.assertEqual((other / "proof").read_text(), str(other))
        self.assertFalse((self.repo / "proof").exists())
        second.set_scripts([{"name": "Current", "command": '/bin/pwd > current'}], 1)
        self.assertEqual(self.state.script_snapshot()["scripts"][0]["label"], "Current")
        self.assertEqual(self.state.script_snapshot()["project"]["revision"], 2)

    def test_subdirectories_and_symlink_checkout_alias_share_git_identity(self):
        self.repository()
        nested = self.repo / "packages" / "app"
        nested.mkdir(parents=True)
        (nested / "package.json").write_text('{"scripts":{"build":"true"}}')
        self.git("add", "packages")
        self.git("commit", "-m", "nested")
        other = self.worktree()
        alias = self.base / "alias"
        alias.symlink_to(other, target_is_directory=True)
        source = self.observer(nested, "nested-state")
        target = self.observer(alias / "packages" / "app", "target-state")
        saved = source.set_scripts([{"name": "Here", "command": "/bin/pwd > proof", "cwd": "packages/app"}], 0)
        self.assertEqual(saved["project"]["key"], target.script_snapshot()["project"]["key"])
        script = target.script_snapshot()["scripts"][0]
        self.assertEqual(script["cwd"], "packages/app")
        target.run_script(script["id"])
        target._children[-1].wait(timeout=5)
        self.assertEqual((other / "packages/app/proof").read_text().strip(), str(other / "packages/app"))
        self.assertFalse((nested / "proof").exists())

    def test_missing_current_checkout_cwd_never_falls_back_to_other_worktree(self):
        self.repository()
        other = self.worktree()
        nested = self.repo / "only-here"
        nested.mkdir()
        self.state.set_scripts([{"name": "Here", "command": "touch proof", "cwd": "only-here"}], 0)
        target = self.observer(other)
        with self.script_launcher() as launch:
            snapshot = target.script_snapshot()
            self.assertEqual(snapshot["scripts"], [])
            self.assertTrue(any("当前 checkout" in error for error in snapshot["errors"]))
            launch.assert_not_called()
        self.assertFalse((nested / "proof").exists())

    def test_concurrent_worktree_writers_use_one_revision_lock(self):
        self.repository()
        second = self.observer(self.worktree())
        barrier = threading.Barrier(2)
        results, failures = [], []

        def save(state, name):
            barrier.wait(timeout=5)
            try:
                results.append(state.set_scripts([{"name": name, "command": "/bin/true"}], 0))
            except project.ProjectError as error:
                failures.append(str(error))

        threads = [threading.Thread(target=save, args=(state, name))
                   for state, name in ((self.state, "First"), (second, "Second"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("revision", failures[0])
        snapshot = self.state.script_snapshot()
        self.assertEqual(snapshot["project"]["revision"], 1)
        self.assertEqual(second.script_snapshot()["scripts"][0]["label"], snapshot["scripts"][0]["label"])

    def test_set_replaces_all_rows_and_invalidates_even_identical_revision_ids(self):
        first = self.state.set_scripts([{"name": "Task", "command": "true"}], 0)["scripts"][0]
        second = self.state.set_scripts([{"name": "Task", "command": "true"}], 1)["scripts"][0]
        self.assertNotEqual(first["id"], second["id"])
        with self.script_launcher() as launch:
            with self.assertRaises(project.ProjectError):
                self.state.run_script(first["id"])
            launch.assert_not_called()
        with self.assertRaisesRegex(project.ProjectError, "revision"):
            self.state.set_scripts([], 1)
        cleared = self.state.set_scripts([], 2)
        self.assertEqual(cleared["scripts"], [])
        self.assertEqual(cleared["project"]["revision"], 3)

    def test_cwd_escape_absolute_paths_limits_and_other_checkout_commands_are_rejected(self):
        self.repository()
        other = self.worktree()
        outside = self.base / "outside"
        outside.mkdir()
        (self.repo / "escape").symlink_to(outside, target_is_directory=True)
        for cwd in (str(self.repo), "..", "../outside", "a/../../outside", "escape", "missing"):
            with self.subTest(cwd=cwd), self.assertRaises(project.ProjectError):
                self.state.set_scripts([{"name": "Task", "command": "true", "cwd": cwd}], 0)
        for command in ("/bin/sh " + str(other / "build.sh"),
                        "cd '%s' && /bin/true" % other,
                        "ROOT='%s' /bin/true" % self.repo,
                        "/bin/sh '../other checkout/build.sh'"):
            with self.subTest(command=command), self.assertRaises(project.ProjectError):
                self.state.set_scripts([{"name": "Task", "command": command}], 0)
        with self.assertRaises(project.ProjectError):
            self.state.set_scripts([{"name": str(i), "command": "true"} for i in range(7)], 0)
        with self.assertRaises(project.ProjectError):
            self.state.set_scripts([{"name": "Same", "command": "true"}, {"name": "Same", "command": "false"}], 0)
        for revision in (-1, True, "0"):
            with self.assertRaises(project.ProjectError):
                self.state.set_scripts([], revision)
        saved = self.state.set_scripts([{"name": "System tool", "command": "/usr/bin/env /bin/true"}], 0)
        self.assertEqual(len(saved["scripts"]), 1)

    def test_non_git_projects_have_separate_private_configuration(self):
        other = self.base / "separate"
        other.mkdir()
        second = self.observer(other)
        self.state.set_scripts([{"name": "Only first", "command": "true"}], 0)
        snapshot = second.script_snapshot()
        self.assertEqual(snapshot["scripts"], [])
        self.assertNotEqual(snapshot["project"]["key"], self.state.script_snapshot()["project"]["key"])

    def test_legacy_worktree_and_nested_commands_are_candidates_without_mutating_files(self):
        self.repository()
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "tracked").write_text("nested")
        self.git("add", "nested")
        self.git("commit", "-m", "nested")
        other = self.worktree()
        directory = self.state._config_directory
        directory.mkdir(parents=True)
        paths = []
        for cwd in (self.repo, other / "nested"):
            path = directory / (project._identity(str(cwd)) + ".json")
            path.write_text(json.dumps({"v": 1, "cwd": str(cwd), "scripts": [
                {"name": "Old " + cwd.name, "command": "/bin/sh '%s/old.sh'" % cwd}]}))
            paths.append((path, path.read_bytes()))
        foreign = directory / "foreign.json"
        foreign.write_text(json.dumps({"v": 1, "cwd": str(self.base / "unrelated"), "scripts": [
            {"name": "Foreign", "command": "false"}]}))
        snapshot = self.state.candidate_snapshot()
        self.assertEqual(snapshot["scripts"], [])
        self.assertEqual({row["cwd"] for row in snapshot["candidates"]}, {".", "nested"})
        self.assertTrue(all(row["source"] == "legacy" and row["reviewRequired"] for row in snapshot["candidates"]))
        for candidate in snapshot["candidates"]:
            with self.assertRaises(project.ProjectError):
                self.state.run_script(candidate["id"])
        self.state.set_scripts([{"name": "Reviewed", "command": "/bin/true"}], 0)
        for path, original in paths:
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.state.candidate_snapshot()["candidates"]), 2)

    def test_package_runner_and_manifest_or_wrapped_script_changes_invalidate_ids(self):
        directory = self.repo / "scripts"
        directory.mkdir()
        path = directory / "build.sh"
        path.write_text("printf original\n")
        package = self.repo / "package.json"
        package.write_text(json.dumps({"scripts": {"build": "sh scripts/build.sh"}}))
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        candidate = self.state.candidate_snapshot()["candidates"][0]
        self.assertEqual(candidate["command"], "pnpm run build")
        first = self.curate([candidate])["scripts"][0]
        self.assertEqual(self.state._scripts[first["id"]]["_argv"], ["pnpm", "run", "build"])
        path.write_text("printf changed\n")
        with self.script_launcher() as launch:
            with self.assertRaisesRegex(project.ProjectError, "已变化"):
                self.state.run_script(first["id"])
            launch.assert_not_called()
        second = self.state.script_snapshot()["scripts"][0]
        package.write_text(json.dumps({"scripts": {"build": "printf other"}}))
        with self.assertRaisesRegex(project.ProjectError, "已变化"):
            self.state.run_script(second["id"])
        (self.repo / "yarn.lock").write_text("")
        failed = self.state.candidate_snapshot()
        self.assertTrue(any("lockfile" in error for error in failed["errors"]))

    def test_confirmation_keeps_complete_command_cwd_and_never_env_secrets(self):
        self.repository()
        nested = self.repo / "nested"
        nested.mkdir()
        command = "printf '%s' '" + "x" * 12000 + "\x1b" + "tail'"
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/launch.json").write_text(json.dumps({
            "configurations": [{"name": "service", "program": "/bin/true",
                                "cwd": "nested", "env": {"TOKEN": "DO_NOT_EXPOSE_ENV_SECRET"}}]}))
        saved = self.state.set_scripts([{"name": "Long", "command": command, "cwd": "nested"}], 0)
        self.assertEqual(saved["scripts"][0]["command"], command.replace("\x1b", "�"))
        self.assertEqual(saved["scripts"][0]["cwd"], "nested")
        self.assertEqual(saved["scripts"][0]["executionCwd"], str(nested))
        self.assertNotIn("DO_NOT_EXPOSE_ENV_SECRET", json.dumps(self.state.candidate_snapshot()))

    def test_directory_scripts_are_read_only_candidates_and_content_changes_invalidate_ids(self):
        directory = self.repo / "scripts"
        directory.mkdir()
        path = directory / "build app.sh"
        path.write_text('#!/bin/bash\n[[ -d scripts ]] || exit 7\nprintf "%s" "$PWD" > compile-proof\n')
        with self.script_launcher() as launch:
            snapshot = self.state.candidate_snapshot()
            launch.assert_not_called()
        self.assertFalse((self.repo / "compile-proof").exists())
        candidate = snapshot["candidates"][0]
        self.assertEqual(candidate["label"], "scripts/build app.sh")
        self.assertEqual(candidate["kind"], "build")
        script = self.curate([candidate])["scripts"][0]
        self.state.run_script(script["id"])
        self.state._children[-1].wait(timeout=5)
        self.assertEqual((self.repo / "compile-proof").read_text(), str(self.repo))
        path.write_text("#!/bin/bash\nprintf changed > should-not-run\n")
        with self.script_launcher() as launch:
            with self.assertRaisesRegex(project.ProjectError, "已变化"):
                self.state.run_script(script["id"])
            launch.assert_not_called()
        updated = self.state.script_snapshot()["scripts"][0]
        self.assertNotEqual(updated["id"], script["id"])
        path.unlink()
        with self.assertRaisesRegex(project.ProjectError, "已变化"):
            self.state.run_script(updated["id"])
        self.assertEqual(self.state.candidate_snapshot()["candidates"], [])
        self.assertFalse((self.repo / "should-not-run").exists())

    def test_directory_scripts_preserve_shebang_flags_and_extensionless_executables(self):
        directory = self.repo / "tools"
        directory.mkdir()
        (directory / "build.py").write_text(
            "#!/usr/bin/env -S python3 -u\nimport sys\nfrom pathlib import Path\n"
            "Path('flags-proof').write_text(str(sys.stdout.write_through))\n")
        executable = directory / "compile-native"
        executable.write_text("#!/bin/sh\nprintf native > native-proof\n")
        executable.chmod(0o700)
        candidates = self.state.candidate_snapshot()["candidates"]
        for script in self.curate(candidates)["scripts"]:
            self.state.run_script(script["id"])
            self.assertEqual(self.state._children[-1].wait(timeout=5), 0)
        self.assertEqual((self.repo / "flags-proof").read_text(), "True")
        self.assertEqual((self.repo / "native-proof").read_text(), "native")

    def test_directory_discovery_stays_in_scope_and_ignores_dependencies_and_symlinks(self):
        self.repository()
        root_scripts = self.repo / "scripts"
        root_scripts.mkdir()
        (root_scripts / "build.sh").write_text("true\n")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "compile.sh").write_bytes(b"#!/bin/sh\n\xff")
        (root_scripts / "linked.sh").symlink_to(outside / "compile.sh")
        (self.repo / "tools").symlink_to(outside, target_is_directory=True)
        for name in ("node_modules", ".hidden", "one/two/three"):
            hidden = root_scripts / name
            hidden.mkdir(parents=True)
            (hidden / "build.sh").write_text("true\n")
        app = self.repo / "packages/app"
        (app / "src").mkdir(parents=True)
        (app / "package.json").write_text('{"scripts":{}}')
        (app / "scripts").mkdir()
        (app / "scripts/compile.sh").write_text("true\n")
        sibling = self.repo / "packages/other/scripts"
        sibling.mkdir(parents=True)
        (sibling / "build.sh").write_text("true\n")
        snapshot = self.observer(app / "src").candidate_snapshot()
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual({row["label"] for row in snapshot["candidates"]},
                         {"scripts/build.sh", "packages/app/scripts/compile.sh"})
        self.assertEqual(snapshot["scripts"], [])

    def test_configured_wrappers_remain_candidates_without_duplicate_direct_file(self):
        directory = self.repo / "scripts"
        directory.mkdir()
        (directory / "build app.sh").write_text("true\n")
        (directory / "check.py").write_text("pass\n")
        config = self.repo / "package.json"
        config.write_text(json.dumps({"scripts": {
            "build": "bash 'scripts/build app.sh' --mode release", "inspect": "cat scripts/check.py"}}))
        self.assertEqual({row["label"] for row in self.state.candidate_snapshot()["candidates"]},
                         {"build", "inspect", "scripts/check.py"})
        config.write_text('{"scripts":{}}')
        self.assertEqual({row["label"] for row in self.state.candidate_snapshot()["candidates"]},
                         {"scripts/build app.sh", "scripts/check.py"})

    def test_directory_scan_limit_is_reported_without_hiding_declared_candidates(self):
        (self.repo / "package.json").write_text('{"scripts":{"build":"echo configured"}}')
        for name in ("build.sh", "compile.sh", "check.sh"):
            (self.repo / name).write_text("true\n")
        with mock.patch.object(self.state, "SCRIPT_SCAN_LIMIT", 2):
            snapshot = self.state.candidate_snapshot()
        self.assertIn("build", {row["label"] for row in snapshot["candidates"]})
        self.assertTrue(any("扫描达到" in error for error in snapshot["errors"]))
        self.assertEqual(snapshot["scripts"], [])

    def test_static_taskfile_discovery_does_not_evaluate_project_code(self):
        marker = self.repo / "should-not-exist"
        (self.repo / "Makefile").write_text(
            "SIDE_EFFECT := $(shell touch %s)\n.PHONY: build\nbuild:\n\t@touch %s\n"
            "%%.o: %%.c\n\tcc $<\n" % (marker, marker))
        (self.repo / "justfile").write_text(
            "side_effect := `touch %s`\ndata := '''\nnot_a_recipe:\n'''\ncheck:\n    touch %s\n"
            "required value:\n    echo {{value}}\noptional value='hello world':\n    echo {{value}}\n"
            "many *values:\n    echo {{values}}\none +values:\n    echo {{values}}\n" % (marker, marker))
        with self.script_launcher() as launch:
            snapshot = self.state.candidate_snapshot()
            launch.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual({row["label"] for row in snapshot["candidates"]}, {"build", "check", "optional", "many"})
        self.assertEqual(snapshot["scripts"], [])
        self.assertEqual(snapshot["errors"], [])

    def test_launch_jsonc_candidate_retains_literal_args_cwd_and_private_env(self):
        directory = self.repo / "service"
        directory.mkdir()
        config = self.repo / ".claude/launch.json"
        config.parent.mkdir()
        config.write_text('''{
          // No discovery-time execution.
          "configurations": [{
            "name": "service", "runtimeExecutable": "/bin/sh", "runtimeArgs": ["-c",],
            "program": "printf '%s|%s|%s' \\"$PWD\\" \\"$MESSAGE\\" \\"$1\\"",
            "args": ["launch", "https://example.invalid/a,}"],
            "cwd": "${workspaceFolder}/service", "env": {"MESSAGE": "/* literal */",},
          },],
        }''')
        snapshot = self.state.candidate_snapshot()
        self.assertEqual(snapshot["errors"], [])
        script = self.curate(snapshot["candidates"])["scripts"][0]
        self.state.run_script(script["id"])
        self.state._children[-1].wait(timeout=5)
        finished = self.state.script_snapshot()["scripts"][0]
        self.assertEqual(finished["state"], "done")
        self.assertEqual(Path(finished["logFile"]).read_text(), str(directory) + "|/* literal */|https://example.invalid/a,}")
        data = project.ProjectState._read_json(config, jsonc=True)
        data["configurations"][0]["env"]["MESSAGE"] = "changed"
        config.write_text(json.dumps(data))
        with self.script_launcher() as launch:
            with self.assertRaisesRegex(project.ProjectError, "已变化"):
                self.state.run_script(script["id"])
            launch.assert_not_called()

    def test_launch_rejects_outside_cwd_and_unsupported_desktop_semantics(self):
        config = self.repo / ".claude/launch.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"configurations": [
            {"name": "escape", "program": "/bin/true", "cwd": str(self.base)},
            {"name": "port", "program": "/bin/true", "autoPort": True},
            {"name": "debug", "program": "/bin/true", "preLaunchTask": "build"}]}))
        snapshot = self.state.candidate_snapshot()
        self.assertEqual(snapshot["candidates"], [])
        self.assertEqual(len(snapshot["errors"]), 3)

    def test_nearest_launch_and_taskfile_edit_invalidate_execution_snapshot(self):
        self.repository()
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/launch.json").write_text(json.dumps({
            "configurations": [{"name": "ancestor", "program": "/bin/true"}]}))
        directory = self.repo / "nested"
        directory.mkdir()
        (directory / ".claude").mkdir()
        (directory / ".claude/launch.json").write_text(json.dumps({
            "configurations": [{"name": "nearest", "program": "/bin/true"}]}))
        taskfile = directory / "Makefile"
        taskfile.write_text("build:\n\ttrue\n")
        nested = self.observer(directory)
        candidates = nested.candidate_snapshot()["candidates"]
        self.assertEqual({row["label"] for row in candidates}, {"nearest", "build"})
        scripts = self.curate(candidates, nested)["scripts"]
        key = next(row["id"] for row in scripts if row["label"] == "build")
        taskfile.write_text("build:\n\tfalse\n")
        with self.script_launcher() as launch:
            with self.assertRaisesRegex(project.ProjectError, "已变化"):
                nested.run_script(key)
            launch.assert_not_called()

    def test_declared_package_manager_cannot_conflict_with_lockfile(self):
        (self.repo / "package.json").write_text(json.dumps({
            "packageManager": "pnpm@10.0.0", "scripts": {"build": "true"}}))
        (self.repo / "yarn.lock").write_text("")
        snapshot = self.state.candidate_snapshot()
        self.assertEqual(snapshot["candidates"], [])
        self.assertTrue(any("packageManager" in error and "lockfile" in error for error in snapshot["errors"]))

    def test_running_script_is_not_duplicated_and_exit_failure_is_not_healthy(self):
        self.state.set_scripts([{"name": "Task", "command": "unused fixture"}], 0)
        script = self.state.script_snapshot()["scripts"][0]
        child = mock.Mock(pid=12345)
        child.poll.return_value = None
        with self.script_launcher(child) as launch:
            self.state.run_script(script["id"])
            self.state.run_script(script["id"])
            self.assertEqual(launch.call_count, 1)
        running = self.state.script_snapshot()["scripts"][0]
        self.assertEqual(running["state"], "running")
        self.assertIsNone(running["exitCode"])
        self.assertTrue(Path(running["logFile"]).is_file())
        child.poll.return_value = 7
        failed = self.state.script_snapshot()["scripts"][0]
        self.assertEqual((failed["state"], failed["exitCode"]), ("failed", 7))
        another = self.observer(self.repo)
        self.assertNotIn("state", another.script_snapshot()["scripts"][0])
        with self.script_launcher(error=FileNotFoundError("missing runner")):
            with self.assertRaises(project.ProjectError):
                self.state.run_script(script["id"])
        failed_launch = self.state.script_snapshot()
        self.assertNotIn("state", failed_launch["scripts"][0])
        self.assertTrue(any("missing runner" in error for error in failed_launch["errors"]))
        self.assertTrue(any("missing runner" in path.read_text() for path in self.state.state_dir.glob("script-*.log")))

    def test_editor_absence_is_not_faked_and_discovery_does_not_open_applications(self):
        with mock.patch.object(project.shutil, "which", return_value=None), mock.patch.object(project.Path, "is_file", return_value=False), mock.patch.object(self.state, "_run") as run:
            self.assertIsNone(self.state.editor_info())
            with self.assertRaises(project.ProjectError):
                self.state.open_editor()
            run.assert_not_called()
        with mock.patch.object(project.shutil, "which", side_effect=lambda executable, **kwargs: "/tools/zed" if executable == "zed" else None), mock.patch.object(project.Path, "is_file", return_value=False), mock.patch.object(self.state, "_run") as run:
            self.assertEqual(self.state.editor_info(), {"label": "Zed", "available": True})
            run.assert_not_called()
            self.state.open_editor()
            run.assert_called_once_with(["/tools/zed", str(self.repo)])

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
        self.state.set_scripts([{"name": "Wait", "command": "sleep 30"}], 0)
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
