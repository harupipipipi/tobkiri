"""Descriptor-pinned, bounded regular work files for portable container tasks."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from tobkiri_protocol.workspace_capsule_v1 import MAX_FILES, MAX_FILE_BYTES, safe_path

MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_TASK_BYTES = MAX_TOTAL_BYTES


def read_work_tree(root: Path) -> dict[str, bytes]:
    """Read bounded regular output after verified container cleanup, rejecting links."""
    files: dict[str, bytes] = {}
    total, entries = 0, 0
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PermissionError("descriptor-safe task output inspection is unavailable")

    def visit(directory: int, prefix: str) -> None:
        nonlocal total, entries
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_FILES * 4:
                    raise ValueError("workspace task output contains too many entries")
                relative = safe_path(prefix + child.name)
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise PermissionError(
                        "workspace task output contains a symbolic link"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    descriptor = os.open(
                        child.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    try:
                        pinned = os.fstat(descriptor)
                        if (pinned.st_dev, pinned.st_ino) != (
                            metadata.st_dev,
                            metadata.st_ino,
                        ):
                            raise PermissionError(
                                "workspace task output directory changed"
                            )
                        visit(descriptor, relative + "/")
                    finally:
                        os.close(descriptor)
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > MAX_FILE_BYTES
                ):
                    raise ValueError("workspace task output file is invalid")
                total += metadata.st_size
                if len(files) >= MAX_FILES or total > min(
                    MAX_TOTAL_BYTES, MAX_TASK_BYTES
                ):
                    raise ValueError("workspace task output exceeds the capsule limit")
                descriptor = os.open(
                    child.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as stream:
                    pinned = os.fstat(stream.fileno())
                    if (pinned.st_dev, pinned.st_ino, pinned.st_size) != (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                    ):
                        raise PermissionError("workspace task output changed")
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != metadata.st_size:
                    raise ValueError("workspace task output size changed")
                files[relative] = data

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor, "")
    finally:
        os.close(descriptor)
    return files
