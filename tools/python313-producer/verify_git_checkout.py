"""Compare Cargo's checkout files with the already authenticated bare Git inputs."""
import hashlib
import os
from pathlib import Path
import stat

from fetch_git import INPUTS, read_git

MAX_FILES = 20000
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024


def verify_file(path: Path, size: int, mode: str, object_id: str) -> None:
    """Hash a physical regular file with Git's exact blob framing."""
    if not 0 <= size <= MAX_FILE or path.parent.resolve() != path.parent:
        raise ValueError("Git checkout file bounds or physical parent mismatch")
    metadata = path.lstat()
    expected_mode = 0o755 if mode == "100755" else 0o644
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size
            or stat.S_IMODE(metadata.st_mode) != expected_mode):
        raise ValueError("Git checkout file type, mode or size changed")
    digest = hashlib.sha1(b"blob " + str(size).encode() + b"\0")
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            count += len(chunk)
            if count > size:
                raise ValueError("Git checkout file grew")
            digest.update(chunk)
    if count != size or digest.hexdigest() != object_id:
        raise ValueError("Git checkout bytes differ from pinned blob")


def verify_checkout(bare: Path, checkout: Path, pin: dict, git_work: Path,
                    totals: dict) -> None:
    """Check all source files and the one explicit nested testsuite checkout."""
    if not checkout.is_dir() or checkout.resolve() != checkout:
        raise ValueError("physical Cargo checkout required")
    rows = read_git(bare, "ls-tree", "-rz", "-l", "--full-tree", pin["commit"])
    expected = set()
    submodules = set()
    for row in rows.rstrip(b"\0").split(b"\0"):
        header, raw_name = row.split(b"\t", 1)
        mode, kind, object_id, raw_size = header.decode("ascii").split()
        name = raw_name.decode("utf-8")
        if (not name or "\\" in name or name.startswith("/")
                or any(part in ("", ".", "..", ".git") for part in name.split("/"))):
            raise ValueError("unsafe Git tree path")
        if name in expected or name in submodules:
            raise ValueError("duplicate Git tree path")
        if mode == "160000":
            if (pin["name"] != "wasm-tools" or name != "tests/testsuite"
                    or kind != "commit" or object_id != INPUTS[1]["commit"]):
                raise ValueError("unexpected Git submodule")
            submodules.add(name)
            verify_checkout(git_work / "testsuite.git", checkout / name,
                            INPUTS[1], git_work, totals)
            continue
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("unsupported Git checkout input kind")
        size = int(raw_size)
        totals["files"] += 1
        totals["bytes"] += size
        if totals["files"] > MAX_FILES or totals["bytes"] > MAX_BYTES:
            raise ValueError("Git checkout closure budget exceeded")
        verify_file(checkout / name, size, mode, object_id)
        expected.add(name)
    actual = set()
    for parent, directories, files in os.walk(checkout):
        for name in list(directories):
            path = Path(parent) / name
            relative = path.relative_to(checkout).as_posix()
            if path.is_symlink():
                raise ValueError("unexpected checkout directory link")
            if relative == ".git" or relative in submodules:
                directories.remove(name)
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(checkout).as_posix()
            if relative in (".git", ".cargo-ok"):
                if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 4096:
                    raise ValueError("unexpected Cargo checkout marker")
                continue
            actual.add(relative)
    if actual != expected:
        raise ValueError("Cargo checkout has missing or unlisted source files")


def verify_cache(cache: Path, git_work: Path) -> dict:
    """Require exactly one root dependency checkout; never select a loose glob."""
    roots = list((cache / "git/checkouts").glob("*/*"))
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("unexpected number of Cargo Git dependency checkouts")
    totals = {"files": 0, "bytes": 0}
    verify_checkout(git_work / "wasm-tools.git", roots[0], INPUTS[0], git_work, totals)
    return totals
