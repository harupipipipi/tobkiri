"""Private, atomic files below the browser host's owned data directory."""

from __future__ import annotations

import json
import importlib
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

MAX_METADATA_BYTES = 4 * 1024 * 1024


def checked_path(root: Path, path: Path) -> Path:
    """Reject links and reparse points within the owned storage subtree."""

    root = root.absolute()
    path = path.absolute()
    relative = path.relative_to(root)
    if any(part in {".", ".."} for part in relative.parts):
        raise PermissionError("Managed browser path is outside its owned directory")
    candidates = [
        root,
        *[root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)],
    ]
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise PermissionError("Managed browser storage cannot contain links")
    return path


def private_directory(root: Path, path: Path) -> Path:
    """Create a checked directory with private Unix permissions."""

    checked_path(root, path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    checked_path(root, path)
    if not path.is_dir():
        raise ValueError("Managed browser directory is unavailable")
    os.chmod(path, 0o700)
    return path


def read_json(root: Path, path: Path) -> dict[str, Any]:
    """Read a bounded regular metadata file without following its final link."""

    checked_path(root, path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise PermissionError("Managed browser metadata is not a regular file")
        body = handle.read(MAX_METADATA_BYTES + 1)
    if len(body) > MAX_METADATA_BYTES:
        raise ValueError("Managed browser metadata is too large")
    raw = json.loads(body.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Managed browser metadata is invalid")
    return raw


def write_json(root: Path, path: Path, value: Mapping[str, Any]) -> None:
    """Replace owned metadata atomically with private file permissions."""

    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(body.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("Managed browser metadata exceeds the 4 MiB storage limit")
    private_directory(root, path.parent)
    checked_path(root, path)
    descriptor, temporary = tempfile.mkstemp(prefix=".metadata.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        checked_path(root, path)
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_lock(root: Path) -> Iterator[None]:
    """Serialize browser lifecycle changes across isolated host invocations."""

    private_directory(root, root)
    path = checked_path(root, root / ".runtime.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PermissionError("Managed browser lock is not a regular file")
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\x00")
        deadline = time.monotonic() + 5
        while not locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Managed browser is busy")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
