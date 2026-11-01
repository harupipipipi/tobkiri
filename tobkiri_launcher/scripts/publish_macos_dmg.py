#!/usr/bin/env python3
"""Atomically publish one descriptor-bound macOS disk image."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import stat
import sys
from pathlib import Path


class PublicationError(RuntimeError):
    """Raised when a disk image cannot be published without replacing a path."""


def format_identity(metadata: os.stat_result) -> str:
    """Return the complete file identity shared with the packaging shell."""

    return "%d:%d:%d:%d:%d:%d" % (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _rename_exclusive(
    source_directory: int,
    source_name: str,
    destination_directory: int,
    destination_name: str,
) -> None:
    """Rename without replacing an existing destination entry."""

    library = ctypes.CDLL(None, use_errno=True)
    arguments = (
        source_directory,
        os.fsencode(source_name),
        destination_directory,
        os.fsencode(destination_name),
    )
    if sys.platform == "darwin":
        rename = library.renameatx_np
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        flags = 0x00000001  # RENAME_NOREPLACE
    else:
        raise PublicationError(
            f"exclusive disk image publication is unsupported on {sys.platform}"
        )
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    if rename(*arguments, flags) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise PublicationError("refusing to overwrite an existing disk image")
        raise PublicationError(
            f"exclusive disk image publication failed: {os.strerror(error_number)}"
        )


def _open_directory(path: Path) -> int:
    """Open one canonical directory without following its final component."""

    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise PublicationError(f"disk image directory is not canonical: {path}")
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )


def _relative_metadata(directory: int, name: str) -> os.stat_result:
    """Read one directory-relative entry without following symbolic links."""

    return os.stat(name, dir_fd=directory, follow_symlinks=False)


def _validate_regular_image(
    metadata: os.stat_result,
    expected_identity: str,
) -> None:
    """Require one single-link, current-user regular file with the bound identity."""

    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or format_identity(metadata) != expected_identity
    ):
        raise PublicationError("disk image identity changed during publication")


def publish(
    source: Path,
    destination: Path,
    expected_identity: str,
) -> None:
    """Move a verified image to a fresh destination and retain its exact inode."""

    if not source.is_absolute() or not destination.is_absolute():
        raise PublicationError("disk image publication paths must be absolute")
    if source.name in {"", ".", ".."} or destination.name in {"", ".", ".."}:
        raise PublicationError("disk image publication names are invalid")

    source_directory = _open_directory(source.parent)
    destination_directory = _open_directory(destination.parent)
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor = os.open(
            source.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=source_directory,
        )
        source_metadata = os.fstat(source_descriptor)
        _validate_regular_image(source_metadata, expected_identity)
        _validate_regular_image(
            _relative_metadata(source_directory, source.name),
            expected_identity,
        )
        try:
            _relative_metadata(destination_directory, destination.name)
        except FileNotFoundError:
            pass
        else:
            raise PublicationError("refusing to overwrite an existing disk image")

        _rename_exclusive(
            source_directory,
            source.name,
            destination_directory,
            destination.name,
        )

        try:
            _relative_metadata(source_directory, source.name)
        except FileNotFoundError:
            pass
        else:
            raise PublicationError("source image remained after atomic publication")
        destination_descriptor = os.open(
            destination.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=destination_directory,
        )
        _validate_regular_image(os.fstat(source_descriptor), expected_identity)
        _validate_regular_image(os.fstat(destination_descriptor), expected_identity)
        _validate_regular_image(
            _relative_metadata(destination_directory, destination.name),
            expected_identity,
        )
        os.fsync(destination_descriptor)
        os.fsync(destination_directory)
        if source_directory != destination_directory:
            os.fsync(source_directory)
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(destination_directory)
        os.close(source_directory)


def main() -> int:
    """Parse command-line arguments and publish one verified disk image."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--expected-identity", required=True)
    arguments = parser.parse_args()
    try:
        publish(
            arguments.source,
            arguments.destination,
            arguments.expected_identity,
        )
    except (OSError, PublicationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
