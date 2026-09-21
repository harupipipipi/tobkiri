"""Read-only identity capture for one approval-gated MCP process start."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import threading
import time
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

_EXECUTABLE_BYTE_LIMIT = 512 * 1024 * 1024


def _check_lifetime(deadline: float, cancellation: threading.Event) -> None:
    if cancellation.is_set():
        raise InterruptedError("MCP preparation cancelled")
    if time.monotonic() >= deadline:
        raise TimeoutError("MCP preparation deadline elapsed")


def connection_plan(
    *,
    request: Mapping[str, Any],
    config: Mapping[str, Any],
    capture: tuple[str, str, str, int],
    owner: tuple[str, str],
    workspace_root: Path,
    workspace_id: str,
    workspace_revision: str | int,
    deadline: float,
    cancellation: threading.Event,
) -> dict[str, Any]:
    """Describe exact inputs without spawning or copying approval authority.

    The resulting plan is Host-private pending-effect data. Approval UI must
    project finite labels from it, never expose request env/argv or the plan.
    """
    _check_lifetime(deadline, cancellation)
    executable = Path(config["command"][0])
    # Follow an explicit installed executable symlink once and bind both its
    # resolved path and bytes. Rebuilding the plan detects target replacement.
    executable = executable.resolve(strict=True)
    digest = hashlib.sha256()
    # Reject special files before reading. A blocking open() of a FIFO would
    # otherwise wait forever before the deadline/cancellation checks. On POSIX,
    # also reject a symlink installed after resolve(); this pins the inspection
    # descriptor, not the subsequent process start.
    descriptor = os.open(
        executable,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or before.st_size > _EXECUTABLE_BYTE_LIMIT
        ):
            raise PermissionError("MCP executable is unavailable")
        bytes_read = 0
        while True:
            _check_lifetime(deadline, cancellation)
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            bytes_read += len(block)
            if bytes_read > _EXECUTABLE_BYTE_LIMIT:
                raise PermissionError("MCP executable exceeds preparation limit")
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_identity(before) != _file_identity(after):
        raise PermissionError("MCP executable changed during preparation")
    return {
        "version": "tobkiri.mcp.connection-plan.v1",
        "profile_id": capture[0],
        "activation_id": capture[1],
        "plan_digest": capture[2],
        "security_epoch": capture[3],
        "owner_principal_id": owner[0],
        "owner_session_id": owner[1],
        "request_digest": canonical_digest(dict(request)),
        "workspace": {
            **_directory(workspace_root),
            "id": workspace_id,
            "revision": workspace_revision,
        },
        "cwd": _directory(Path(config["cwd"])),
        "executable": {
            "path": str(executable),
            "sha256": "sha256:" + digest.hexdigest(),
            **_file_identity(after),
        },
    }


def _directory(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("MCP workspace is unavailable")
    return {
        "path": str(path),
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _file_identity(metadata: os.stat_result) -> dict[str, str | int]:
    # Filesystem identifiers and nanosecond times may exceed I-JSON's exact
    # integer range. Decimal strings preserve their full value in snapshots.
    return {
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": str(metadata.st_size),
        "mtime_ns": str(metadata.st_mtime_ns),
        "ctime_ns": str(metadata.st_ctime_ns),
    }
