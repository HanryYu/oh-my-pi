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
import plistlib
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
        self.app = self.prefix / rex.LINK_APP
        self.registration = mock.patch.object(rex, "register_link_app").start()
        self.addCleanup(mock.patch.stopall)
        original_run = rex.run
        def without_codesign(argv, **kwargs):
            if str(argv[0]) == "/usr/bin/codesign":
                return ""
            return original_run(argv, **kwargs)
        mock.patch.object(rex, "run", side_effect=without_codesign).start()
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
        executable = runtime / "path-link-handler"
        rex.write_bytes(executable, ("handler fixture " + label).encode(), 0o700)
        metadata["pathLinkHandler"] = {"path": executable.name,
                                      "sha256": rex.hashlib.sha256(executable.read_bytes()).hexdigest()}
        rex.json_write(runtime / "runtime.json", metadata)
        rex.write_bytes(runtime / "rex-omp", rex.launcher_bytes(self.prefix, runtime), 0o700)
        return runtime

    def install_old(self):
        old = self.retained("old")
        rex.activate(self.prefix, old, self.locations, observer=False)
        return old

    def snapshot(self):
        app = tuple(sorted((str(path.relative_to(self.app)), path.read_bytes(), path.stat().st_mode)
                           for path in self.app.rglob("*") if path.is_file())) if self.app.exists() else None
        return self.active.read_bytes(), self.config.read_bytes(), self.launcher.read_bytes(), app

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

    def test_swift_compile_failure_removes_candidate_before_activation(self):
        self.install_old()
        self.bump_upstream()
        original = self.snapshot()
        runtimes = set((self.prefix / "runtimes").iterdir())
        with mock.patch.object(rex, "bun_runtime", return_value=sys.executable), \
                mock.patch.object(rex, "compile_link_handler", side_effect=rex.RexError("Swift compiler failed")), \
                mock.patch.object(rex, "frozen_dependencies") as dependencies:
            with self.assertRaisesRegex(rex.RexError, "Swift compiler failed"):
                self.update()
        dependencies.assert_not_called()
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(set((self.prefix / "runtimes").iterdir()), runtimes)

    def test_compiler_checks_all_resources_and_requires_executable_output(self):
        runtime = self.retained("compile")
        (runtime / "path-link-handler").unlink()
        with mock.patch.object(rex, "run", return_value="") as compiler:
            with self.assertRaisesRegex(rex.RexError, "not produced"):
                rex.compile_link_handler(runtime)
        argv = compiler.call_args.args[0]
        self.assertEqual(argv[:4], ["/usr/bin/xcrun", "swiftc", "-O", "-framework"])
        self.assertEqual(argv[-2:], ["-o", runtime / "path-link-handler"])
        (runtime / "source" / rex.RESOURCE / "path_opener.py").unlink()
        with mock.patch.object(rex, "run") as compiler:
            with self.assertRaisesRegex(rex.RexError, "resource is missing"):
                rex.compile_link_handler(runtime)
        compiler.assert_not_called()

    def test_new_handler_integrity_failure_leaves_installation_untouched(self):
        self.install_old()
        original = self.snapshot()
        new = self.retained("corrupted")
        (new / "path-link-handler").write_bytes(b"changed")
        with self.assertRaisesRegex(rex.RexError, "integrity"):
            rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertEqual(self.snapshot(), original)

    def test_bundle_uses_only_our_scheme_and_an_immutable_launcher(self):
        runtime = self.install_old()
        info = plistlib.loads((self.app / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], rex.LINK_BUNDLE)
        self.assertEqual(info["CFBundleURLTypes"], [{"CFBundleURLName": rex.LINK_BUNDLE,
                                                    "CFBundleURLSchemes": ["rex-omp-file"]}])
        self.assertTrue(info["LSUIElement"])
        self.assertEqual(info["RexOMPLauncher"], str(runtime / "rex-omp"))
        self.registration.assert_called_once_with(self.app)
        module = Path(self.locations["rexDir"]) / rex.MODULE
        for name in ("path_links.py", "path_opener.py"):
            self.assertEqual((module / name).read_bytes(),
                             (runtime / "source" / rex.RESOURCE / name).read_bytes())

    def test_registration_failure_restores_prior_bundle_and_active_pointer(self):
        self.install_old()
        original = self.snapshot()
        new = self.retained("registration")
        self.registration.reset_mock()
        self.registration.side_effect = [rex.RexError("registration failed"), None]
        with self.assertRaisesRegex(rex.RexError, "registration failed"):
            rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(self.registration.call_args_list,
                         [mock.call(self.app), mock.call(self.app, unregister=False)])

    def test_first_registration_failure_removes_only_new_installation(self):
        new = self.retained("first")
        original = self.config.read_bytes()
        self.registration.side_effect = [rex.RexError("registration failed"), None]
        with self.assertRaisesRegex(rex.RexError, "registration failed"):
            rex.activate(self.prefix, new, self.locations, observer=False)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.active.exists())
        self.assertFalse(self.launcher.exists())
        self.assertEqual(self.config.read_bytes(), original)
        self.registration.assert_has_calls([mock.call(self.app), mock.call(self.app, unregister=True)])
        self.assertEqual((self.old_bridge / "user-note").read_bytes(), b"user's extension content\x00")

    def test_unrelated_bundle_is_never_replaced(self):
        self.install_old()
        info = self.app / "Contents/Info.plist"
        info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "unrelated.app"}))
        original = self.snapshot()
        with self.assertRaisesRegex(rex.RexError, "unrelated"):
            rex.activate(self.prefix, self.retained("new"), self.locations, observer=False)
        self.assertEqual(self.snapshot(), original)

    def test_legacy_rollback_keeps_receiver_pinned_to_new_runtime(self):
        legacy = self.retained("legacy")
        metadata = rex.json_read(legacy / "runtime.json")
        del metadata["pathLinkHandler"]
        (legacy / "runtime.json").unlink()
        rex.json_write(legacy / "runtime.json", metadata)
        (legacy / "path-link-handler").unlink()
        for name in ("path_links.py", "path_opener.py"):
            (legacy / "source" / rex.RESOURCE / name).unlink()
        rex.activate(self.prefix, legacy, self.locations, observer=False)
        self.assertFalse(self.app.exists())
        new = self.retained("new")
        rex.activate(self.prefix, new, self.locations, observer=False)
        info = (self.app / "Contents/Info.plist").read_bytes()
        handler = (self.app / "Contents/MacOS/RexPathLinkHandler").read_bytes()
        self.registration.reset_mock()
        rex.activate(self.prefix, legacy, self.locations, observer=False)
        self.assertEqual((self.app / "Contents/Info.plist").read_bytes(), info)
        self.assertEqual((self.app / "Contents/MacOS/RexPathLinkHandler").read_bytes(), handler)
        self.registration.assert_not_called()
        with mock.patch.object(rex.os, "execve", side_effect=OSError("exec boundary")) as execute:
            with self.assertRaises(OSError):
                rex.entry([str(self.prefix), new.name, "open-link", "rex-omp-file://source/tmp/file"])
        executable, argv, _ = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(argv, [sys.executable, "-B", str(new / "source" / rex.RESOURCE / "path_links.py"),
                                "--rex", self.locations["rexBinary"],
                                "--url", "rex-omp-file://source/tmp/file"])

    def test_entry_defaults_links_only_for_valid_rex_without_explicit_overrides(self):
        runtime = self.install_old()
        session = "11111111-2222-4333-8444-555555555555"
        block = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
        cases = [
            ({"REX_SESSION": "session:" + session, "REX_BLOCK": "block:" + block},
             {"PI_LINK_SCHEME": "rex-omp-file", "PI_LINK_HOST": session + "." + block.lower()}),
            ({}, {}),
            ({"REX_SESSION": "session:invalid", "REX_BLOCK": "block:" + block}, {}),
            ({"REX_SESSION": "session:" + session}, {}),
            ({"REX_SESSION": session, "REX_BLOCK": block, "PI_LINK_SCHEME": "custom"},
             {"PI_LINK_SCHEME": "custom"}),
            ({"REX_SESSION": session, "REX_BLOCK": block, "PI_LINK_HOST": ""},
             {"PI_LINK_HOST": ""}),
        ]
        for values, expected in cases:
            with self.subTest(values=values), mock.patch.dict(os.environ, values, clear=True):
                with mock.patch.object(rex.os, "execve", side_effect=OSError("exec boundary")) as execute:
                    with self.assertRaises(OSError):
                        rex.entry([str(self.prefix), runtime.name, "--no-extensions"])
                environment = execute.call_args.args[2]
                self.assertEqual({key: value for key, value in environment.items()
                                  if key in ("PI_LINK_SCHEME", "PI_LINK_HOST")}, expected)
                self.assertEqual(dict(os.environ), values)

    def test_open_link_rejects_extra_arguments_before_any_exec(self):
        runtime = self.install_old()
        with mock.patch.object(rex.os, "execve") as execute:
            with self.assertRaisesRegex(rex.RexError, "Usage"):
                rex.entry([str(self.prefix), runtime.name, "open-link", "url", "extra"])
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
