"""Acquire pinned CPython schema dependency source as data on Linux CI.

The archive and extracted source are never imported or executed here. Only the
Only the closed source selection below is published; the rest of the archive remains
compressed under the private CI destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile

from build_native import fingerprint
import fetch_rpds
import stage_sources
import verify_source

LOCKED_PREFIX = "Python-3.13.16"
LOCKED_FILENAME = "Python-3.13.16.tar.xz"
LOCKED_URL = "https://www.python.org/ftp/python/3.13.16/Python-3.13.16.tar.xz"
LOCKED_SHA256 = "f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d"
MAX_ARCHIVE_BYTES = verify_source.MAX_ARCHIVE
MAX_MEMBERS = stage_sources.MAX_MEMBERS
MAX_SELECTED_FILE_BYTES = 4 * 1024 * 1024
MAX_SELECTED_TOTAL_BYTES = 4 * 1024 * 1024
DISK_FLOOR = 8 * 1024**3
FORMAT = "tobkiri.python313-stdlib-inputs/1"

SELECTED_FILES = (
    "logging/__init__.py",
    "logging/config.py",
    "logging/handlers.py",
    "traceback.py",
    "linecache.py",
    "textwrap.py",
    "_colorize.py",
    "string.py",
    "tokenize.py",
    "token.py",
    "subprocess.py",
    "locale.py",
    "signal.py",
    "selectors.py",
    # The unchanged format corpus calls datetime.strptime after restoration.
    # Its lazy parser imports calendar; both are original CPython source DATA.
    "_strptime.py",
    "calendar.py",
    # Distribution.files() lazily parses the unchanged wheel RECORD via csv.
    "csv.py",
    "quopri.py",
    # Distribution.metadata() lazily uses the original email metadata adapter.
    "importlib/metadata/_adapters.py",
    "importlib/metadata/_text.py",
    "email/__init__.py",
    "email/_encoded_words.py",
    "email/_header_value_parser.py",
    "email/_parseaddr.py",
    "email/_policybase.py",
    "email/base64mime.py",
    "email/charset.py",
    "email/contentmanager.py",
    "email/encoders.py",
    "email/errors.py",
    "email/feedparser.py",
    "email/generator.py",
    "email/header.py",
    "email/headerregistry.py",
    "email/iterators.py",
    "email/message.py",
    "email/parser.py",
    "email/policy.py",
    "email/quoprimime.py",
    "email/utils.py",
)
_ARCHIVE_MEMBERS = {
    f"{LOCKED_PREFIX}/Lib/{relative}": relative for relative in SELECTED_FILES
}


def _source_pin() -> dict:
    """Return the existing CPython source archive pin without resolving anything."""
    pin = stage_sources.ARCHIVES.get("cpython")
    expected = {
        "filename": LOCKED_FILENAME,
        "sha256": LOCKED_SHA256,
        "prefix": LOCKED_PREFIX,
        "url": LOCKED_URL,
    }
    if pin != expected or verify_source.ARCHIVE_SHA256 != LOCKED_SHA256:
        raise ValueError("CPython stdlib source pin differs from stage_sources")
    return dict(pin)


def _source_members(archive_path: Path, pin: dict) -> tuple[dict, dict]:
    """Stream the pinned tar.xz and capture only the selected regular files."""
    archive_identity = fingerprint(archive_path, MAX_ARCHIVE_BYTES)
    if archive_identity["sha256"] != pin["sha256"]:
        raise ValueError("CPython stdlib archive differs from its source pin")

    selected: dict[str, tuple[bytes, int]] = {}
    kinds: dict[str, str] = {}
    seen: set[str] = set()
    selected_total = 0
    members = 0
    try:
        with tarfile.open(archive_path, mode="r|xz") as bundle:
            for member in bundle:
                members += 1
                if members > MAX_MEMBERS:
                    raise ValueError("CPython stdlib archive member bound exceeded")
                name = member.name.rstrip("/")
                path = PurePosixPath(name)
                parts = name.split("/")
                if (
                    not name
                    or path.is_absolute()
                    or "\\" in name
                    or any(part in ("", ".", "..") for part in parts)
                    or parts[0] != pin["prefix"]
                ):
                    raise ValueError(
                        f"unsafe CPython stdlib archive path: {member.name}"
                    )
                if name in seen:
                    raise ValueError(f"duplicate CPython stdlib archive member: {name}")
                seen.add(name)

                relative = "/".join(parts[1:])
                kind = (
                    "file"
                    if member.isfile()
                    else "directory"
                    if member.isdir()
                    else "link"
                    if member.issym() or member.islnk()
                    else "special"
                )
                if kind == "special":
                    raise ValueError(
                        f"unsupported CPython stdlib archive member: {name}"
                    )
                if not relative and kind != "directory":
                    raise ValueError("CPython stdlib archive root is not a directory")
                parents = PurePosixPath(relative).parents
                if any(
                    kinds.get(str(parent)) in ("file", "link", "special")
                    for parent in parents
                    if str(parent) != "."
                ):
                    raise ValueError("CPython stdlib archive file/directory collision")
                if kind == "directory" and member.size:
                    raise ValueError("CPython stdlib directory has content")
                if member.mode & ~0o777:
                    raise ValueError("CPython stdlib archive member has special mode")
                if kind == "file" and any(
                    existing.startswith(relative + "/") for existing in kinds
                ):
                    raise ValueError("CPython stdlib archive file/directory collision")
                kinds[relative] = kind

                output_name = _ARCHIVE_MEMBERS.get(name)
                if output_name is None:
                    continue
                if kind != "file":
                    raise ValueError(
                        f"selected CPython stdlib member is not regular: {name}"
                    )
                if member.mode not in (0o644, 0o755):
                    raise ValueError(
                        f"selected CPython stdlib mode is unsupported: {name}"
                    )
                if not 0 <= member.size <= MAX_SELECTED_FILE_BYTES:
                    raise ValueError(
                        f"selected CPython stdlib file exceeds its bound: {name}"
                    )
                selected_total += member.size
                if selected_total > MAX_SELECTED_TOTAL_BYTES:
                    raise ValueError(
                        "selected CPython stdlib files exceed their total bound"
                    )
                incoming = bundle.extractfile(member)
                if incoming is None:
                    raise ValueError(
                        f"selected CPython stdlib member is unreadable: {name}"
                    )
                content = bytearray()
                remaining = member.size
                with incoming:
                    while remaining:
                        chunk = incoming.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError(f"truncated CPython stdlib member: {name}")
                        content.extend(chunk)
                        remaining -= len(chunk)
                    if incoming.read(1):
                        raise ValueError(f"oversized CPython stdlib member: {name}")
                selected[output_name] = (bytes(content), member.mode)
    except (tarfile.TarError, EOFError, lzma.LZMAError, OSError) as error:
        raise ValueError("invalid pinned CPython stdlib archive") from error

    if set(selected) != set(SELECTED_FILES):
        missing = sorted(set(SELECTED_FILES) - selected.keys())
        raise ValueError(f"pinned CPython stdlib files are missing: {missing}")
    if fingerprint(archive_path, MAX_ARCHIVE_BYTES) != archive_identity:
        raise ValueError("CPython stdlib archive changed while it was inspected")
    files = {
        name: {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": oct(mode),
        }
        for name, (data, mode) in sorted(selected.items())
    }
    return selected, {"archive": archive_identity, "files": files}


def _read_source_tree(source_root: Path) -> dict[str, tuple[bytes, int]]:
    """Read only the fixed file tree, checking all size bounds before reading."""
    expected_children: dict[str, set[str]] = {"": set()}
    for relative in SELECTED_FILES:
        parts = PurePosixPath(relative).parts
        parent_parts: list[str] = []
        for part in parts[:-1]:
            parent = "/".join(parent_parts)
            child = "/".join((*parent_parts, part))
            expected_children.setdefault(parent, set()).add(part)
            expected_children.setdefault(child, set())
            parent_parts.append(part)
        expected_children["/".join(parent_parts)].add(parts[-1])

    for relative, children in expected_children.items():
        directory = source_root / relative if relative else source_root
        try:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ValueError(
                    f"CPython source directory is not physical: {relative}"
                )
            actual_children = set()
            for child in directory.iterdir():
                actual_children.add(child.name)
                if len(actual_children) > len(children):
                    raise ValueError("CPython source tree has unsupported entries")
        except OSError as error:
            raise ValueError(
                f"CPython source directory is unavailable: {relative}"
            ) from error
        if actual_children != children:
            raise ValueError("CPython source tree differs from the fixed selection")

    metadata = {}
    total_bytes = 0
    for relative in SELECTED_FILES:
        path = source_root / relative
        try:
            file_stat = path.lstat()
        except OSError as error:
            raise ValueError(
                f"CPython source file is unavailable: {relative}"
            ) from error
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"CPython source is not a regular file: {relative}")
        size = file_stat.st_size
        permissions = stat.S_IMODE(file_stat.st_mode)
        if not 0 <= size <= MAX_SELECTED_FILE_BYTES or permissions not in (
            0o644,
            0o755,
        ):
            raise ValueError(
                f"CPython source file size or mode is unsupported: {relative}"
            )
        total_bytes += size
        if total_bytes > MAX_SELECTED_TOTAL_BYTES:
            raise ValueError("CPython source files exceed their total bound")
        metadata[relative] = (
            path,
            size,
            permissions,
            file_stat.st_dev,
            file_stat.st_ino,
        )

    observed = {}
    for relative, (path, size, permissions, device, inode) in metadata.items():
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_size != size
                    or opened.st_dev != device
                    or opened.st_ino != inode
                ):
                    raise ValueError(
                        f"CPython source changed while being read: {relative}"
                    )
                data = stream.read(size + 1)
        except OSError as error:
            raise ValueError(
                f"CPython source file is unavailable: {relative}"
            ) from error
        if len(data) != size:
            raise ValueError(f"CPython source changed while being read: {relative}")
        observed[relative] = (data, permissions)
    return observed


def _inventory(selected: dict[str, tuple[bytes, int]]) -> dict:
    """Build the receipt inventory directly from selected source bytes and modes."""
    return {
        name: {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": oct(mode),
        }
        for name, (data, mode) in sorted(selected.items())
    }


def run(destination: Path) -> dict:
    """Fetch the pinned CPython source and stage only the selected stdlib files."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("CPython stdlib acquisition is restricted to Linux CI")
    destination = Path(destination)
    if (
        not destination.is_absolute()
        or not destination.parent.is_dir()
        or destination.parent.resolve() != destination.parent
        or destination.exists()
        or destination.is_symlink()
        or shutil.disk_usage(destination.parent).free < DISK_FLOOR
    ):
        raise ValueError("physical new destination and existing disk floor required")

    pin = _source_pin()
    destination.mkdir(mode=0o700)
    archives = destination / "archives"
    archives.mkdir(mode=0o700)
    archive_path = archives / pin["filename"]
    downloaded = fetch_rpds.download(
        pin["url"], archive_path, pin["sha256"], MAX_ARCHIVE_BYTES
    )
    archive_identity = fingerprint(archive_path, MAX_ARCHIVE_BYTES)
    if downloaded != archive_identity["bytes"] or not downloaded:
        raise ValueError("downloaded CPython stdlib archive size differs")

    selected, derivation = _source_members(archive_path, pin)
    source_root = destination / "source"
    source_root.mkdir(mode=0o700)
    directories = {
        parent
        for relative in SELECTED_FILES
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    }
    for parent in sorted(directories, key=lambda path: (len(path.parts), str(path))):
        (source_root / parent).mkdir(mode=0o700)
    for relative, (data, mode) in selected.items():
        path = source_root / relative
        with path.open("xb") as output:
            output.write(data)
        path.chmod(mode)

    source_inventory = _inventory(selected)
    if source_inventory != derivation["files"]:
        raise ValueError(
            "staged CPython stdlib inventory differs from the pinned archive"
        )
    receipt = {
        "format": FORMAT,
        "outcome": "success",
        "source_pin": pin,
        "archive": archive_identity,
        "files": source_inventory,
        "upstream_execution": False,
        "production_enabled": False,
    }
    manifest = destination / "stdlib-inputs.json"
    with manifest.open("x", encoding="utf-8") as output:
        json.dump(receipt, output, sort_keys=True, indent=2)
        output.write("\n")
    return receipt


def verify(destination: Path) -> dict:
    """Re-derive selected files from the pinned archive and compare staged bytes."""
    destination = Path(destination)
    if not destination.is_absolute() or destination.resolve() != destination:
        raise ValueError("absolute physical CPython stdlib destination required")
    try:
        if not stat.S_ISDIR(destination.lstat().st_mode):
            raise ValueError("CPython stdlib destination is not a physical directory")
    except OSError as error:
        raise ValueError("CPython stdlib destination is unavailable") from error

    pin = _source_pin()
    archives_root = destination / "archives"
    source_root = destination / "source"
    receipt_path = destination / "stdlib-inputs.json"
    try:
        if {item.name for item in destination.iterdir()} != {
            "archives",
            "source",
            "stdlib-inputs.json",
        }:
            raise ValueError("CPython stdlib destination contains unsupported entries")
        if (
            not stat.S_ISDIR(archives_root.lstat().st_mode)
            or {item.name for item in archives_root.iterdir()} != {pin["filename"]}
            or not stat.S_ISDIR(source_root.lstat().st_mode)
        ):
            raise ValueError("CPython stdlib archive/source directory layout differs")
    except OSError as error:
        raise ValueError(
            "CPython stdlib archive/source layout is unavailable"
        ) from error

    archive_path = archives_root / pin["filename"]
    selected, derivation = _source_members(archive_path, pin)
    actual = _read_source_tree(source_root)
    if _inventory(actual) != derivation["files"] or actual != selected:
        raise ValueError("CPython logging tree differs from its pinned archive")

    try:
        receipt_stat = receipt_path.lstat()
        if not stat.S_ISREG(receipt_stat.st_mode) or receipt_stat.st_size > 1024 * 1024:
            raise ValueError("CPython stdlib receipt is not a bounded regular file")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("CPython stdlib receipt is unavailable or invalid") from error

    expected = {
        "format": FORMAT,
        "outcome": "success",
        "source_pin": pin,
        "archive": derivation["archive"],
        "files": derivation["files"],
        "upstream_execution": False,
        "production_enabled": False,
    }
    if receipt != expected:
        raise ValueError("CPython stdlib receipt differs from pinned archive contents")
    return {
        "verified": True,
        "files": len(derivation["files"]),
        "archive": derivation["archive"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    run(parser.parse_args().destination)
