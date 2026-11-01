"""Restore the exact upstream wasi-libc wasm-tools build input on Linux CI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tarfile
import time
import zipfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from build_native import fingerprint

VERSION = "1.244.0"
PREFIX = "wasm-tools-" + VERSION + "-x86_64-linux"
URL = (
    "https://github.com/bytecodealliance/wasm-tools/releases/download/v"
    + VERSION
    + "/"
    + PREFIX
    + ".tar.gz"
)
ARCHIVE_BYTES = 5_932_679
ARCHIVE_SHA256 = "c3bf279c10e8ca37262773c829efd45423b5efeeb93ba1dc17baf25d9479074c"
LIMIT = 64 * 1024 * 1024
FORMAT = "tobkiri.ui-poll-build-tools/1"
CMAKE_VERSION = "3.31.10"
CMAKE_FILENAME = "cmake-3.31.10-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
CMAKE_URL = (
    "https://files.pythonhosted.org/packages/f4/2e/"
    "cefc60143950ca1a8dda26d4c8484e6fc406db16e7ca098ebae549de35f9/" + CMAKE_FILENAME
)
CMAKE_BYTES = 27_808_794
CMAKE_SHA256 = "2f766bb46367e5e0559fa33184653754bce044583a06014dcaebf8e6dff8a1f1"
CMAKE_EXECUTABLES = frozenset(
    "cmake/data/bin/" + name for name in ("cmake", "ctest", "cpack")
)


def cmake_entries(archive: Path, destination: Path | None = None) -> dict:
    """Derive every CMake wheel byte from its pin; optionally stage it as data."""
    if archive.lstat().st_nlink != 1 or fingerprint(archive, CMAKE_BYTES) != {
        "bytes": CMAKE_BYTES,
        "sha256": CMAKE_SHA256,
    }:
        raise ValueError("CMake wheel differs from its exact input pin")
    if destination is not None:
        destination.mkdir(mode=0o700)
    seen, files, total = set(), {}, 0
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 8192:
            raise ValueError("CMake wheel member count exceeded")
        for member in bundle.infolist():
            name = member.filename.rstrip("/")
            mode = member.external_attr >> 16
            allowed = (0, stat.S_IFDIR) if member.is_dir() else (0, stat.S_IFREG)
            if (
                not name
                or "\\" in name
                or ":" in name
                or "\0" in name
                or any(p in ("", ".", "..") for p in name.split("/"))
                or name.casefold() in seen
                or member.flag_bits & 1
                or stat.S_IFMT(mode) not in allowed
                or mode & 0o7000
                or not 0 <= member.file_size <= LIMIT
            ):
                raise ValueError("unsafe CMake wheel member")
            seen.add(name.casefold())
            if member.is_dir():
                if member.file_size:
                    raise ValueError("CMake wheel directory has content")
                if destination is not None:
                    (destination / name).mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            total += member.file_size
            if total > 128 * 1024 * 1024:
                raise ValueError("CMake wheel expanded bound exceeded")
            raw = bundle.read(member)
            if len(raw) != member.file_size:
                raise ValueError("CMake wheel member changed size")
            executable = name in CMAKE_EXECUTABLES
            if executable and (
                len(raw) < 20
                or raw[:6] != b"\x7fELF\x02\x01"
                or int.from_bytes(raw[18:20], "little") != 62
            ):
                raise ValueError("CMake tool is not a native Linux x86_64 executable")
            files[name] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": 0o700 if executable else 0o600,
            }
            if destination is not None:
                path = destination / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with path.open("xb") as output:
                    output.write(raw)
                path.chmod(files[name]["mode"])
    if not CMAKE_EXECUTABLES <= files.keys():
        raise ValueError("CMake wheel executable inventory is incomplete")
    return files


def selected(archive: Path) -> tuple[bytes, dict]:
    """Derive the executable from the complete pinned archive, without running it."""
    if archive.lstat().st_nlink != 1 or fingerprint(archive, ARCHIVE_BYTES) != {
        "bytes": ARCHIVE_BYTES,
        "sha256": ARCHIVE_SHA256,
    }:
        raise ValueError("wasm-tools archive differs from its upstream pin")
    seen = set()
    total = 0
    executable = None
    files = {}
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (
                len(seen) >= 128
                or name in seen
                or "\\" in name
                or "\0" in name
                or parts[0] != PREFIX
                or any(p in ("", ".", "..") for p in parts)
                or member.mode & ~0o777
            ):
                raise ValueError("unsafe wasm-tools archive member")
            seen.add(name)
            if member.type == tarfile.DIRTYPE:
                if member.size:
                    raise ValueError("wasm-tools directory has content")
                continue
            if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                raise ValueError("wasm-tools archive has a link or special member")
            if not 0 <= member.size <= LIMIT:
                raise ValueError("wasm-tools member exceeds its bound")
            total += member.size
            if total > LIMIT:
                raise ValueError("wasm-tools expanded bound exceeded")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError("wasm-tools member is unreadable")
            with stream:
                raw = stream.read(member.size + 1)
            if len(raw) != member.size:
                raise ValueError("wasm-tools archive member changed size")
            files[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            if name == PREFIX + "/wasm-tools":
                if not member.mode & 0o111:
                    raise ValueError("wasm-tools executable lacks its upstream mode")
                executable = raw
    if (
        executable is None
        or len(executable) < 20
        or executable[:6] != b"\x7fELF\x02\x01"
        or int.from_bytes(executable[18:20], "little") != 62
    ):
        raise ValueError("wasm-tools is not the pinned Linux x86_64 executable")
    return executable, files


def receipt(archive: Path, executable: bytes, members: dict, cmake: dict) -> dict:
    return {
        "format": FORMAT,
        "version": VERSION,
        "outcome": "success",
        "archive": {"url": URL, **fingerprint(archive, ARCHIVE_BYTES)},
        "members": members,
        "executable": {
            "bytes": len(executable),
            "sha256": hashlib.sha256(executable).hexdigest(),
        },
        "cmake": {
            "version": CMAKE_VERSION,
            "url": CMAKE_URL,
            "archive": {"bytes": CMAKE_BYTES, "sha256": CMAKE_SHA256},
            "source_root": "cmake",
            "executable": "cmake/data/bin/cmake",
            "files": cmake,
        },
        "upstream_execution": False,
        "production_enabled": False,
    }


def verify(destination: Path) -> dict:
    if not destination.is_absolute() or destination.resolve() != destination:
        raise ValueError("physical build-tool root required")
    if {p.name for p in destination.iterdir()} != {
        "source.tar.gz",
        "wasm-tools",
        "tool-inputs.json",
        "cmake.whl",
        "cmake",
    }:
        raise ValueError("unexpected libc build-tool input")
    archive = destination / "source.tar.gz"
    executable, members = selected(archive)
    cmake = cmake_entries(destination / "cmake.whl")
    expected = receipt(archive, executable, members, cmake)
    observed = set()
    cmake_root = destination / "cmake"
    if not stat.S_ISDIR(cmake_root.lstat().st_mode):
        raise ValueError("CMake tree is not physical")
    for path in cmake_root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        relative = path.relative_to(cmake_root).as_posix()
        entry = cmake.get(relative)
        if (
            entry is None
            or not stat.S_ISREG(mode)
            or path.lstat().st_nlink != 1
            or stat.S_IMODE(mode) != entry["mode"]
            or fingerprint(path, LIMIT) != {k: entry[k] for k in ("bytes", "sha256")}
        ):
            raise ValueError("CMake staged file differs from its pinned wheel")
        observed.add(relative)
    if observed != set(cmake):
        raise ValueError("CMake tree is incomplete")
    path = destination / "wasm-tools"
    if (
        path.lstat().st_nlink != 1
        or stat.S_IMODE(path.lstat().st_mode) != 0o700
        or fingerprint(path, LIMIT) != expected["executable"]
    ):
        raise ValueError("libc build-tool executable changed")
    recorded = destination / "tool-inputs.json"
    fingerprint(recorded, 4 * 1024 * 1024)
    if json.loads(recorded.read_bytes()) != expected:
        raise ValueError("libc build-tool receipt differs from pinned archive")
    return expected


def download(
    url: str, archive: Path, expected_bytes: int, expected_sha256: str
) -> None:
    request = Request(url, headers={"User-Agent": "Tobkiri-libc-build-input"})
    deadline = time.monotonic() + 120
    with urlopen(request, timeout=30) as response:
        final = urlsplit(response.geturl())
        if (
            final.scheme != "https"
            or final.username
            or final.password
            or final.port
            or final.hostname
            not in {
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
                "files.pythonhosted.org",
            }
        ):
            raise ValueError("unexpected pinned build-tool release redirect")
        total = 0
        with archive.open("xb") as output:
            archive.chmod(0o600)
            while chunk := response.read(min(65536, expected_bytes - total + 1)):
                total += len(chunk)
                if total > expected_bytes or time.monotonic() > deadline:
                    raise ValueError("pinned build-tool download bound exceeded")
                output.write(chunk)
    if fingerprint(archive, expected_bytes) != {
        "bytes": expected_bytes,
        "sha256": expected_sha256,
    }:
        raise ValueError("pinned build-tool download identity differs")


def run(destination: Path) -> dict:
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("libc build-tool acquisition is restricted to Linux CI")
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or not destination.parent.is_dir()
        or os.path.lexists(destination)
        or shutil.disk_usage(destination.parent).free < 8 * 1024**3
    ):
        raise ValueError("new private tool directory and original disk floor required")
    destination.mkdir(mode=0o700)
    archive = destination / "source.tar.gz"
    download(URL, archive, ARCHIVE_BYTES, ARCHIVE_SHA256)
    download(CMAKE_URL, destination / "cmake.whl", CMAKE_BYTES, CMAKE_SHA256)
    executable, members = selected(archive)
    path = destination / "wasm-tools"
    path.write_bytes(executable)
    path.chmod(0o700)
    cmake = cmake_entries(destination / "cmake.whl", destination / "cmake")
    result = receipt(archive, executable, members, cmake)
    manifest = destination / "tool-inputs.json"
    manifest.write_text(json.dumps(result, indent=2) + "\n")
    manifest.chmod(0o600)
    return verify(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify(args.destination) if args.verify else run(args.destination)
    print(json.dumps({"version": result["version"], "upstream_execution": False}))
