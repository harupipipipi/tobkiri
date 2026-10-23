"""Replay the recorded native build-Python stage on an isolated Linux CI host.

The native compiler comes from the recorded CI image, not the historical
Debian compiler. A successful build records its own identity; it does not
replace any historical component pin or establish Wasm/guest acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent
STAGED_SHA256 = "f4edf03314a3d7fcd63b655153c64a3a07ea32fe7ef264e186e121acfacca367"
RECIPE_SHA256 = "06e41cabb1217df66c6e40f45cc1a1b108e5ffdc4645ef852d7bedb5b42c6599"
GIB = 1024 ** 3


def fingerprint(path: Path, maximum: int = GIB) -> dict:
    """Hash a bounded regular input without loading its contents into memory."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"regular input required: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while data := stream.read(64 * 1024):
            size += len(data)
            if size > maximum:
                raise ValueError("input size bound exceeded")
            digest.update(data)
    return {"bytes": size, "sha256": digest.hexdigest()}


def verify_tree(root: Path) -> dict:
    """Bind every staged byte and mode before executing its configure script."""
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("absolute physical source path required")
    manifest = root / "stage-manifest.json"
    identity = fingerprint(manifest, 4 * 1024 * 1024)
    if identity["sha256"] != STAGED_SHA256:
        raise ValueError("staged manifest identity mismatch")
    files = json.loads(manifest.read_bytes())["files"]
    observed = set()
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        name = path.relative_to(root).as_posix()
        if name == "stage-manifest.json":
            continue
        entry = files.get(name)
        if entry is None or not stat.S_ISREG(mode):
            raise ValueError("unlisted or non-regular staged input")
        if (fingerprint(path, 16 * 1024 * 1024)
                != {key: entry[key] for key in ("bytes", "sha256")}
                or stat.S_IMODE(mode) != int(entry["mode"], 8)):
            raise ValueError(f"staged input changed: {name}")
        observed.add(name)
    if observed != set(files):
        raise ValueError("staged input missing")
    return {**identity, "files": len(observed)}


def sandbox_command(source: Path, work: Path, recipe: Path,
                    readonly: tuple[tuple[Path, str], ...] = (), *,
                    address_space_bytes: int = 2 * GIB) -> list[str]:
    """Expose only read-only tools/source and this stage's private scratch."""
    if address_space_bytes not in (2 * GIB, 4 * GIB, 8 * GIB):
        raise ValueError("only recorded native, Rust and component-stage address-space limits allowed")
    command = [
        "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--cap-drop", "ALL", "--clearenv",
    ]
    for directory in ("/usr", "/bin", "/lib", "/lib64", "/etc/alternatives"):
        command += ["--ro-bind", directory, directory]
    command += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", "/source", "--ro-bind", str(source / "cpython"),
        "/source/cpython", "--ro-bind", str(recipe), "/recipe.sh",
        "--bind", str(work), "/work",
    ]
    for path, mount in readonly:
        command += ["--ro-bind", str(path), mount]
    command += [
        "--chdir", "/work",
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LC_ALL", "C",
        "--setenv", "CONFIG_SITE", "/dev/null", "--setenv", "TMPDIR", "/tmp",
        "--setenv", "CC", "/usr/bin/gcc", "--setenv", "CXX", "/usr/bin/g++",
        "--", "/usr/bin/prlimit", f"--as={address_space_bytes}", f"--fsize={GIB}",
        "--core=0", "--", "/bin/sh", "/recipe.sh",
    ]
    return command


def run(source: Path, destination: Path) -> None:
    """Build only after complete input checks; retain separate stage evidence."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("this resource-intensive stage is restricted to Linux CI")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("new absolute destination under a physical parent required")
    # Exclusive creation also rejects an existing symlink; never remove user data.
    destination.mkdir(mode=0o700)
    inputs = verify_tree(source)
    recipe = HERE / "trials/2026-10-19/native-build-python.sh"
    recipe_identity = fingerprint(recipe)
    if recipe_identity["sha256"] != RECIPE_SHA256:
        raise ValueError("recorded native recipe changed")
    work = destination / "work"
    work.mkdir(mode=0o700)
    command = sandbox_command(source, work, recipe)
    tool_names = ("bwrap", "gcc", "g++", "as", "ld", "make", "dash", "mawk",
                  "sed", "grep", "prlimit")
    tools = {name: {"path": str(path), **fingerprint(path)}
             for name in tool_names
             for path in [Path(f"/usr/bin/{name}").resolve(strict=True)]}
    packages = subprocess.check_output(
        ["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Version}\n"],
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, timeout=30)
    (destination / "host-packages.txt").write_bytes(packages)
    receipt = {
        "format": "tobkiri.python313-native-rebuild/1", "outcome": "pending",
        "source": inputs, "recipe": recipe_identity, "tools": tools,
        "ci_image": {key: os.environ.get(key) for key in ("ImageOS", "ImageVersion")},
        "host_packages": fingerprint(destination / "host-packages.txt"),
        "command": command, "guest_execution": False, "componentization": False,
        "production_enabled": False, "historical_binary_identity_claimed": False,
        "native_build_python_only": True,
    }
    receipt_path = destination / "native-build.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    supervisor = HERE / "trials/2026-10-20-current-caller/supervise.py"
    result = subprocess.run([
        sys.executable, "-I", "-B", str(supervisor), "--work", str(work),
        "--receipt", str(destination / "supervision.json"),
        "--log", str(destination / "build.log"), "--", *command,
    ], check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    receipt["outcome"] = "failed"
    receipt["supervisor_exit_code"] = result.returncode
    try:
        if result.returncode != 0:
            with (destination / "build.log").open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 8192))
                print(log.read(8192).decode("utf-8", errors="replace"))
            raise RuntimeError("isolated native build failed; inspect build.log")
        supervision = json.loads((destination / "supervision.json").read_text())
        if supervision["outcome"] != "success":
            raise RuntimeError("native stage physical cleanup is not confirmed")
        receipt["source_after"] = verify_tree(source)
        receipt["python"] = fingerprint(work / "native2/python")
        receipt["outcome"] = "success"
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.destination)
