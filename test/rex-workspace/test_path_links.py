"""Path link protocol tests; no Rex server or filesystem target is opened."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

MODULE = Path(__file__).resolve().parents[2] / "packages/coding-agent/rex-workspace"
SPEC = importlib.util.spec_from_file_location("path_links", MODULE / "path_links.py")
links = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(links)
SESSION = "01a18bb0-ff4f-7829-aa39-eb90131cb46f"
ANCHOR = "01a18bb1-ffff-7000-aaaa-000000000001"
BASE = f"rex-omp-file://{SESSION}.{ANCHOR}"


class Protocol(unittest.TestCase):
    def test_unicode_spaces_and_command_text_are_only_path_data(self):
        path = "/tmp/中文 a;$(touch no)/`whoami`%?#.md"
        result = links.parse_url(BASE + quote(path, safe="/"))
        self.assertEqual(result, {"session_id": "session:" + SESSION, "anchor": "block:" + ANCHOR,
                                  "path": path})

    def test_positions_and_known_fragments(self):
        for suffix in ("?line=12&col=3", "#L12C3", "#12:3", "?line=12#L12C3"):
            with self.subTest(suffix=suffix):
                result = links.parse_url(BASE + "/file" + suffix)
                self.assertEqual((result["line"], result["col"]), (12, 3))
        self.assertEqual(links.parse_url(BASE + "/file#L12")["line"], 12)
        self.assertNotIn("line", links.parse_url(BASE + "/file#Heading-%3Ashell"))

    def test_rejects_wrong_authority(self):
        for value in ("file:///tmp/x", BASE.replace(SESSION, "localhost"),
                      BASE.replace("://", "://user@"), BASE + ":123/file", BASE + ".evil/file",
                      BASE + "/%2frelative", " " + BASE + "/file"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                links.parse_url(value)

    def test_rejects_controls_encoding_and_bad_positions(self):
        for suffix in ("/%00", "/%1b", "/%7f", "/%C2%85", "/%E2%80%AE", "/%FF", "/%", "/%GG",
                       "/a\nb", "/file?line=0", "/file?line=-1", "/file?line=2147483648",
                       "/file?line=1&line=2", "/file?col=3", "/file?cmd=quit", "/file?line=1%0A",
                       "/file?line=2#L3", "/file#L0", "/file#L2147483648", "/file#%00",
                       "/file?home_this_machine=true", "/file?home_label=Home"):
            with self.subTest(suffix=suffix), self.assertRaises((ValueError, UnicodeError)):
                links.parse_url(BASE + suffix)

    def test_receiver_uses_argv_and_propagates_exit(self):
        path = "/tmp/a ' ; $(touch bad) 中文.md"
        url = BASE + quote(path, safe="/") + "?line=5"
        inventory = mock.Mock(returncode=0, stdout=json.dumps({"home": {"this_machine": True, "label": ""}}), stderr="")
        with mock.patch.object(links.subprocess, "run", side_effect=[inventory, mock.Mock(returncode=7, stdout="", stderr="")]) as run:
            self.assertEqual(links.main(["--rex", "/trusted/rex", "--url", url, "--module", str(MODULE)]), 7)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["/trusted/rex", "--autostart=false", "do", str(MODULE / "lua/open-path.lua")])
        self.assertEqual(command[4], "--args")
        self.assertEqual(json.loads(command[5])["path"], path)
        self.assertEqual(command[6], "--json")
        self.assertEqual(run.call_args_list[0].args[0], ["/trusted/rex", "--autostart=false", "hosts", "--json"])
        self.assertEqual(run.call_args_list[0].kwargs["env"], run.call_args.kwargs["env"])
        self.assertTrue(json.loads(command[5])["home_this_machine"])
        self.assertEqual(json.loads(command[5])["home_label"], "")
        self.assertFalse(run.call_args.kwargs["check"])
        for key in ("REX_SERVER", "REX_SESSION", "REX_BLOCK", "REX_CLIENT"):
            self.assertNotIn(key, run.call_args.kwargs["env"])

    def invoke_receiver(self, path, result, extra="", code=0, inventory=None):
        completed = mock.Mock(returncode=code, stdout=json.dumps(result), stderr="")
        opened = mock.Mock(returncode=0, stdout="", stderr="")
        if inventory is None:
            inventory = {"home": {"this_machine": True, "label": ""}}
        hosts = mock.Mock(returncode=0, stdout=json.dumps(inventory), stderr="")
        with mock.patch.object(links.subprocess, "run", side_effect=[hosts, completed, opened]) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            exit_code = links.main(["--rex", "/trusted/rex", "--url", BASE + quote(path, safe="/") + extra,
                                    "--module", str(MODULE)])
        return exit_code, run.call_args_list, output.getvalue(), errors.getvalue()

    def test_confirmed_local_file_and_directory_use_system_open(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "中文 a ; $(touch bad) ' #.md"
            target.write_text("content", encoding="utf-8")
            for path in (str(target), directory):
                result = {"server": "local", "session_id": "session:" + SESSION, "local_path": path,
                          "line": 12, "col": 3}
                exit_code, calls, output, errors = self.invoke_receiver(path, result, "?line=12&col=3")
                self.assertEqual(exit_code, 0, errors)
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[2].args[0], ["/usr/bin/open", path])
                self.assertNotIn("shell", calls[2].kwargs)
                self.assertEqual(json.loads(output), result)

    def test_local_missing_and_special_paths_do_not_open(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            for path in (str(fifo), str(Path(directory) / "missing")):
                result = {"server": "local", "session_id": "session:" + SESSION, "local_path": path}
                exit_code, calls, output, errors = self.invoke_receiver(path, result)
                self.assertEqual(exit_code, 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(output, "")
                self.assertIn("Cannot open Rex path", errors)

    def test_remote_same_named_local_file_is_never_opened_or_statted(self):
        with tempfile.NamedTemporaryFile() as local_file:
            result = {"server": "Home", "session_id": "session:" + SESSION,
                      "window_id": "remote-window", "block_id": "remote-block"}
            for code in (0, 7):
                with mock.patch.object(links.os, "stat", wraps=os.stat) as inspect:
                    exit_code, calls, output, errors = self.invoke_receiver(local_file.name, result, code=code)
                self.assertEqual(exit_code, code)
                self.assertEqual(len(calls), 2)
                self.assertFalse(any(str(call.args[0]) == local_file.name for call in inspect.call_args_list))
                if code == 0:
                    self.assertEqual(json.loads(output), result)
                else:
                    self.assertEqual(output, "")

    def test_inconsistent_local_result_cannot_trigger_system_open(self):
        result = {"server": "Home", "session_id": "session:" + SESSION, "local_path": "/tmp/a"}
        exit_code, calls, output, errors = self.invoke_receiver("/tmp/a", result)
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(output, "")
        self.assertIn("inconsistent local path", errors)

    def test_remote_home_never_authorizes_local_system_open(self):
        inventory = {"home": {"this_machine": False, "label": "Home", "endpoint": "ignored"}}
        with tempfile.NamedTemporaryFile() as local_file:
            result = {"server": "Home", "session_id": "session:" + SESSION,
                      "window_id": "remote-window", "block_id": "remote-block"}
            with mock.patch.object(links.os, "stat", wraps=os.stat) as inspect:
                exit_code, calls, output, errors = self.invoke_receiver(local_file.name, result, inventory=inventory)
            self.assertEqual(exit_code, 0, errors)
            self.assertEqual(len(calls), 2)
            self.assertFalse(any(str(call.args[0]) == local_file.name for call in inspect.call_args_list))
            self.assertIs(json.loads(calls[1].args[0][5])["home_this_machine"], False)
            self.assertEqual(json.loads(output), result)
            result = {"server": "Home", "session_id": "session:" + SESSION, "local_path": local_file.name}
            exit_code, calls, output, errors = self.invoke_receiver(local_file.name, result, inventory=inventory)
            self.assertEqual(exit_code, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(output, "")
            self.assertIn("inconsistent local path", errors)

    def test_missing_or_non_boolean_home_identity_is_unsupported(self):
        for inventory in ({}, {"home": {}}, {"home": {"this_machine": "true"}},
                          {"home": {"this_machine": 1}}, {"home": {"this_machine": None}}):
            exit_code, calls, output, errors = self.invoke_receiver("/tmp/a", {}, inventory=inventory)
            self.assertEqual(exit_code, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(output, "")
            self.assertIn("does not identify", errors)

    def test_receiver_rejects_relative_executable_before_spawning(self):
        with mock.patch.object(links.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(links.main(["--rex", "rex", "--url", BASE + "/tmp/x"]), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
