#!/usr/bin/env python3
"""Regression tests for Git integration and activation safety, not install copying.

Run: python3 scripts/install-tests/rex-update.py
The parent separately exercises a real isolated install with frozen dependencies,
prebuilt addon, readonly CLI and zero-model tests. These tests never use live Rex.
"""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("rex_fork_manager", ROOT / "scripts/rex.py")
rex = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rex)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rex-update-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.prefix = rex.private_directory(self.base / "private")
        rex.private_directory(self.prefix / "runtimes")
        self.locations = rex.installation_paths(self.prefix, self.base / "agent", self.base / "rex",
                                                self.base / "bin", sys.executable)
        self.config = self.base / "rex/init.lua"
        self.launcher = self.base / "bin/rex-omp"
        self.active = self.prefix / "active.json"
        self.old_bridge = self.base / "agent/extensions/rex-subagent-viewer"
        self.old_bridge.mkdir(mode=0o700, parents=True)
        (self.old_bridge / "user-note").write_bytes(b"user's extension content\x00")
        self.config.parent.mkdir(mode=0o700)
        self.config.write_bytes(b"-- unrelated user config\nlocal user_setting = true\n")
        self.config.chmod(0o640)
        # Only Git integration is fixture-sized. Feature/test files come from the
        # real repository; a conflict fails before dependency/model initialization.
        self.upstream = self.base / "upstream"
        self.upstream.mkdir(mode=0o700)
        rex.git(self.upstream, "init", "--initial-branch=main")
        self.packages(self.upstream, "1.0.0")
        (self.upstream / "shared.txt").write_text("base\n")
        self.commit(self.upstream, "initial upstream")
        self.base_commit = rex.git(self.upstream, "rev-parse", "HEAD")
        rex.git(self.upstream, "tag", "v1.0.0")
        self.fork = self.base / "fork"
        rex.git(None, "clone", "--no-hardlinks", self.upstream, self.fork)
        rex.git(self.fork, "checkout", "-b", "rex")
        shutil.copytree(ROOT / rex.RESOURCE, self.fork / rex.RESOURCE)
        feature = json.loads((ROOT / rex.MANIFEST).read_text())
        feature["initialBase"] = {"tag": "v1.0.0", "commit": self.base_commit}
        (self.fork / rex.MANIFEST).write_text(json.dumps(feature))
        for name in feature["requiredFeatures"] + list(rex.TESTS):
            target = self.fork / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
        self.commit(self.fork, "fork features")

    def commit(self, source, message):
        rex.git(source, "add", "--all")
        rex.git(source, "commit", "-m", message)

    def packages(self, source, version):
        for name in ("coding-agent", "natives"):
            destination = source / "packages" / name / "package.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({"name": name, "version": version}))
        (source / "package.json").write_text(json.dumps({"private": True}))

    def retained(self, label):
        """Activation fixture: a retained tree, not a claimed validated install."""
        runtime, commit = rex.checkout_candidate(self.prefix, self.fork, branch="rex")
        metadata = {"schema": 1, "id": runtime.name, "commit": commit, "version": "1.0.0",
                    "fork": {"remote": str(self.fork), "branch": "rex"}, "bun": sys.executable,
                    "upstream": {"remote": str(self.upstream), "tag": "v1.0.0", "commit": self.base_commit},
                    "testFixture": label}
        rex.json_write(runtime / "runtime.json", metadata)
        rex.write_bytes(runtime / "rex-omp", rex.launcher_bytes(self.prefix, runtime), 0o700)
        return runtime

    def install_old(self):
        old = self.retained("old")
        rex.activate(self.prefix, old, self.locations, observer=False)
        return old

    def snapshot(self):
        return self.active.read_bytes(), self.config.read_bytes(), self.launcher.read_bytes()

    def bump_upstream(self, conflicting=False):
        self.packages(self.upstream, "1.0.1")
        if conflicting:
            (self.upstream / "shared.txt").write_text("upstream changed this line\n")
        else:
            (self.upstream / "upstream-only.txt").write_text("official release\n")
        self.commit(self.upstream, "new official release")
        rex.git(self.upstream, "tag", "v1.0.1")

    def update(self):
        with redirect_stdout(io.StringIO()):
            return rex.main(["update", "--prefix", str(self.prefix), "--fork", str(self.fork),
                             "--upstream", str(self.upstream), "--upstream-version", "1.0.1"])

    def test_real_merge_conflict_leaves_active_config_and_developer_checkout_untouched(self):
        self.install_old()
        original = self.snapshot()
        (self.fork / "shared.txt").write_text("fork changed this line\n")
        self.commit(self.fork, "fork conflict")
        fork_head = rex.git(self.fork, "rev-parse", "HEAD")
        self.bump_upstream(conflicting=True)
        runtimes = set((self.prefix / "runtimes").iterdir())
        with self.assertRaises(rex.RexError):
            self.update()
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(rex.git(self.fork, "rev-parse", "HEAD"), fork_head)
        self.assertFalse((self.fork / ".git/MERGE_HEAD").exists())
        self.assertEqual(set((self.prefix / "runtimes").iterdir()), runtimes)

    def test_failed_toolchain_gate_does_not_activate_a_successful_git_merge(self):
        self.install_old()
        self.bump_upstream()
        original = self.snapshot()
        runtimes = set((self.prefix / "runtimes").iterdir())
        # Fault injection at the real download/toolchain boundary, not a fake
        # successful runtime or an echo command masquerading as Bun.
        with mock.patch.object(rex, "bun_runtime", side_effect=rex.RexError("download checksum mismatch")):
            with self.assertRaises(rex.RexError):
                self.update()
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(set((self.prefix / "runtimes").iterdir()), runtimes)

    def test_tag_package_mismatch_cannot_be_reported_as_a_followed_release(self):
        self.install_old()
        original = self.snapshot()
        rex.git(self.upstream, "tag", "v1.0.1")  # Actual package remains 1.0.0.
        with self.assertRaises(rex.RexError):
            self.update()
        self.assertEqual(self.snapshot(), original)

    def test_unsupported_future_bun_engine_fails_before_activation_or_download(self):
        self.install_old()
        self.bump_upstream()
        package = self.fork / "package.json"
        package.write_text(json.dumps({"private": True, "engines": {"bun": "^1.5.0"}}))
        self.commit(self.fork, "unsupported future engine")
        original = self.snapshot()
        with mock.patch.object(rex, "fetch_bytes", side_effect=AssertionError("must not guess a Bun version")):
            with self.assertRaises(rex.RexError):
                self.update()
        self.assertEqual(self.snapshot(), original)

    def test_real_git_integration_has_both_fork_and_tag_ancestors(self):
        self.bump_upstream()
        runtime, fork_commit = rex.checkout_candidate(self.prefix, self.fork, branch="rex")
        proof = rex.integrate_upstream(runtime / "source", str(self.upstream), "v1.0.1", self.base_commit)
        self.assertTrue(rex.ancestor(runtime / "source", proof["commit"], "HEAD"))
        self.assertTrue(rex.ancestor(runtime / "source", fork_commit, "HEAD"))
        self.assertNotEqual(rex.git(runtime / "source", "rev-parse", "HEAD"), fork_commit)
        self.assertEqual(rex.json_read(runtime / "source/packages/coding-agent/package.json")["version"], "1.0.1")
        self.assertFalse((self.fork / "upstream-only.txt").exists())

    def test_partial_clone_skips_deleted_blobs_without_losing_release_ancestry(self):
        historical = self.fork / "historical.bin"
        historical.write_bytes(b"retired release asset\n" * 50000)
        self.commit(self.fork, "historical asset")
        blob = rex.git(self.fork, "rev-parse", "HEAD:historical.bin")
        historical.unlink()
        self.commit(self.fork, "remove historical asset")
        rex.git(self.fork, "config", "uploadpack.allowFilter", "true")
        self.bump_upstream()
        home = self.base / "git-home"
        home.mkdir()
        origin = "https://rex-test.invalid/fork.git"
        with mock.patch.dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "config")):
            # A real filtered Git transport, without public network access.
            rex.git(None, "config", "--global", "url." + self.fork.as_uri() + ".insteadOf", origin)
            runtime, fork_commit = rex.checkout_candidate(self.prefix, origin, branch="rex")
            source = runtime / "source"
            objects = rex.git(source, "rev-list", "--objects", "--missing=print", "HEAD")
            self.assertIn("?" + blob, objects.splitlines())
            self.assertTrue(rex.ancestor(source, self.base_commit, fork_commit))
            proof = rex.integrate_upstream(source, str(self.upstream), "v1.0.1", self.base_commit)
            self.assertTrue(rex.ancestor(source, proof["commit"], "HEAD"))
            self.assertTrue(rex.ancestor(source, fork_commit, "HEAD"))
            self.assertEqual(rex.json_read(source / "packages/coding-agent/package.json")["version"], "1.0.1")

    def test_activation_and_rollback_keep_previous_runtime_and_open_files_intact(self):
        old = self.install_old()
        before = (old / "runtime.json").read_bytes()
        old_inode = (old / "source").stat().st_ino
        new = self.retained("new")
        with (old / "runtime.json").open("rb") as session_file:
            rex.activate(self.prefix, new, self.locations, observer=False)
            self.assertEqual(rex.load_state(self.prefix)["active"], new.name)
            self.assertEqual(rex.load_state(self.prefix)["previous"], old.name)
            self.assertEqual(session_file.read(), before)
            self.assertEqual((old / "source").stat().st_ino, old_inode)
            self.assertTrue((old / "rex-omp").is_file())
            # No live Rex in permanent tests; its orderly-stop behavior has
            # separate real worker transport tests in test/rex-workspace.
            with mock.patch.object(rex, "stop_observer"), mock.patch.object(rex, "reload_rex"):
                with redirect_stdout(io.StringIO()):
                    rex.main(["rollback", "--prefix", str(self.prefix)])
        self.assertEqual(rex.load_state(self.prefix)["active"], old.name)
        self.assertEqual(rex.load_state(self.prefix)["previous"], new.name)
        self.assertEqual((old / "runtime.json").read_bytes(), before)
        self.assertTrue((new / "runtime.json").is_file())
        self.assertIn(str(old / "source/scripts/rex.py"), self.launcher.read_text())

    def test_active_entry_moves_forward_but_existing_session_entry_stays_pinned(self):
        old = self.install_old()
        new = self.retained("new")
        rex.activate(self.prefix, new, self.locations, observer=False)
        profile = str(self.base / "caller-profile")
        with mock.patch.dict(os.environ, PI_CODING_AGENT_DIR=profile, OMP_CONFIG_SYNC="1"):
            for selector, expected in (("active", new), (old.name, old)):
                # Observe the real exec boundary without a fake Bun process.
                with mock.patch.object(rex.os, "execve", side_effect=OSError("exec boundary")) as execute:
                    with self.assertRaises(OSError):
                        rex.entry([str(self.prefix), selector, "--profile", "work", "--no-extensions"])
                _, argv, environment = execute.call_args.args
                self.assertEqual(argv[1], str(expected / "source" / rex.CLI))
                self.assertEqual(argv[2:], ["--profile", "work", "--no-extensions"])
                self.assertEqual(environment["REX_WORKSPACE_OMP"], str(expected / "rex-omp"))
                self.assertEqual(environment["PI_CODING_AGENT_DIR"], profile)
                self.assertEqual(environment["OMP_CONFIG_SYNC"], "1")

    def test_last_stage_failure_restores_config_entry_and_active_pointer(self):
        self.install_old()
        original = self.snapshot()
        new = self.retained("new")
        replace = os.replace
        def fail_state(source, destination):
            if Path(destination) == self.active and Path(source).name.startswith(".staging-state-"):
                raise OSError("injected atomic pointer failure")
            return replace(source, destination)
        with mock.patch.object(rex.os, "replace", side_effect=fail_state):
            with self.assertRaises(OSError):
                rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertEqual(self.snapshot(), original)
        self.assertTrue((new / "runtime.json").is_file())

    def test_synced_legacy_extension_is_never_moved_or_deleted(self):
        inode = self.old_bridge.stat().st_ino
        note = self.old_bridge / "user-note"
        content = note.read_bytes()
        old = self.install_old()
        new = self.retained("new")
        rex.activate(self.prefix, new, self.locations, observer=False)
        rex.activate(self.prefix, old, self.locations, observer=False)
        self.assertEqual(self.old_bridge.stat().st_ino, inode)
        self.assertEqual(note.read_bytes(), content)
        self.assertFalse((self.base / "agent/extension-backups").exists())

    def test_reload_failure_restores_active_and_reloads_original_configuration(self):
        self.install_old()
        original = self.snapshot()
        new = self.retained("new")
        with mock.patch.object(rex, "stop_observer") as stop:
            with mock.patch.object(rex, "reload_rex", side_effect=[OSError("reload failed"), None]) as reload:
                with self.assertRaises(OSError):
                    rex.activate(self.prefix, new, self.locations)
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(reload.call_count, 2)

    def test_foreign_launcher_symlink_is_rejected_without_touching_its_target(self):
        self.install_old()
        original = self.active.read_bytes(), self.config.read_bytes()
        foreign = self.base / "official-omp"
        foreign.write_bytes(b"official untouched\n")
        self.launcher.unlink()
        self.launcher.symlink_to(foreign)
        new = self.retained("new")
        with self.assertRaises(rex.RexError):
            rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertEqual((self.active.read_bytes(), self.config.read_bytes()), original)
        self.assertTrue(self.launcher.is_symlink())
        self.assertEqual(foreign.read_bytes(), b"official untouched\n")

    def test_unrelated_command_and_malformed_managed_config_are_not_overwritten(self):
        self.install_old()
        new = self.retained("new")
        self.launcher.write_text("#!/bin/sh\nexit 42\n")
        original = self.snapshot()
        with self.assertRaises(rex.RexError):
            rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertEqual(self.snapshot(), original)
        self.launcher.write_bytes(rex.launcher_bytes(self.prefix, new))
        self.config.write_bytes(b"user configuration" + rex.BEGIN)
        original = self.snapshot()
        with self.assertRaises(rex.RexError):
            rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertEqual(self.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
