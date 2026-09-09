"""Bounded process-shared locking for the existing legacy approval file."""

from __future__ import annotations

import errno
import importlib
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def legacy_approval_lock(path: Path, *, timeout: float = 5.0) -> Iterator[None]:
    """Serialize approval updates without reclaiming another process's lock."""
    if not 0 < timeout <= 5:
        raise ValueError("approval lock timeout must be within five seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise OSError("approval lock must not be a symbolic link")
    descriptor = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError("approval lock must be a singly-linked regular file")
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            if opened.st_size == 0:
                os.write(descriptor, b"0")
        else:
            import fcntl

        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("approval lock deadline exceeded") from error
                time.sleep(min(0.01, remaining))
        named = lock_path.lstat()
        if (
            named.st_dev != opened.st_dev or named.st_ino != opened.st_ino
            or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
        ):
            raise OSError("approval lock identity changed")
        yield
    finally:
        # Closing releases the OS lock even after an exception or process exit.
        # Never unlink: a waiter may already hold the same file descriptor.
        os.close(descriptor)
