"""Origin filesystem opener behavior without starting shells or editors."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[2] / "packages/coding-agent/rex-workspace"
SPEC = importlib.util.spec_from_file_location("path_opener", MODULE / "path_opener.py")
opener = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opener)


class Executed(Exception):
    pass


class Opener(unittest.TestCase):
    def test_file_uses_readonly_vim_and_literal_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 ; $(touch bad) ' %.md"
            path.write_text("set modeline?\n", encoding="utf-8")
            with mock.patch.object(opener, "executable", return_value=True), \
                    mock.patch.object(opener.os, "chdir") as chdir, \
                    mock.patch.object(opener.os, "execve", side_effect=Executed) as execute, \
                    mock.patch.dict(os.environ, {"VIMINIT": "!touch bad", "EXINIT": "!touch bad", "VIMRUNTIME": "/tmp/bad"}):
                with self.assertRaises(Executed):
                    opener.open_path(str(path), 12, 3)
            executable, command, environment = execute.call_args.args
            self.assertEqual(executable, "/usr/bin/vim")
            self.assertEqual(command[-2:], ["--", str(path)])
            for flag in ("-R", "-M", "-Z", "-n"):
                self.assertIn(flag, command)
            self.assertIn("set nomodeline noexrc", command)
            self.assertIn("call cursor(12,3)", command)
            self.assertEqual(command[command.index("-u") + 1], "NONE")
            self.assertEqual(command[command.index("-U") + 1], "NONE")
            self.assertNotIn("VIMINIT", environment)
            self.assertNotIn("EXINIT", environment)
            self.assertNotIn("VIMRUNTIME", environment)
            chdir.assert_called_once_with("/")

    def test_directory_changes_cwd_then_execs_account_login_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            def execute(*args):
                events.append(("exec", args))
                raise Executed
            with mock.patch.object(opener.pwd, "getpwuid", return_value=mock.Mock(pw_shell="/bin/zsh")), \
                    mock.patch.object(opener, "executable", return_value=True), \
                    mock.patch.object(opener.os, "chdir", side_effect=lambda path: events.append(("cwd", path))), \
                    mock.patch.object(opener.os, "execve", side_effect=execute), \
                    mock.patch.dict(os.environ, {"SHELL": "/tmp/untrusted-shell"}):
                with self.assertRaises(Executed):
                    opener.open_path(directory)
            self.assertEqual(events[0], ("cwd", directory))
            self.assertEqual(events[1][1][:2], ("/bin/zsh", ["zsh", "-i", "-l"]))

    def test_missing_vim_is_visible_failure(self):
        with tempfile.NamedTemporaryFile() as target, \
                mock.patch.object(opener, "executable", return_value=False), \
                mock.patch.object(opener.os, "execve") as execute, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(opener.main([target.name, "", ""]), 1)
        self.assertIn("requires Vim", errors.getvalue())
        execute.assert_not_called()

    def test_missing_path_and_special_file_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                opener.open_path(str(fifo))
            with self.assertRaises(OSError):
                opener.open_path(str(Path(directory) / "missing"))

    def test_rejects_bad_path_and_location_before_stat(self):
        for path, line, col in (("relative", None, None), ("/tmp/a\x00", None, None),
                                ("//host/a", None, None), ("/tmp/a", 0, None),
                                ("/tmp/a", 1, -1), ("/tmp/a", None, 1), ("/tmp/a", True, None)):
            with self.subTest(path=path, line=line, col=col), mock.patch.object(opener.os, "stat") as inspect:
                with self.assertRaises(ValueError):
                    opener.open_path(path, line, col)
                inspect.assert_not_called()
        for text in ("0", "-1", "1|quit", "2147483648"):
            with self.assertRaises(ValueError):
                opener.position(text)


if __name__ == "__main__":
    unittest.main()
