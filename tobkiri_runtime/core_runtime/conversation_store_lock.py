"""Recoverable lock shared by the Python and Rust Conversation Store owners."""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any, Iterator

from core_runtime.runtime_locks import FileLock, LockInfo, LockTimeout

_NAME = "conversations.lock"
_LEASE = "directory-flock-v1"
_MAX_RECORD_BYTES = 4096
_LEGACY_FIELDS = {"owner", "pid", "acquired_at", "stale_after_seconds"}


@contextmanager
def conversation_store_lock(root: Path, *, timeout_ms: int = 60000) -> Iterator[None]:
    """Hold the shared Conversation Store write lock until the transaction exits."""
    path = root / _NAME
    if sys.platform not in {"darwin", "linux"}:
        with FileLock(path, timeout_ms=timeout_ms):
            yield
        return

    import fcntl

    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(directory)
        if not _owned(opened):
            raise PermissionError(f"Unsafe Conversation Store lock directory: {root}")

        while True:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                current = root.lstat()
                if not _owned(current) or not _same_inode(opened, current):
                    raise PermissionError(
                        f"Conversation Store lock directory changed: {root}"
                    )
                if _make_available(directory):
                    with _stage_record(directory) as (name, record):
                        try:
                            _rename_noreplace(directory, name, _NAME)
                        except FileExistsError:
                            # Older binaries can still publish with O_EXCL.
                            pass
                        else:
                            try:
                                yield
                            finally:
                                _unlink_if_same(directory, _NAME, record)
                            return
                fcntl.flock(directory, fcntl.LOCK_UN)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockTimeout(f"Timed out acquiring lock: {path}")
            time.sleep(min(remaining, 0.05))
    finally:
        os.close(directory)


def _owned(metadata: os.stat_result) -> bool:
    return metadata.st_uid == os.geteuid() and metadata.st_mode & 0o022 == 0


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _safe_record(metadata: os.stat_result) -> bool:
    return (
        _owned(metadata)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and 0 < metadata.st_size <= _MAX_RECORD_BYTES
    )


def _record_stamp(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
    )


def _make_available(directory: int) -> bool:
    try:
        named = os.stat(_NAME, dir_fd=directory, follow_symlinks=False)
        if not _safe_record(named):
            return False
        descriptor = os.open(
            _NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False

    try:
        opened = os.fstat(descriptor)
        if not _safe_record(opened) or not _same_inode(named, opened):
            return False
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        latest = os.fstat(descriptor)
        if (
            latest.st_size != len(raw)
            or _record_stamp(opened) != _record_stamp(latest)
            or not _reclaimable(raw)
        ):
            return False
        return _unlink_if_same(directory, _NAME, latest, require_unchanged=True)
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate lock record field")
    return result


def _reclaimable(raw: bytes) -> bool:
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(record, dict) or record.keys() not in (
            _LEGACY_FIELDS,
            _LEGACY_FIELDS | {"lease"},
        ):
            return False
        pid = record["pid"]
        owner = record["owner"]
        acquired_at = record["acquired_at"]
        if (
            type(pid) is not int
            or not 0 < pid <= 2**31 - 1
            or not isinstance(owner, str)
            or not owner.encode("utf-8")
            or type(acquired_at) not in (int, float)
            or not math.isfinite(acquired_at)
            or acquired_at <= 0
            or record["stale_after_seconds"] is not None
        ):
            return False
    except (ValueError, OverflowError, RecursionError):
        return False

    if "lease" in record:
        return record["lease"] == _LEASE

    # Only ESRCH proves absence. Live/reused PIDs and EPERM keep legacy locks busy.
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.ESRCH
    return False


@contextmanager
def _stage_record(directory: int) -> Iterator[tuple[str, os.stat_result]]:
    pid = os.getpid()
    info = LockInfo(f"pid:{pid}", pid, time.time()).to_dict()
    payload = json.dumps({**info, "lease": _LEASE}).encode("utf-8")
    name = f".conversations.lock.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        record = os.fstat(descriptor)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
            yield name, record
        finally:
            _unlink_if_same(directory, name, record)
    finally:
        os.close(descriptor)


def _unlink_if_same(
    directory: int,
    name: str,
    expected: os.stat_result,
    *,
    require_unchanged: bool = False,
) -> bool:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not _same_inode(current, expected):
            return False
        if require_unchanged and _record_stamp(current) != _record_stamp(expected):
            return False
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass
    return True


@cache
def _exclusive_rename() -> tuple[Any, int]:
    library = ctypes.CDLL(None, use_errno=True)
    name, flags = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(library, name, None)
    if function is None:
        raise OSError(errno.ENOSYS, "Atomic no-replace lock publication is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function, flags


def _rename_noreplace(directory: int, source: str, destination: str) -> None:
    function, flags = _exclusive_rename()
    if function(
        directory, os.fsencode(source), directory, os.fsencode(destination), flags
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
