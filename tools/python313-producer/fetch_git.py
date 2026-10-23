"""Restore only the pinned Git dependency and its recorded testsuite gitlink.

Uses the CI image's Git client with a private empty home/template and no
credential helpers or checkout hooks. No downloaded source is executed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from build_native import fingerprint
from build_wasi import run_stage
from fetch_sdk import inventory

INPUTS = (
    {"name": "wasm-tools", "url": "https://github.com/dicej/wasm-tools",
     "commit": "b072b0caa8307779558d96a62bc9522abda6a7fb",
     "tree": "dbc1de5b0219dc5b55e93393bdd190f8faf04b07"},
    {"name": "testsuite", "url": "https://github.com/WebAssembly/testsuite",
     "commit": "7b59001394c1d9c073ffa3c419df0bd52474bc6d",
     "tree": "447086cd65106fbb8e289ddb9204794b84859ba7"},
)
GIT = ("/usr/bin/git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
       "-c", "core.fsmonitor=false", "-c", "protocol.version=2",
       "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true")
SUBMODULES = (b'[submodule "tests/testsuite"]\n\tpath = tests/testsuite\n'
              b'\turl = https://github.com/WebAssembly/testsuite\n')
MAX_OUTPUT = 2 * 1024 * 1024


def validate_commit(data: bytes, expected_id: str, expected_tree: str) -> None:
    """Independently bind Git's returned commit bytes and their root tree."""
    if len(data) > 1024 * 1024:
        raise ValueError("Git commit size bound exceeded")
    object_bytes = b"commit " + str(len(data)).encode() + b"\0" + data
    if hashlib.sha1(object_bytes).hexdigest() != expected_id:
        raise ValueError("Git commit object identity mismatch")
    headers = data.split(b"\n\n", 1)[0].splitlines()
    expected = b"tree " + expected_tree.encode()
    if (not headers or headers[0] != expected
            or sum(line.startswith(b"tree ") for line in headers) != 1):
        raise ValueError("Git dependency root tree mismatch")


def script(work: Path) -> str:
    """Construct the two exact shallow fetches, without recursive acquisition."""
    lines = ["set -eu", f"export HOME={shlex.quote(str(work / 'home'))}",
             f"export XDG_CONFIG_HOME={shlex.quote(str(work / 'home'))}",
             "export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null",
             "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=0",
             "export GIT_TERMINAL_PROMPT=0 GIT_ALLOW_PROTOCOL=https",
             "export GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false"]
    for entry in INPUTS:
        repository = work / (entry["name"] + ".git")
        lines += [
            shlex.join([*GIT, "init", "--bare", "--template=" + str(work / "empty-template"),
                        str(repository)]),
            shlex.join([*GIT, "--git-dir=" + str(repository), "fetch", "--depth=1",
                        "--no-tags", "--no-recurse-submodules", entry["url"],
                        "+" + entry["commit"] + ":refs/tobkiri/input"]),
            shlex.join([*GIT, "--git-dir=" + str(repository), "fsck", "--strict",
                        "--no-reflogs"]),
        ]
    return "\n".join(lines) + "\n"


def read_git(repository: Path, *arguments: str) -> bytes:
    """Read bounded local objects; all acquisition has already completed."""
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(repository.parent / "home"),
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    process = subprocess.Popen([*GIT, "--git-dir=" + str(repository), *arguments],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=environment)
    try:
        assert process.stdout is not None
        data = process.stdout.read(MAX_OUTPUT + 1)
        if len(data) > MAX_OUTPUT:
            raise ValueError("Git object output bound exceeded")
        if process.wait(timeout=30) != 0:
            raise ValueError("Git object read failed")
        return data
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def verify_inputs(work: Path) -> list[dict]:
    """Check both pinned object graphs and the only permitted submodule edge."""
    records = []
    for entry in INPUTS:
        repository = work / (entry["name"] + ".git")
        commit = read_git(repository, "cat-file", "commit", entry["commit"])
        validate_commit(commit, entry["commit"], entry["tree"])
        tree = read_git(repository, "cat-file", "tree", entry["tree"])
        if hashlib.sha1(b"tree " + str(len(tree)).encode() + b"\0" + tree).hexdigest() != entry["tree"]:
            raise ValueError("Git tree object identity mismatch")
        listed = read_git(repository, "ls-tree", "-rz", "--full-tree", entry["commit"])
        links = [row for row in listed.split(b"\0") if row.startswith(b"160000 ")]
        expected_links = ([] if entry["name"] == "testsuite" else [
            b"160000 commit " + INPUTS[1]["commit"].encode() + b"\ttests/testsuite"])
        if links != expected_links:
            raise ValueError("unexpected nested Git input")
        if entry["name"] == "wasm-tools":
            if read_git(repository, "show", entry["commit"] + ":.gitmodules") != SUBMODULES:
                raise ValueError("submodule source declaration changed")
        records.append({**entry, "source_entries": len(listed.rstrip(b"\0").split(b"\0")),
                        "commit_sha256": hashlib.sha256(commit).hexdigest(),
                        "tree_sha256": hashlib.sha256(tree).hexdigest(),
                        "git_files": inventory(repository)})
    return records


def acquire(destination: Path) -> None:
    """Acquire public Git inputs on CI under the existing physical supervisor."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("Git input restoration is restricted to Linux CI")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("new absolute destination under physical parent required")
    destination.mkdir(mode=0o700)
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("home", "empty-template"):
        (work / name).mkdir(mode=0o700)
    recipe = destination / "fetch.sh"
    recipe.write_text(script(work))
    receipt = {"format": "tobkiri.python313-git-inputs/1", "outcome": "pending",
               "git_client": fingerprint(Path("/usr/bin/git").resolve()),
               "recipe": fingerprint(recipe), "upstream_execution": False,
               "checkout_hooks": False, "cargo_invoked": False,
               "offline_environment_complete": False, "production_enabled": False}
    try:
        receipt["supervision"] = run_stage(
            "pinned-git-inputs", ["/bin/sh", str(recipe)], work, destination / "fetch")
        receipt["inputs"] = verify_inputs(work)
        receipt["outcome"] = "success"
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        (destination / "git-manifest.json").write_text(
            json.dumps(receipt, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    acquire(parser.parse_args().destination)
