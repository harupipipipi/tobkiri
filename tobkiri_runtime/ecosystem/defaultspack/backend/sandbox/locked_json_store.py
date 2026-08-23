from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable, Iterator, TypeVar


_T = TypeVar("_T")


class LockedJsonStore:
    """Atomically update a JSON object under a process-wide file lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        """Read the current JSON object, returning an empty object on absence."""
        with self.locked():
            return self._read_unlocked()

    def update(
        self,
        callback: Callable[
            [dict[str, Any]],
            tuple[dict[str, Any], _T],
        ],
    ) -> _T:
        """Update the object transactionally and return the callback result."""
        with self.locked():
            data = self._read_unlocked()
            next_data, result = callback(data)
            self._write_unlocked(next_data)
            return result

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this store's process-wide advisory lock without mutation."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.lock_path.open("a+b") as handle:
            _lock_file_handle(handle)
            try:
                yield
            finally:
                _unlock_file_handle(handle)

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(self.path)


def _lock_file_handle(handle: Any) -> None:
    if os.name == "nt":
        _windows_lock(handle)
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        return


def _unlock_file_handle(handle: Any) -> None:
    if os.name == "nt":
        _windows_unlock(handle)
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        return


def _windows_lock(handle: Any) -> None:
    try:
        import msvcrt

        _ensure_lock_byte(handle)
        handle.seek(0)
        msvcrt.locking(  # type: ignore[attr-defined]
            handle.fileno(),
            msvcrt.LK_LOCK,  # type: ignore[attr-defined]
            1,
        )
    except (ImportError, OSError):
        return


def _windows_unlock(handle: Any) -> None:
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(  # type: ignore[attr-defined]
            handle.fileno(),
            msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
            1,
        )
    except (ImportError, OSError):
        return


def _ensure_lock_byte(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
