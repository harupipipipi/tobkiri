"""Byte-verified copies into caller-owned, exclusively created output handles."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .windows_abi_types import WindowsCRuntime, WindowsCTypes

import ctypes
import hashlib
import os
import stat
from typing import BinaryIO

_BLOCK_SIZE = 64 * 1024


def _sparse_control(output: BinaryIO, code: int, data: ctypes.Structure | None = None) -> None:
    if os.name != "nt":
        return
    import msvcrt
    from ctypes import wintypes

    control = cast("WindowsCTypes", ctypes).WinDLL("kernel32", use_last_error=True).DeviceIoControl
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
    if not control(
        cast("WindowsCRuntime", msvcrt).get_osfhandle(output.fileno()),
        code,
        ctypes.byref(data) if data is not None else None,
        ctypes.sizeof(data) if data is not None else 0,
        None,
        0,
        ctypes.byref(returned),
        None,
    ):
        error = cast("WindowsCTypes", ctypes).get_last_error()
        raise OSError(
            error,
            "PackVM destination cannot perform sparse file operation; "
            "use a filesystem supporting sparse files",
        )


def _enable_sparse(output: BinaryIO) -> None:
    # FSCTL_SET_SPARSE with NULL input marks only the newly owned output.
    _sparse_control(output, 0x000900C4)


def _deallocate_zero_ranges(output: BinaryIO, ranges: list[tuple[int, int]]) -> None:
    if os.name != "nt":
        return

    class ZeroData(ctypes.Structure):
        _fields_ = [("FileOffset", ctypes.c_int64), ("BeyondFinalZero", ctypes.c_int64)]

    for start, end in ranges:
        # FSCTL_SET_ZERO_DATA uses an exclusive ending offset. A seek followed
        # by WriteFile can allocate preceding zeros even on a sparse NTFS file;
        # explicitly deallocate only the source-verified zero spans after EOF
        # and all buffered writes are finalized, before readback verification.
        _sparse_control(output, 0x000980C8, ZeroData(start, end))


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
    zero_ranges: list[tuple[int, int]] = []
    zero_start: int | None = None
    while block := source.read(_BLOCK_SIZE):
        total += len(block)
        if total > size_bytes:
            raise ValueError("PackVM input changed during copy")
        digest.update(block)
        if sparse and not block.strip(b"\0"):
            if zero_start is None:
                zero_start = total - len(block)
            output.seek(len(block), os.SEEK_CUR)
        else:
            if zero_start is not None:
                zero_ranges.append((zero_start, total - len(block)))
                zero_start = None
            if output.write(block) != len(block):
                raise OSError("PackVM copy encountered a short write")
    if zero_start is not None:
        zero_ranges.append((zero_start, total))
    if total != size_bytes or "sha256:" + digest.hexdigest() != expected_digest:
        raise ValueError("PackVM input digest mismatch or input changed during copy")
    # Seeking past EOF alone does not create trailing holes or an all-zero file.
    output.truncate(total)
    output.flush()
    if sparse:
        _deallocate_zero_ranges(output, zero_ranges)
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
