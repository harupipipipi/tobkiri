"""Byte-verified copies into caller-owned, exclusively created output handles."""

from __future__ import annotations

import hashlib
import os
import stat
from typing import BinaryIO

_BLOCK_SIZE = 64 * 1024


def _enable_sparse(output: BinaryIO) -> None:
    if os.name != "nt":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    control = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
    control.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    control.restype = wintypes.BOOL
    returned = wintypes.DWORD()
    # FSCTL_SET_SPARSE with a NULL input buffer sets sparse mode. Only the
    # caller's newly created destination handle is modified, never the source.
    if not control(
        msvcrt.get_osfhandle(output.fileno()),
        0x000900C4,
        None,
        0,
        None,
        0,
        ctypes.byref(returned),
        None,
    ):
        error = ctypes.get_last_error()
        raise OSError(
            error,
            "PackVM destination cannot enable sparse files; "
            "use a filesystem supporting sparse files",
        )


def copy_verified_stream(
    source: BinaryIO,
    output: BinaryIO,
    *,
    expected_digest: str,
    size_bytes: int,
    sparse: bool = False,
) -> int:
    """Copy and verify every logical byte, including holes, on pinned handles.

    The caller must exclusively create a readable/writable, empty destination
    and remove it on failure. Source identity and path checks remain the caller's
    responsibility. Sparse support never reduces capacity reservations. Windows
    fails closed if sparse marking fails, rather than silently expanding images.
    """
    metadata = os.fstat(output.fileno())
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != 0
        or metadata.st_nlink != 1
        or output.tell() != 0
        or size_bytes < 0
    ):
        raise ValueError("PackVM copy requires an empty private regular output")
    if sparse:
        _enable_sparse(output)
    digest = hashlib.sha256()
    total = 0
    while block := source.read(_BLOCK_SIZE):
        total += len(block)
        if total > size_bytes:
            raise ValueError("PackVM input changed during copy")
        digest.update(block)
        if sparse and not block.strip(b"\0"):
            output.seek(len(block), os.SEEK_CUR)
        elif output.write(block) != len(block):
            raise OSError("PackVM copy encountered a short write")
    if total != size_bytes or "sha256:" + digest.hexdigest() != expected_digest:
        raise ValueError("PackVM input digest mismatch or input changed during copy")
    # Seeking past EOF alone does not create trailing holes or an all-zero file.
    output.truncate(total)
    output.flush()
    os.fsync(output.fileno())
    output.seek(0)
    copied = hashlib.sha256()
    read_total = 0
    while block := output.read(_BLOCK_SIZE):
        copied.update(block)
        read_total += len(block)
    if read_total != total or "sha256:" + copied.hexdigest() != expected_digest:
        raise ValueError("PackVM destination copy digest mismatch")
    return total
