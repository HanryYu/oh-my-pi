"""Validate OSC 8 path URLs and hand their data to Rex's host router."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unicodedata
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

MAX_POSITION = 2_147_483_647
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
AUTHORITY = re.compile(rf"({UUID})\.({UUID})\Z")
FRAGMENT = re.compile(r"L?([0-9]+)(?:[C:]([0-9]+))?\Z", re.IGNORECASE)


def clean_text(value):
    return not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def position(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("Line and column must be positive integers")
    number = int(value)
    if not 1 <= number <= MAX_POSITION:
        raise ValueError("Line or column is out of range")
    return number


def decode(value):
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Invalid percent encoding")
    result = unquote_to_bytes(value).decode("utf-8", "strict")
    if not clean_text(result):
        raise ValueError("Control characters are not allowed")
    return result


def parse_url(url):
    if not isinstance(url, str) or len(url) > 32768 or url != url.strip() or not clean_text(url):
        raise ValueError("Invalid path URL")
    parsed = urlsplit(url)
    authority = AUTHORITY.fullmatch(parsed.netloc)
    if parsed.scheme != "rex-omp-file" or not authority:
        raise ValueError("Expected a rex-omp-file URL with a session and pane UUID")
    path = decode(parsed.path)
    if not path.startswith("/") or path.startswith("//") or len(path.encode("utf-8")) > 16384:
        raise ValueError("Expected an absolute file path")
    result = {"session_id": "session:" + authority[1].lower(),
              "anchor": "block:" + authority[2].lower(), "path": path}
    # Validate escapes before parse_qsl, which otherwise replaces invalid UTF-8.
    decode(parsed.query)
    query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                      encoding="utf-8", errors="strict") if parsed.query else []
    for key, value in query:
        if key not in {"line", "col"} or key in result:
            raise ValueError("Unexpected or repeated position parameter")
        result[key] = position(value)
    fragment = decode(parsed.fragment)
    match = FRAGMENT.fullmatch(fragment)
    if match:
        for key, value in zip(("line", "col"), match.groups()):
            if value is not None:
                number = position(value)
                if key in result and result[key] != number:
                    raise ValueError("Conflicting URL positions")
                result[key] = number
    # Markdown heading fragments are deliberately not editor commands.
    if "col" in result and "line" not in result:
        raise ValueError("A column requires a line")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rex", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--module", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    try:
        if not os.path.isabs(args.rex):
            raise ValueError("The Rex executable must be an absolute path")
        request = parse_url(args.url)
        module = args.module.resolve(strict=True)
        script = module / "lua/open-path.lua"
        if not script.is_file():
            raise ValueError("The installed path router is unavailable")
        environment = os.environ.copy()
        for key in ("REX_SERVER", "REX_SESSION", "REX_BLOCK", "REX_CLIENT"):
            environment.pop(key, None)
        inventory = subprocess.run([args.rex, "--autostart=false", "hosts", "--json"],
                                   check=False, env=environment, capture_output=True, text=True, encoding="utf-8")
        if inventory.returncode:
            raise ValueError("Cannot determine the Rex home server: " + inventory.stderr.strip())
        hosts = json.loads(inventory.stdout)
        home = hosts.get("home") if isinstance(hosts, dict) else None
        if not isinstance(home, dict) or type(home.get("this_machine")) is not bool:
            raise ValueError("This Rex version does not identify whether its home server is this machine")
        label = home.get("label", "")
        if not isinstance(label, str) or len(label) > 256 or not clean_text(label):
            raise ValueError("Rex returned an invalid home server label")
        request["home_this_machine"], request["home_label"] = home["this_machine"], label
        completed = subprocess.run([args.rex, "--autostart=false", "do", str(script),
                                    "--args", json.dumps(request, ensure_ascii=False), "--json"],
                                   check=False, env=environment, capture_output=True, text=True, encoding="utf-8")
        if completed.returncode:
            if completed.stderr:
                print(completed.stderr.rstrip(), file=sys.stderr)
            if completed.stdout:
                print(completed.stdout.rstrip(), file=sys.stderr)
            return completed.returncode
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get("session_id") != request["session_id"]:
            raise ValueError("Rex returned an invalid path routing result")
        if "local_path" in result:
            if (
                request["home_this_machine"] is not True
                or result.get("server") != (request["home_label"] or "local")
                or result["local_path"] != request["path"]
                or any(result.get(key) != request.get(key) for key in ("line", "col"))
            ):
                raise ValueError("Rex returned an inconsistent local path")
            mode = os.stat(result["local_path"]).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError("Only regular files and directories can be opened")
            opened = subprocess.run(["/usr/bin/open", result["local_path"]], check=False,
                                    capture_output=True, text=True, encoding="utf-8")
            if opened.returncode:
                if opened.stderr:
                    print(opened.stderr.rstrip(), file=sys.stderr)
                return opened.returncode
        elif not (isinstance(result.get("window_id"), str) and isinstance(result.get("block_id"), str)):
            raise ValueError("Rex did not return an opened path terminal")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, UnicodeError) as error:
        print("Cannot open Rex path: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
