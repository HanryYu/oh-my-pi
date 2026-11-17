"""Run on the origin host, never on the URL receiver's filesystem."""

import os
import pwd
import re
import stat
import sys
import unicodedata

MAX_POSITION = 2_147_483_647
VIM_PATHS = ("/usr/bin/vim", "/bin/vim", "/opt/homebrew/bin/vim", "/usr/local/bin/vim")


def position(value):
    if value == "":
        return None
    if not re.fullmatch(r"[0-9]{1,10}", value) or not 1 <= int(value) <= MAX_POSITION:
        raise ValueError("Invalid line or column")
    return int(value)


def executable(path):
    return os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def open_path(path, line=None, col=None):
    if not path.startswith("/") or path.startswith("//") or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in path
    ):
        raise ValueError("Expected an absolute path without control characters")
    for value in (line, col):
        if value is not None and (type(value) is not int or not 1 <= value <= MAX_POSITION):
            raise ValueError("Invalid line or column")
    if col is not None and line is None:
        raise ValueError("A column requires a line")
    mode = os.stat(path).st_mode
    environment = os.environ.copy()
    if stat.S_ISDIR(mode):
        shell = pwd.getpwuid(os.getuid()).pw_shell
        if not executable(shell):
            raise ValueError("The account's login shell is unavailable")
        os.chdir(path)
        os.execve(shell, [os.path.basename(shell), "-i", "-l"], environment)
    elif stat.S_ISREG(mode):
        vim = next((candidate for candidate in VIM_PATHS if executable(candidate)), None)
        if vim is None:
            raise ValueError("Read-only viewing requires Vim; no installed Vim was found")
        # No target content, filename, or URL data becomes an Ex or shell command.
        command = [vim, "-N", "-u", "NONE", "-U", "NONE", "-i", "NONE", "-n", "-R", "-M", "-Z",
                   "--cmd", "set nomodeline noexrc", "-c", "set number", "-c", "syntax enable"]
        if line is not None:
            command += ["-c", "call cursor(%d,%d)" % (line, col or 1)]
        command += ["--", path]
        for key in ("VIMINIT", "GVIMINIT", "EXINIT", "VIM", "VIMRUNTIME"):
            environment.pop(key, None)
        os.chdir("/")
        os.execve(vim, command, environment)
    else:
        raise ValueError("Only regular files and directories can be opened")


def main(argv=None):
    values = sys.argv[1:] if argv is None else argv
    try:
        if len(values) != 3:
            raise ValueError("Expected path, line, and column")
        open_path(values[0], position(values[1]), position(values[2]))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print("Cannot open Rex path: " + str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
