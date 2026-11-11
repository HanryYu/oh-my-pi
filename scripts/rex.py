#!/usr/bin/env python3
"""Install and update the personal Rex OMP fork without modifying official OMP.

Every candidate owns its checkout, dependencies and launcher for its entire life.
Only active.json and the managed integration are replaced during activation.
"""

import argparse
import base64
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile

FORK = "https://github.com/HanryYu/oh-my-pi.git"
UPSTREAM = "https://github.com/can1357/oh-my-pi.git"
BRANCH = "rex"
BUN_VERSION = "1.4.4"
RESOURCE = Path("packages/coding-agent/rex-workspace")
CLI = Path("packages/coding-agent/src/cli.ts")
MANIFEST = RESOURCE / "runtime.json"
MODULE = "rex-agent-workspace"
MARKER = "# Managed by rex-omp fork\n"
OLD_MARKER = "# Managed by rex-agent-workspace\n"
BEGIN = b"\n-- BEGIN rex-agent-workspace (managed)\n"
END = b"-- END rex-agent-workspace (managed)\n"
VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")
TESTS = (
    "packages/coding-agent/test/rex/bridge.test.ts",
    "packages/coding-agent/test/rex/journal.test.ts",
    "packages/coding-agent/test/rex/security.test.ts",
    "packages/coding-agent/test/rex/live.test.ts",
    "packages/coding-agent/test/eval/agent-bridge.test.ts",
    "packages/coding-agent/test/eval/eval-parent-tool-call-id.test.ts",
    "packages/coding-agent/test/task/parked-subagent-prewalk-revive.test.ts",
)


class RexError(RuntimeError):
    pass


def safe_path(value):
    """Reject link traversal, including dangling links, before normalization."""
    path = Path(os.path.abspath(os.path.expanduser(str(value))))
    for part in (path,) + tuple(path.parents):
        if part.is_symlink() and str(part) not in ("/tmp", "/var", "/etc"):
            raise RexError("Refusing symlink installation path: " + str(part))
    return path.resolve()


def inside(path, parent):
    return path == parent or parent in path.parents


def private_directory(path):
    path = safe_path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RexError("Private directory is not owned safely: " + str(path))
    path.chmod(0o700)
    return path


def json_read(path):
    path = safe_path(path)
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def json_write(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def write_bytes(path, content, mode=0o600):
    path = safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def run(argv, cwd=None, env=None, timeout=600):
    result = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").strip()
        output = result.stdout.decode("utf-8", "replace").strip()
        raise RexError(shlex.join([str(arg) for arg in argv]) + " failed ("
                       + str(result.returncode) + ")\n" + diagnostic + ("\n" + output if output else ""))
    return result.stdout.decode("utf-8", "strict").strip()


def git(source, *args):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Rex OMP local integration",
                       GIT_AUTHOR_EMAIL="rex-omp@localhost", GIT_COMMITTER_NAME="Rex OMP local integration",
                       GIT_COMMITTER_EMAIL="rex-omp@localhost")
    return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-c", "protocol.file.allow=always", *args], cwd=source, env=environment)


def ancestor(source, older, newer):
    try:
        git(source, "merge-base", "--is-ancestor", older, newer)
        return True
    except RexError:
        return False


def exact_version(value):
    value = value[1:] if value.startswith("v") else value
    if not VERSION.fullmatch(value):
        raise RexError("Expected an exact stable version, not a branch or prerelease: " + value)
    return value


def remote(value):
    if value.startswith("https://") and urlparse(value).hostname:
        return value
    if Path(value).is_absolute() and Path(value).is_dir():
        return str(safe_path(value))
    raise RexError("Remote must be HTTPS or an explicit absolute local Git repository: " + value)


def fetch_bytes(url, host=None):
    if urlparse(url).scheme != "https":
        raise RexError("Downloads require HTTPS")
    request = Request(url, headers={"User-Agent": "rex-omp-fork", "Accept": "application/json"})
    with urlopen(request, timeout=120) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or (host is not None and final.hostname != host):
            raise RexError("Unexpected download redirect: " + response.geturl())
        return response.read()


def release_tag(upstream, requested=None):
    if requested:
        return "v" + exact_version(requested)
    if upstream != UPSTREAM:
        raise RexError("Custom/local upstream requires --upstream-version for reproducible integration")
    release = json.loads(fetch_bytes("https://api.github.com/repos/can1357/oh-my-pi/releases/latest"))
    if release.get("draft") or release.get("prerelease"):
        raise RexError("Official release is not stable")
    tag = release.get("tag_name", "")
    if tag != "v" + exact_version(tag):
        raise RexError("Official stable release has an unsupported tag")
    return tag


def checkout_candidate(prefix, origin, revision="HEAD", branch=None):
    """A local clone, never a worktree of or a write into the user's checkout."""
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    runtime = private_directory(prefix / "runtimes" / identifier)
    source = runtime / "source"
    try:
        git(None, "clone", "--no-checkout", "--no-hardlinks", remote(str(origin)), str(source))
        if branch:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
                raise RexError("Invalid fork branch")
            git(source, "fetch", "--no-tags", "origin", "refs/heads/" + branch + ":refs/remotes/origin/" + branch)
            revision = "refs/remotes/origin/" + branch
        git(source, "checkout", "--detach", revision)
        return runtime, git(source, "rev-parse", "HEAD")
    except BaseException:
        shutil.rmtree(runtime)
        raise


def integrate_upstream(source, upstream, tag, initial_base=None):
    """Resolve the actual official tag, merge it, then prove its ancestry."""
    version = exact_version(tag)
    tag = "v" + version
    git(source, "remote", "add", "rex-upstream", remote(upstream))
    ref = "refs/tags/rex-upstream/" + tag
    git(source, "fetch", "--no-tags", "rex-upstream", "refs/tags/" + tag + ":" + ref)
    commit = git(source, "rev-parse", ref + "^{commit}")
    if initial_base and not ancestor(source, initial_base, commit):
        raise RexError("Official tag is not descended from this fork's supported base")
    for package in ("packages/coding-agent/package.json", "packages/natives/package.json"):
        metadata = json.loads(git(source, "show", commit + ":" + package))
        if metadata.get("version") != version:
            raise RexError("Official tag/package version mismatch: " + package)
    git(source, "merge", "--no-edit", "--no-ff", ref)
    if not ancestor(source, commit, "HEAD"):
        raise RexError("Official release is not an ancestor of the integrated candidate")
    for package in ("packages/coding-agent/package.json", "packages/natives/package.json"):
        if json_read(source / package).get("version") != version:
            raise RexError("Merged candidate did not adopt the official package version: " + package)
    return {"remote": upstream, "tag": tag, "commit": commit, "version": version}


def manifest(source):
    value = json_read(source / MANIFEST)
    if value.get("schema") != 1 or value.get("protocol") != {"root": 3, "live": 1}:
        raise RexError("Unsupported fork workspace protocol")
    if not set(TESTS).issubset(value.get("validationTests", [])):
        raise RexError("Fork validation suite is incomplete")
    for name in value.get("requiredFeatures", []) + list(TESTS):
        path = safe_path(source / name)
        if not inside(path, source) or not path.is_file():
            raise RexError("Fork feature/test is missing: " + str(name))
    base = value.get("initialBase", {})
    if not re.fullmatch(r"[0-9a-f]{40}", base.get("commit", "")):
        raise RexError("Fork manifest has no exact initial upstream base")
    return value


def bun_requirement(source):
    """Deliberately support only explicit stable minimum/exact engine ranges."""
    minimum = tuple(map(int, BUN_VERSION.split(".")))
    exact = None
    packages = [source / "package.json", *sorted((source / "packages").glob("*/package.json"))]
    for package in packages:
        requirement = json_read(package).get("engines", {}).get("bun")
        if not requirement:
            continue
        match = re.fullmatch(r"(>=)?(\d+\.\d+\.\d+)", requirement.strip())
        if not match:
            raise RexError("Unsupported Bun engine constraint in " + str(package) + ": " + requirement)
        version = tuple(map(int, match[2].split(".")))
        if match[1]:
            minimum = max(minimum, version)
        elif exact is not None and exact != version:
            raise RexError("Candidate packages require conflicting exact Bun versions")
        else:
            exact = version
    if exact is not None and exact < minimum:
        raise RexError("Candidate Bun engine constraints cannot be satisfied")
    return minimum, exact


def bun_runtime(prefix, source, supplied=None):
    minimum, exact = bun_requirement(source)
    wanted = ".".join(map(str, exact or minimum))
    toolchains = private_directory(prefix / "toolchains")
    data = None
    if supplied:
        # Copy, never modify, a caller's or earlier runtime's private executable.
        executable = Path(supplied).expanduser().resolve(strict=True)
        reported = run([executable, "--version"])
        if not VERSION.fullmatch(reported):
            raise RexError("Supplied Bun did not report a stable release version")
        version = tuple(map(int, reported.split(".")))
        if version >= minimum and (exact is None or version == exact):
            wanted, data = reported, executable.read_bytes()
    if data is None:
        asset_arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x64-baseline"}.get(platform.machine())
        if sys.platform != "darwin" or asset_arch is None:
            raise RexError("Automatic private Bun bootstrap supports macOS arm64/x64; supply a matching --bun")
        asset = "bun-darwin-" + asset_arch
        base = "https://github.com/oven-sh/bun/releases/download/bun-v" + wanted + "/"
        sums = fetch_bytes(base + "SHASUMS256.txt").decode().splitlines()
        expected = next((line.split()[0] for line in sums if line.split() and
                         line.split()[-1].lstrip("*") == asset + ".zip"), None)
        if not expected or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise RexError("Official Bun release is missing its SHA256 checksum")
        archive = fetch_bytes(base + asset + ".zip")
        if not hmac.compare_digest(hashlib.sha256(archive).hexdigest(), expected):
            raise RexError("Private Bun archive checksum mismatch")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            data = bundle.read(asset + "/bun")
    fingerprint = hashlib.sha256(data).hexdigest()
    directory = private_directory(toolchains / ("bun-" + wanted + "-" + fingerprint[:16]))
    executable = safe_path(directory / "bun")
    if executable.exists():
        if hashlib.sha256(executable.read_bytes()).hexdigest() != fingerprint:
            raise RexError("Private Bun cache was modified")
    else:
        write_bytes(executable, data, 0o700)
    if run([executable, "--version"]) != wanted:
        raise RexError("Private Bun executable version mismatch")
    return str(executable)


def native_addon(runtime, version, bun, installed=None):
    source = runtime / "source"
    host = {"darwin": "darwin", "linux": "linux"}.get(sys.platform)
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(platform.machine())
    if not host or not arch:
        raise RexError("No supported prebuilt native addon for this host")
    tag = host + "-" + arch
    names = (["pi_natives." + tag + "-baseline.node", "pi_natives." + tag + ".node"] if arch == "x64"
             else ["pi_natives." + tag + ".node"])
    destination = safe_path(source / "packages/natives/native")
    stamp = ("PI_NATIVES_VERSION_STAMP:" + version + "\0").encode()
    installed = Path(installed).expanduser() if installed else Path(os.environ.get("PI_NATIVES_DIR", str(Path.home() / ".omp/natives")))
    proof = None
    for name in names:
        candidate = installed / version / name
        if candidate.is_symlink() or not candidate.is_file():
            continue
        info = candidate.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            continue
        data = candidate.read_bytes()
        if stamp not in data:
            continue
        write_bytes(destination / name, data)
        proof = {"source": "installed", "path": str(candidate), "sha256": hashlib.sha256(data).hexdigest(), "file": name}
        break
    if proof is None:
        package = "@oh-my-pi/pi-natives-" + tag
        metadata = json.loads(fetch_bytes("https://registry.npmjs.org/" + quote(package, safe="@/") + "/" + version,
                                          host="registry.npmjs.org"))
        dist = metadata.get("dist", {})
        integrity, url = dist.get("integrity", ""), dist.get("tarball", "")
        if metadata.get("name") != package or metadata.get("version") != version or not integrity.startswith("sha512-"):
            raise RexError("Native package metadata does not identify the exact requested release")
        if urlparse(url).scheme != "https" or urlparse(url).hostname != "registry.npmjs.org":
            raise RexError("Native package tarball is not hosted on the HTTPS npm registry")
        data = fetch_bytes(url, host="registry.npmjs.org")
        actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        if not hmac.compare_digest(actual, integrity):
            raise RexError("Native package SRI mismatch")
        notices = private_directory(runtime / "native-licenses")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = {item.name: item for item in archive.getmembers()}
            selected = next((name for name in names if "package/" + name in members), None)
            if selected is None:
                raise RexError("Native package has no suitable host addon")
            for name in (selected, "LICENSE", "THIRD-PARTY-NOTICES.txt"):
                member = members.get("package/" + name)
                if member is None or not member.isfile():
                    raise RexError("Native archive is missing a regular addon/legal file: " + name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise RexError("Cannot read native archive member")
                payload = stream.read()
                if name == selected and stamp not in payload:
                    raise RexError("Native addon stamp does not match the requested release")
                write_bytes((destination if name == selected else notices) / name, payload)
        proof = {"source": "registry", "package": package, "integrity": integrity, "tarball": url, "file": selected}
        json_write(notices / "distribution.json", proof)
    reported = run([bun, source / "scripts/install-tests/native-version.ts", destination / proof["file"]])
    if reported != version:
        raise RexError("Native addon reported a different release version: " + reported)
    proof["version"] = reported
    return proof


def validation_environment(runtime, home):
    # Validation must never discover accounts, sync credentials or run a model.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("REX_", "OMP_", "PI_", "XDG_", "GIT_"))}
    environment.update(HOME=str(home), PI_CODING_AGENT_DIR=str(home / "agent"),
                       XDG_CONFIG_HOME=str(home / "config"), XDG_CACHE_HOME=str(home / "cache"),
                       XDG_DATA_HOME=str(home / "data"), OMP_CONFIG_SYNC="0", REX_SUBAGENT_VIEWER="0",
                       PI_NATIVES_DIR=str(runtime / "natives"), PYTHONDONTWRITEBYTECODE="1",
                       BUN_INSTALL_CACHE_DIR=str(runtime / "dependency-cache"))
    return environment


def frozen_dependencies(runtime, bun):
    source = runtime / "source"
    manifest(source)
    with tempfile.TemporaryDirectory(prefix="rex-validate-", dir="/tmp") as temporary:
        home = Path(temporary)
        environment = validation_environment(runtime, home)
        # No scripts: prebuilt native installation is explicit and version checked.
        run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], cwd=source, env=environment, timeout=1800)


def behavior_validation(runtime, bun, version):
    source = runtime / "source"
    with tempfile.TemporaryDirectory(prefix="rex-validate-", dir="/tmp") as temporary:
        home = Path(temporary)
        environment = validation_environment(runtime, home)
        run([bun, "test", *TESTS], cwd=source, env=environment, timeout=900)
        output = run([bun, source / CLI, "--version"], cwd=source, env=environment, timeout=60)
        if not re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])", output):
            raise RexError("Source CLI version smoke did not report the integrated release")
        # Help traverses the real early readonly dispatch, not an installer mock.
        before = set(home.rglob("*"))
        help_text = run([bun, source / CLI, "rex-view", "--help"], cwd=source, env=environment, timeout=60)
        if "--root" not in help_text or "--module" not in help_text:
            raise RexError("Native rex-view command did not expose its readonly route")
        created = set(home.rglob("*")) - before
        if any(path.name in ("agent.db", "settings.json", "settings.yml", "settings.yaml") or
               path.suffix == ".jsonl" for path in created):
            raise RexError("Readonly help unexpectedly initialized account/session configuration")
    return {"tests": list(TESTS), "versionSmoke": output, "readonlyHelp": True}


def launcher_bytes(prefix, runtime, active=False):
    argv = [sys.executable, str(runtime / "source/scripts/rex.py"), "__entry", str(prefix),
            "active" if active else runtime.name]
    return ("#!/bin/sh\n" + MARKER + "exec " + shlex.join(argv) + ' "$@"\n').encode()


def prepare_candidate(prefix, origin, bun=None, upstream=UPSTREAM, upstream_version=None,
                      branch=None, native_dir=None):
    runtime, fork_commit = checkout_candidate(prefix, origin, branch=branch)
    try:
        source = runtime / "source"
        feature = manifest(source)
        base = feature["initialBase"]
        selected_version = upstream_version or (json_read(source / "packages/coding-agent/package.json")["version"] if branch is None else None)
        tag = release_tag(upstream, selected_version)
        official = integrate_upstream(source, upstream, tag, base["commit"])
        private_bun = bun_runtime(prefix, source, bun)
        private_directory(runtime / "natives")
        frozen_dependencies(runtime, private_bun)
        native = native_addon(runtime, official["version"], private_bun, native_dir)
        evidence = behavior_validation(runtime, private_bun, official["version"])
        metadata = {"schema": 1, "id": runtime.name, "createdAt": datetime.now(timezone.utc).isoformat(),
                    "version": official["version"], "commit": git(source, "rev-parse", "HEAD"),
                    "forkCommit": fork_commit, "fork": {"remote": str(origin), "branch": branch or BRANCH},
                    "upstream": official, "bun": private_bun, "native": native, "validation": evidence}
        write_bytes(runtime / "rex-omp", launcher_bytes(prefix, runtime), 0o700)
        # Publish ready only after the complete gate; failed candidates are private.
        json_write(runtime / "runtime.json", metadata)
        return runtime
    except BaseException:
        shutil.rmtree(runtime)
        raise


def managed_config(original, module, rex_binary):
    def lua_string(value):
        value = str(value)
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise RexError("Installation paths must not contain control characters")
        return json.dumps(value, ensure_ascii=False)
    block = (BEGIN + ("dofile(" + lua_string(module / "lua/init.lua") + ")({module = " + lua_string(module)
                      + ", rex = " + lua_string(rex_binary) + "})\n").encode() + END)
    starts, ends = original.count(BEGIN), original.count(END)
    if starts != ends or starts > 1:
        raise RexError("Malformed or duplicate managed Rex configuration block")
    if not starts:
        return original + block
    start, end = original.index(BEGIN), original.index(END)
    if end < start:
        raise RexError("Malformed managed Rex configuration block")
    return original[:start] + block + original[end + len(END):]


def check_launcher(path, prefix=None):
    path = safe_path(path)
    if path.exists():
        if not path.is_file():
            raise RexError("rex-omp is not a regular managed command")
        contents = path.read_text()
        if not any(contents.startswith("#!/bin/sh\n" + marker) for marker in (MARKER, OLD_MARKER)):
            raise RexError("Refusing to overwrite an unrelated rex-omp command: " + str(path))
        if prefix is not None and contents.startswith("#!/bin/sh\n" + MARKER):
            lines = contents.splitlines()
            command = shlex.split(lines[2]) if len(lines) > 2 else []
            if len(command) < 6 or command[3] != "__entry" or command[4] != str(prefix):
                raise RexError("rex-omp belongs to a different private prefix: " + str(path))


def installation_paths(prefix, agent_dir, rex_dir, bin_dir, rex_binary):
    paths = {"agentDir": str(safe_path(agent_dir)), "rexDir": str(safe_path(rex_dir)),
             "binDir": str(safe_path(bin_dir)), "rexBinary": os.path.abspath(os.path.expanduser(str(rex_binary)))}
    locations = [prefix] + [Path(paths[key]) for key in ("agentDir", "rexDir", "binDir")]
    for i, left in enumerate(locations):
        for right in locations[i + 1:]:
            if inside(left, right) or inside(right, left):
                raise RexError("Runtime, profile, Rex and command directories must not overlap")
    if "extensions" in Path(paths["agentDir"]).parts or "extensions" in Path(paths["rexDir"]).parts:
        raise RexError("Profile and Rex directories must be outside extension discovery")
    return paths


@contextmanager
def locks(paths):
    with ExitStack() as stack:
        for path in sorted(set(paths)):
            path = safe_path(path)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = stack.enter_context(os.fdopen(descriptor, "a+b"))
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RexError("Unsafe install lock: " + str(path))
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_state(prefix, required=True):
    file = safe_path(prefix / "active.json")
    if not file.exists() and not required:
        return None
    state = json_read(file)
    if state.get("schema") != 1 or not IDENTIFIER.fullmatch(state.get("active", "")):
        raise RexError("Invalid active runtime state")
    return state


def ready_runtime(prefix, identifier):
    if not IDENTIFIER.fullmatch(identifier):
        raise RexError("Invalid runtime identifier")
    runtime = safe_path(prefix / "runtimes" / identifier)
    metadata = json_read(runtime / "runtime.json")
    if metadata.get("schema") != 1 or metadata.get("id") != identifier or not (runtime / "rex-omp").is_file():
        raise RexError("Runtime has not passed the validation gate: " + identifier)
    return runtime, metadata


def stop_observer(module, worker=None):
    worker = safe_path(worker or module / "worker.py")
    if worker.is_file():
        run([sys.executable, worker, "stop", "--module", module], timeout=25)


def reload_rex(rex_binary):
    run([rex_binary, "--autostart=false", "config", "reload"], timeout=30)


def remove_owned(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def apply_transaction(plans, finish=None, restore=None):
    """Stage siblings on each target filesystem; retain all originals externally."""
    entries = []
    try:
        for target, stage, backup in plans:
            safe_path(target)
            entry = {"target": target, "backup": backup, "saved": False, "active": False}
            entries.append(entry)
            if target.exists():
                if backup is None or backup.exists():
                    raise RexError("Activation backup collides or is missing")
                if target.is_file() and stage is not None:
                    # Copy the backup, then atomically replace the live file.
                    # In particular active.json is never temporarily absent.
                    shutil.copy2(target, backup)
                else:
                    os.replace(target, backup)
                entry["saved"] = True
            if stage is not None:
                os.replace(stage, target)
                entry["active"] = True
        if finish:
            finish()
    except BaseException as error:
        failures = []
        for entry in reversed(entries):
            try:
                if entry["saved"] and entry["backup"].is_file():
                    os.replace(entry["backup"], entry["target"])
                else:
                    if entry["active"]:
                        remove_owned(entry["target"])
                    if entry["saved"]:
                        os.replace(entry["backup"], entry["target"])
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        if restore:
            try:
                restore()
            except Exception as restore_error:
                failures.append(str(restore_error))
        if failures:
            raise RexError("Activation failed: " + str(error) + "; rollback errors: " + "; ".join(failures)) from error
        raise


def activate(prefix, runtime, locations, observer=True):
    ready_runtime(prefix, runtime.name)
    rex_dir, bin_dir = (Path(locations[name]) for name in ("rexDir", "binDir"))
    for directory in (rex_dir, bin_dir):
        safe_path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_paths = [path / ".rex-omp-install.lock" for path in (rex_dir, bin_dir)]
    with locks(lock_paths):
        current = load_state(prefix, required=False)
        module = safe_path(rex_dir / MODULE)
        config = safe_path(rex_dir / "init.lua")
        launcher = safe_path(bin_dir / "rex-omp")
        check_launcher(launcher, prefix)
        if module.exists() and not module.is_dir():
            raise RexError("Installation module is not a directory: " + str(module))
        if config.exists() and not config.is_file():
            raise RexError("Rex init.lua is not a regular file")
        rex_backups = private_directory(rex_dir / "workspace-backups")
        state_backups = private_directory(prefix / "state-backups")
        command_backups = private_directory(bin_dir / ".rex-omp-backups")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
        stages = []
        plans = []
        try:
            module_stage = Path(tempfile.mkdtemp(prefix=".staging-module-", dir=rex_backups))
            stages.append(module_stage)
            resource = runtime / "source" / RESOURCE
            for name in ("worker.py", "project.py"):
                shutil.copy2(safe_path(resource / name), module_stage / name)
            (module_stage / "lua").mkdir(mode=0o700)
            for item in sorted((resource / "lua").glob("*.lua")):
                shutil.copy2(safe_path(item), module_stage / "lua" / item.name)
            original = config.read_bytes() if config.exists() else b""
            if module.exists() and (BEGIN not in original or not (module / "lua/init.lua").is_file()):
                raise RexError("Refusing to replace an unmanaged Rex workspace directory: " + str(module))
            config_stage = rex_backups / (".staging-config-" + stamp)
            stages.append(config_stage)
            mode = stat.S_IMODE(config.stat().st_mode) if config.exists() else 0o600
            write_bytes(config_stage, managed_config(original, module, locations["rexBinary"]), mode)
            launch_stage = command_backups / (".staging-command-" + stamp)
            stages.append(launch_stage)
            write_bytes(launch_stage, launcher_bytes(prefix, runtime, active=True), 0o700)
            previous = current["active"] if current and current["active"] != runtime.name else current.get("previous") if current else None
            state = {"schema": 1, "active": runtime.name, "previous": previous,
                     "activatedAt": datetime.now(timezone.utc).isoformat(), "installation": locations}
            state_stage = state_backups / (".staging-state-" + stamp)
            stages.append(state_stage)
            json_write(state_stage, state)
            plans.extend(((module, module_stage, rex_backups / (MODULE + "-" + stamp)),
                          (config, config_stage, rex_backups / ("init.lua-" + stamp)),
                          (launcher, launch_stage, command_backups / ("rex-omp-" + stamp)),
                          (prefix / "active.json", state_stage, state_backups / ("active-" + stamp + ".json"))))
            if observer:
                stop_observer(module)
            def restore_observer():
                # A failed config reload may already have started the new
                # watcher. Stop it via the immutable helper, even on first
                # install where rollback removes the installed module.
                stop_observer(module, resource / "worker.py")
                reload_rex(locations["rexBinary"])
            apply_transaction(plans,
                              finish=(lambda: reload_rex(locations["rexBinary"])) if observer else None,
                              restore=restore_observer if observer else None)
            return state
        finally:
            for stage in stages:
                if stage.exists():
                    remove_owned(stage)


def manager_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prefix = Path.home() / ".local/share/rex-omp"
    for name in ("install", "update", "rollback", "fork-info"):
        command = commands.add_parser(name)
        command.add_argument("--prefix", type=Path, default=prefix)
        if name in ("install", "update"):
            command.add_argument("--bun", type=Path)
            command.add_argument("--upstream", default=None)
            command.add_argument("--upstream-version")
            command.add_argument("--native-dir", type=Path)
        if name == "install":
            command.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
            command.add_argument("--agent-dir", type=Path, default=Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".omp/agent"))))
            command.add_argument("--rex-dir", type=Path, default=Path.home() / ".config/rex")
            command.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
            command.add_argument("--rex", default=shutil.which("rex") or "rex")
            command.add_argument("--fork", default=FORK)
            command.add_argument("--fork-branch", default=BRANCH)
        if name == "update":
            command.add_argument("--fork")
            command.add_argument("--fork-branch")
        if name == "rollback":
            command.add_argument("--runtime", help="Retained immutable runtime id; default is the previous active runtime")
    return parser


def entry(argv):
    if len(argv) < 2:
        raise RexError("Invalid managed launcher")
    prefix, identifier, *forwarded = argv
    if forwarded and forwarded[0] in ("update", "rollback", "fork-info"):
        return main([forwarded[0], "--prefix", prefix, *forwarded[1:]])
    prefix = safe_path(prefix)
    if identifier == "active":
        identifier = load_state(prefix)["active"]
    runtime, metadata = ready_runtime(prefix, identifier)
    environment = dict(os.environ, REX_WORKSPACE_OMP=str(runtime / "rex-omp"),
                       PI_NATIVES_DIR=str(runtime / "natives"))
    # No config/profile/account/model environment overrides. exec retains PTY,
    # controlling terminal, signals and the source CLI's normal sync lifecycle.
    os.execve(metadata["bun"], [metadata["bun"], str(runtime / "source" / CLI), *forwarded], environment)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "__entry":
        return entry(argv[1:])
    args = manager_parser().parse_args(argv)
    prefix = private_directory(args.prefix)
    private_directory(prefix / "runtimes")
    with locks([prefix / "install.lock"]):
        current = load_state(prefix, required=args.command != "install")
        if args.command == "fork-info":
            runtime, metadata = ready_runtime(prefix, current["active"])
            print(json.dumps({"schema": 1, "prefix": str(prefix), "state": current, "runtime": metadata,
                              "executable": str(runtime / "rex-omp")}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "rollback":
            identifier = args.runtime or current.get("previous")
            if not identifier:
                raise RexError("There is no previous validated runtime to roll back to")
            runtime, _ = ready_runtime(prefix, identifier)
            state = activate(prefix, runtime, current["installation"])
        else:
            if args.command == "install":
                source = safe_path(args.source)
                if current:
                    raise RexError("This prefix is already installed; use rex-omp update or a new --prefix")
                if git(source, "status", "--porcelain", "--untracked-files=normal"):
                    raise RexError("Source checkout has uncommitted changes; commit them before installing")
                rex_binary = shutil.which(args.rex) if not Path(args.rex).is_absolute() else args.rex
                if not rex_binary:
                    raise RexError("Rex executable was not found; supply --rex")
                locations = installation_paths(prefix, args.agent_dir, args.rex_dir, args.bin_dir, rex_binary)
                check_launcher(Path(locations["binDir"]) / "rex-omp", prefix)
                runtime = prepare_candidate(prefix, source, bun=args.bun, upstream=args.upstream or UPSTREAM,
                                            upstream_version=args.upstream_version, native_dir=args.native_dir)
                metadata = json_read(runtime / "runtime.json")
                # Future updates fetch the public fork, not the developer checkout.
                metadata["fork"] = {"remote": remote(args.fork), "branch": args.fork_branch}
                ready = runtime / "runtime.json"
                ready.unlink()
                json_write(ready, metadata)
            else:
                _, active = ready_runtime(prefix, current["active"])
                locations = current["installation"]
                upstream = args.upstream or active["upstream"]["remote"]
                runtime = prepare_candidate(prefix, args.fork or active["fork"]["remote"], bun=args.bun or active["bun"],
                                            upstream=upstream, upstream_version=args.upstream_version,
                                            branch=args.fork_branch or active["fork"]["branch"], native_dir=args.native_dir)
            state = activate(prefix, runtime, locations)
        print(json.dumps({"active": state["active"], "previous": state.get("previous"), "prefix": str(prefix)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RexError, OSError, ValueError, subprocess.TimeoutExpired, tarfile.TarError, zipfile.BadZipFile) as error:
        print("rex-omp: " + str(error), file=sys.stderr)
        sys.exit(1)
