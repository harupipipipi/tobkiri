"""Acquire one pinned wasi-libc source archive for CI-only UI poll analysis.

The archive and staged source are treated only as data. No compiler, build
system, application, or guest is invoked by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import time
from typing import BinaryIO
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

COMMIT = "161b3195fc2558d2b1ba3eb9ffae3b2b47407623"
PREFIX = "wasi-libc-" + COMMIT
URL = "https://codeload.github.com/WebAssembly/wasi-libc/tar.gz/" + COMMIT
ARCHIVE_BYTES = 1_657_525
ARCHIVE_SHA256 = "3bc20d349e6f9fbc4602fb29c80706a25b2875569c3320b11f7f4cf291903f9c"
POLL_PATH = "libc-bottom-half/cloudlibc/src/libc/poll/poll.c"
POLL_SHA256 = "22b6fa1ef5462b5a6d3b91b2c26e62bb857c6aa964f5f1047d2c45ffd3c4c0c9"

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
DISK_FLOOR = 8 * 1024**3
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_DEADLINE_SECONDS = 120
READ_BYTES = 64 * 1024
FORMAT = "tobkiri.python313-ui-poll-libc-inputs/1"


def _archive_identity(path: Path) -> dict[str, object]:
    """Hash a bounded physical archive without following links."""
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("pinned wasi-libc archive is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("pinned wasi-libc archive must be an unlinked regular file")
    if info.st_size != ARCHIVE_BYTES or info.st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("pinned wasi-libc archive size differs")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != info.st_size
                or opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
            ):
                raise ValueError("pinned wasi-libc archive changed while opening")
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(min(READ_BYTES, MAX_ARCHIVE_BYTES - size + 1)):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("wasi-libc archive exceeds its compressed bound")
                digest.update(chunk)
    except OSError as error:
        raise ValueError("pinned wasi-libc archive is unavailable") from error
    identity = {"bytes": size, "sha256": digest.hexdigest()}
    if size != ARCHIVE_BYTES or identity["sha256"] != ARCHIVE_SHA256:
        raise ValueError("pinned wasi-libc archive identity differs")
    return identity


def _validate_final_url(final_url: str) -> None:
    try:
        parsed = urlsplit(final_url)
    except ValueError as error:
        raise ValueError("wasi-libc download URL is invalid") from error
    expected_path = f"/WebAssembly/wasi-libc/tar.gz/{COMMIT}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "codeload.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
        or final_url != URL
    ):
        raise ValueError("wasi-libc redirect differs from the pinned HTTPS URL")


def _download_archive(destination: Path) -> dict[str, object]:
    """Stream the exact archive into a private temporary name and verify it."""
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    request = Request(URL, headers={"User-Agent": "Tobkiri-source-replay"})
    temporary = destination.with_name(destination.name + ".part")
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            _validate_final_url(response.geturl())
            digest = hashlib.sha256()
            size = 0
            try:
                with temporary.open("xb") as output:
                    temporary.chmod(0o600)
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError("wasi-libc download deadline exceeded")
                        chunk = response.read(min(READ_BYTES, ARCHIVE_BYTES - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > ARCHIVE_BYTES or size > MAX_ARCHIVE_BYTES:
                            raise ValueError(
                                "wasi-libc download exceeds its pinned size"
                            )
                        digest.update(chunk)
                        output.write(chunk)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ValueError("wasi-libc pinned download failed") from error
    if size != ARCHIVE_BYTES or digest.hexdigest() != ARCHIVE_SHA256:
        temporary.unlink(missing_ok=True)
        raise ValueError("wasi-libc download differs from its pinned archive")
    os.rename(temporary, destination)
    return _archive_identity(destination)


def _read_member(stream: BinaryIO, size: int, name: str) -> bytes:
    content = bytearray()
    remaining = size
    while remaining:
        chunk = stream.read(min(READ_BYTES, remaining))
        if not chunk:
            raise ValueError(f"truncated wasi-libc archive member: {name}")
        content.extend(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        raise ValueError(f"oversized wasi-libc archive member: {name}")
    return bytes(content)


def _archive_entries(
    archive_path: Path,
) -> tuple[dict[str, tuple[bytes, bool]], list[str], dict]:
    """Stream a pinned-layout tar.gz and derive its complete safe source tree."""
    archive = _archive_identity(archive_path)
    files: dict[str, tuple[bytes, bool]] = {}
    explicit_files: set[str] = set()
    explicit_directories: set[str] = set()
    directories: set[str] = set()
    members = 0
    expanded = 0
    root_seen = False

    try:
        with tarfile.open(archive_path, mode="r|gz") as bundle:
            for member in bundle:
                members += 1
                if members > MAX_MEMBERS:
                    raise ValueError("wasi-libc archive member bound exceeded")
                raw_name = member.name
                if (
                    not isinstance(raw_name, str)
                    or "\\" in raw_name
                    or "\x00" in raw_name
                ):
                    raise ValueError("wasi-libc archive path is invalid")
                if member.isdir() and raw_name.endswith("/"):
                    raw_name = raw_name[:-1]
                path = PurePosixPath(raw_name)
                parts = raw_name.split("/")
                if (
                    not raw_name
                    or path.is_absolute()
                    or any(part in ("", ".", "..") for part in parts)
                    or parts[0] != PREFIX
                ):
                    raise ValueError(f"unsafe wasi-libc archive path: {member.name}")
                if len(parts) == 1:
                    relative = ""
                    if member.type != tarfile.DIRTYPE:
                        raise ValueError("wasi-libc archive root is not a directory")
                    if root_seen:
                        raise ValueError("duplicate wasi-libc archive root")
                    root_seen = True
                else:
                    relative = "/".join(parts[1:])

                if member.mode < 0 or member.mode & ~0o777:
                    raise ValueError("wasi-libc archive member has special mode bits")
                if member.type == tarfile.DIRTYPE:
                    if member.size != 0:
                        raise ValueError("wasi-libc directory member has content")
                    if relative:
                        if (
                            relative in explicit_directories
                            or relative in explicit_files
                        ):
                            raise ValueError("duplicate or colliding wasi-libc path")
                        for parent in PurePosixPath(relative).parents:
                            parent_name = str(parent)
                            if parent_name == ".":
                                continue
                            if parent_name in explicit_files:
                                raise ValueError("wasi-libc file/directory collision")
                            directories.add(parent_name)
                        explicit_directories.add(relative)
                        directories.add(relative)
                elif member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    if not relative:
                        raise ValueError("wasi-libc archive root is not a regular file")
                    if relative in explicit_files or relative in directories:
                        raise ValueError("duplicate or colliding wasi-libc path")
                    if not 0 <= member.size <= MAX_FILE_BYTES:
                        raise ValueError(
                            f"wasi-libc file exceeds its bound: {relative}"
                        )
                    expanded += member.size
                    if expanded > MAX_EXPANDED_BYTES:
                        raise ValueError("wasi-libc expanded source exceeds its bound")
                    for parent in PurePosixPath(relative).parents:
                        parent_name = str(parent)
                        if parent_name == ".":
                            continue
                        if parent_name in explicit_files:
                            raise ValueError("wasi-libc file/directory collision")
                        directories.add(parent_name)
                    incoming = bundle.extractfile(member)
                    if incoming is None:
                        raise ValueError(
                            f"wasi-libc source member is unreadable: {relative}"
                        )
                    with incoming:
                        data = _read_member(incoming, member.size, relative)
                    files[relative] = (data, bool(member.mode & 0o111))
                    explicit_files.add(relative)
                else:
                    raise ValueError(
                        f"unsupported wasi-libc archive member: {relative}"
                    )

                if relative and relative in explicit_files:
                    for parent in PurePosixPath(relative).parents:
                        parent_name = str(parent)
                        if parent_name != "." and parent_name in explicit_files:
                            raise ValueError("wasi-libc file/directory collision")
        if not root_seen:
            raise ValueError("wasi-libc archive root directory is missing")
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError("invalid pinned wasi-libc source archive") from error

    poll = files.get(POLL_PATH)
    if poll is None or hashlib.sha256(poll[0]).hexdigest() != POLL_SHA256:
        raise ValueError("wasi-libc poll.c identity differs from the pinned source")
    if _archive_identity(archive_path) != archive:
        raise ValueError("wasi-libc archive changed while being inspected")
    return files, sorted(directories), archive


def _file_inventory(files: dict[str, tuple[bytes, bool]]) -> dict[str, dict]:
    return {
        path: {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "executable": executable,
            "mode": "0o700" if executable else "0o600",
        }
        for path, (data, executable) in sorted(files.items())
    }


def _pin() -> dict[str, object]:
    if (
        len(COMMIT) != 40
        or any(character not in "0123456789abcdef" for character in COMMIT)
        or PREFIX != "wasi-libc-" + COMMIT
        or URL != "https://codeload.github.com/WebAssembly/wasi-libc/tar.gz/" + COMMIT
        or ARCHIVE_BYTES <= 0
        or ARCHIVE_BYTES > MAX_ARCHIVE_BYTES
        or POLL_PATH.startswith("/")
        or "\\" in POLL_PATH
        or ".." in PurePosixPath(POLL_PATH).parts
        or re.fullmatch(r"[0-9a-f]{64}", ARCHIVE_SHA256) is None
        or re.fullmatch(r"[0-9a-f]{64}", POLL_SHA256) is None
    ):
        raise ValueError("wasi-libc source pin is invalid")
    return {
        "commit": COMMIT,
        "prefix": PREFIX,
        "url": URL,
        "archive_bytes": ARCHIVE_BYTES,
        "archive_sha256": ARCHIVE_SHA256,
    }


def _receipt(
    archive: dict, files: dict[str, tuple[bytes, bool]], directories: list[str]
) -> dict:
    poll = files[POLL_PATH][0]
    return {
        "format": FORMAT,
        "outcome": "success",
        "complete": True,
        "source_pin": _pin(),
        "archive": {"path": "source.tar.gz", **archive},
        "source_root": "source",
        "directories": directories,
        "files": _file_inventory(files),
        "poll": {
            "path": POLL_PATH,
            "bytes": len(poll),
            "sha256": hashlib.sha256(poll).hexdigest(),
        },
        "hashes": "integrity-only-not-authentication",
        "source_only": True,
        "build_performed": False,
        "production_enabled": False,
    }


def _receipt_bytes(receipt: dict) -> bytes:
    return (
        json.dumps(receipt, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_source_tree(
    source_root: Path,
    files: dict[str, tuple[bytes, bool]],
    directories: list[str],
) -> None:
    source_root.mkdir(mode=0o700)
    source_root.chmod(0o700)
    for relative in sorted(directories, key=lambda item: (item.count("/"), item)):
        directory = source_root / relative
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    for relative, (data, executable) in sorted(files.items()):
        output = source_root / relative
        with output.open("xb") as stream:
            stream.write(data)
        output.chmod(0o700 if executable else 0o600)


def run(destination: Path) -> dict:
    """Download and stage only the pinned wasi-libc source on Linux CI."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("wasi-libc source acquisition is restricted to Linux CI")
    destination = Path(destination)
    parent = destination.parent
    if (
        not destination.is_absolute()
        or not parent.is_dir()
        or parent.resolve() != parent
        or os.path.lexists(destination)
        or shutil.disk_usage(parent).free < DISK_FLOOR
    ):
        raise ValueError("physical new destination and 8 GiB disk floor required")
    _pin()

    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    archive_path = destination / "source.tar.gz"
    archive = _download_archive(archive_path)
    files, directories, archive = _archive_entries(archive_path)
    source_root = destination / "source"
    _write_source_tree(source_root, files, directories)
    receipt = _receipt(archive, files, directories)
    receipt_raw = _receipt_bytes(receipt)
    if len(receipt_raw) > MAX_MANIFEST_BYTES:
        raise ValueError("wasi-libc source manifest exceeds its size bound")
    receipt_path = destination / "source-inputs.json"
    with receipt_path.open("xb") as output:
        output.write(receipt_raw)
    receipt_path.chmod(0o600)
    return receipt


def _read_physical_file(path: Path, maximum: int, expected_mode: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(
            f"wasi-libc staged file is unavailable: {path.name}"
        ) from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > maximum
        or stat.S_IMODE(info.st_mode) != expected_mode
    ):
        raise ValueError(f"wasi-libc staged file is unsafe: {path.name}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != info.st_size
                or opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
            ):
                raise ValueError(f"wasi-libc staged file changed: {path.name}")
            data = stream.read(maximum + 1)
    except OSError as error:
        raise ValueError(
            f"wasi-libc staged file is unavailable: {path.name}"
        ) from error
    if len(data) != info.st_size or len(data) > maximum:
        raise ValueError(f"wasi-libc staged file size changed: {path.name}")
    return data


def _read_source_tree(
    source_root: Path,
) -> tuple[dict[str, tuple[bytes, bool]], list[str]]:
    try:
        root_info = source_root.lstat()
    except OSError as error:
        raise ValueError("wasi-libc source directory is unavailable") from error
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
        raise ValueError("wasi-libc source root is not a private physical directory")

    files: dict[str, tuple[bytes, bool]] = {}
    directories: list[str] = []
    total = 0
    count = 0

    def visit(directory: Path, relative: str) -> None:
        nonlocal total, count
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ValueError(
                f"wasi-libc source directory cannot be read: {relative}"
            ) from error
        for child in children:
            count += 1
            if count > MAX_MEMBERS:
                raise ValueError("wasi-libc staged member bound exceeded")
            rel = f"{relative}/{child.name}" if relative else child.name
            try:
                info = child.lstat()
            except OSError as error:
                raise ValueError(
                    f"wasi-libc source entry is unavailable: {rel}"
                ) from error
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError(f"wasi-libc source directory mode differs: {rel}")
                directories.append(rel)
                visit(child, rel)
            elif stat.S_ISREG(info.st_mode):
                mode = stat.S_IMODE(info.st_mode)
                if mode not in (0o600, 0o700):
                    raise ValueError(f"wasi-libc source file mode differs: {rel}")
                if info.st_nlink != 1:
                    raise ValueError(f"wasi-libc source file is hard-linked: {rel}")
                if not 0 <= info.st_size <= MAX_FILE_BYTES:
                    raise ValueError(f"wasi-libc source file exceeds bound: {rel}")
                total += info.st_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("wasi-libc staged source exceeds expanded bound")
                data = _read_physical_file(child, MAX_FILE_BYTES, mode)
                files[rel] = (data, mode == 0o700)
            else:
                raise ValueError(
                    f"wasi-libc source contains a link or special entry: {rel}"
                )

    visit(source_root, "")
    return files, sorted(directories)


def verify(destination: Path) -> dict:
    """Re-derive source inventory from the archive and compare every staged byte."""
    destination = Path(destination)
    if (
        not destination.is_absolute()
        or destination.resolve() != destination
        or destination.parent.resolve() != destination.parent
    ):
        raise ValueError("absolute physical wasi-libc destination required")
    try:
        info = destination.lstat()
    except OSError as error:
        raise ValueError("wasi-libc destination is unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("wasi-libc destination is not a private physical directory")
    _pin()

    expected_root_entries = {"source.tar.gz", "source", "source-inputs.json"}
    try:
        if {item.name for item in destination.iterdir()} != expected_root_entries:
            raise ValueError("wasi-libc destination contains unexpected entries")
    except OSError as error:
        raise ValueError("wasi-libc destination cannot be enumerated") from error

    archive_path = destination / "source.tar.gz"
    archive_info = archive_path.lstat()
    if stat.S_IMODE(archive_info.st_mode) != 0o600:
        raise ValueError("wasi-libc retained archive mode differs")
    selected, directories, archive = _archive_entries(archive_path)
    actual_files, actual_directories = _read_source_tree(destination / "source")
    if actual_files != selected or actual_directories != directories:
        raise ValueError("staged wasi-libc source differs from its pinned archive")

    receipt_path = destination / "source-inputs.json"
    receipt_raw = _read_physical_file(receipt_path, MAX_MANIFEST_BYTES, 0o600)
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("wasi-libc source receipt is invalid") from error
    expected = _receipt(archive, selected, directories)
    if receipt != expected or receipt_raw != _receipt_bytes(expected):
        raise ValueError("wasi-libc source receipt differs from the retained archive")
    return {
        "verified": True,
        "source_root": "source",
        "files": len(selected),
        "directories": len(directories),
        "archive": archive,
        "poll": expected["poll"],
        "source_only": True,
        "build_performed": False,
        "production_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify(args.destination) if args.verify else run(args.destination)
    print(
        json.dumps(
            {
                "verified": result.get("verified", True),
                "source_root": result["source_root"],
                "files": result["files"]
                if type(result["files"]) is int
                else len(result["files"]),
                "build_performed": False,
                "production_enabled": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
