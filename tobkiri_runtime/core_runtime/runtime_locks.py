"""Generic runtime lock primitives for durable pack runtimes."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from tobkiri_protocol.secure_persistence import SecureDirectory


class LockTimeout(TimeoutError):
    """Raised when a runtime lock cannot be acquired before the timeout."""


@dataclass(frozen=True)
class LockInfo:
    owner: str
    pid: int
    acquired_at: float
    stale_after_seconds: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "stale_after_seconds": self.stale_after_seconds,
        }


class FileLock:
    """Small cross-process lock backed by an atomic lock file."""

    def __init__(
        self,
        path: str | Path,
        *,
        owner: str = "",
        timeout_ms: int = 60000,
        stale_after_seconds: Optional[float] = None,
        poll_interval: float = 0.05,
        reentrant: bool = False,
        directory: SecureDirectory | None = None,
    ) -> None:
        self.path = Path(path)
        if directory is not None and self.path.parent != directory.root:
            raise ValueError("lock must be an immediate child of its pinned directory")
        self._directory = directory
        self.owner = owner or f"pid:{os.getpid()}"
        self.timeout_ms = timeout_ms
        self.stale_after_seconds = stale_after_seconds
        self.poll_interval = poll_interval
        self.reentrant = reentrant
        self._local_lock = threading.RLock()
        self._held = 0

    def acquire(self) -> "FileLock":
        deadline = time.monotonic() + max(self.timeout_ms, 0) / 1000.0
        with self._local_lock:
            if self._held and self.reentrant:
                self._held += 1
                return self

            if self._directory is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                LockInfo(
                    owner=self.owner,
                    pid=os.getpid(),
                    acquired_at=time.time(),
                    stale_after_seconds=self.stale_after_seconds,
                ).to_dict(),
                ensure_ascii=True,
                sort_keys=True,
            )

            while True:
                try:
                    fd = (
                        self._directory.open_lock(self.path.name, exclusive=True)
                        if self._directory is not None
                        else os.open(
                            str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                        )
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(payload)
                    self._held = 1
                    return self
                except FileExistsError:
                    if self.is_stale():
                        self.break_stale()
                        continue
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"Timed out acquiring lock: {self.path}")
                    time.sleep(self.poll_interval)

    def release(self) -> None:
        with self._local_lock:
            if self._held > 1 and self.reentrant:
                self._held -= 1
                return
            if not self._held:
                return
            try:
                current = self.read_info()
                if current is None or current.get("owner") == self.owner:
                    self._unlink(missing_ok=True)
            finally:
                self._held = 0

    def read_info(self) -> Optional[dict[str, Any]]:
        try:
            raw = (
                self._directory.read_bytes_bounded(
                    self.path.name, max_bytes=4096
                ).decode("utf-8")
                if self._directory is not None
                else self.path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def is_stale(self) -> bool:
        info = self.read_info()
        if info is None:
            return False
        acquired_at = info.get("acquired_at")
        stale_after = info.get("stale_after_seconds", self.stale_after_seconds)
        if not isinstance(acquired_at, (int, float)) or not isinstance(
            stale_after, (int, float)
        ):
            return False
        return time.time() - float(acquired_at) > float(stale_after)

    def _unlink(self, *, missing_ok: bool = False) -> None:
        if self._directory is not None:
            self._directory.unlink(self.path.name, missing_ok=missing_ok)
        else:
            self.path.unlink(missing_ok=missing_ok)

    def break_stale(self) -> bool:
        if not self.is_stale():
            return False
        try:
            self._unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


class NamedLock:
    """A file lock whose filename is derived from a logical runtime name."""

    def __init__(
        self,
        root: str | Path,
        name: str,
        *,
        owner: str = "",
        timeout_ms: int = 60000,
        stale_after_seconds: Optional[float] = None,
        reentrant: bool = False,
        directory: SecureDirectory | None = None,
    ) -> None:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        if not safe:
            safe = "lock"
        self._lock = FileLock(
            Path(root) / f"{safe}.lock",
            owner=owner,
            timeout_ms=timeout_ms,
            stale_after_seconds=stale_after_seconds,
            reentrant=reentrant,
            directory=directory,
        )

    def acquire(self) -> FileLock:
        return self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()
