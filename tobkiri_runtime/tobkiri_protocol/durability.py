"""Cross-platform durable atomic file publication primitives."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
from pathlib import Path
from typing import Any

_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_WINDOWS_ALREADY_EXISTS = frozenset({80, 183})


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Durably replace ``path`` with ``data`` or propagate the exact failure.

    The temporary file is created beside the destination so publication stays
    on one filesystem. File contents are flushed before publication. POSIX then
    performs ``os.replace`` followed by a parent-directory fsync; Windows uses
    MoveFileExW with REPLACE_EXISTING | WRITE_THROUGH because FlushFileBuffers
    is not a documented directory-handle durability primitive.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file_durable(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_file_durable(source: Path, destination: Path) -> None:
    """Replace ``destination`` with the strongest documented metadata durability."""

    if os.name == "nt":
        _move_windows_file_write_through(
            source,
            destination,
            replace_existing=True,
        )
        return
    os.replace(source, destination)
    flush_directory(destination.parent)


def publish_file_durable(source: Path, destination: Path) -> None:
    """Publish ``source`` at a new destination without replacing an existing file.

    ``source`` must already contain flushed bytes. On success this function
    consumes ``source``. A pre-existing destination raises ``FileExistsError``.
    """

    if os.name == "nt":
        _move_windows_file_write_through(
            source,
            destination,
            replace_existing=False,
        )
        return
    os.link(source, destination)
    source.unlink()
    flush_directory(destination.parent)


def flush_directory(path: Path) -> None:
    """Flush POSIX directory metadata.

    Windows callers must use ``replace_file_durable`` or
    ``publish_file_durable`` so metadata publication goes through the documented
    MoveFileExW write-through path instead of attempting FlushFileBuffers on a
    directory handle.
    """

    if os.name == "nt":
        raise OSError(
            errno.ENOTSUP,
            "directory fsync is not exposed as a documented Win32 primitive; "
            "use a durable file publication helper",
            str(path),
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_windows_file_write_through(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool,
    kernel32: Any | None = None,
) -> None:
    """Move one file with the documented Win32 write-through contract."""

    native = kernel32
    if native is None:
        win_dll: Any = getattr(ctypes, "WinDLL")
        native = win_dll("kernel32", use_last_error=True)
        native.MoveFileExW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        )
        native.MoveFileExW.restype = ctypes.c_int

    flags = _MOVEFILE_WRITE_THROUGH
    if replace_existing:
        flags |= _MOVEFILE_REPLACE_EXISTING
    if native.MoveFileExW(str(source), str(destination), flags):
        return

    error_code = _windows_last_error()
    if not replace_existing and error_code in _WINDOWS_ALREADY_EXISTS:
        raise FileExistsError(
            errno.EEXIST,
            "destination already exists",
            str(destination),
        )
    raise _windows_error("MoveFileExW", error_code=error_code)


def _windows_last_error() -> int:
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _windows_error(operation: str, *, error_code: int | None = None) -> OSError:
    """Build the native Windows error, including in portable adapter tests."""

    code = _windows_last_error() if error_code is None else int(error_code)
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        return OSError(f"{operation} failed: {win_error(code)}")
    return OSError(code, f"{operation} failed with Windows error {code}")
