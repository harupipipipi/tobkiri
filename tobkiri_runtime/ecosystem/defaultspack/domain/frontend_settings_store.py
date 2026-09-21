from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REVISION_KEY = "_settings_revision"


class FrontendSettingsCorruptError(ValueError):
    """Raised when neither the settings document nor its backup is readable."""


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


class FrontendSettingsStore:
    """Serialize and atomically persist the shared frontend settings document."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.backup_path = path.with_suffix(f"{path.suffix}.bak")
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")

    def read(self) -> dict[str, Any]:
        """Read settings, recovering a corrupt primary document from backup."""
        with self._locked():
            return self._read_locked(recover=True)

    def update(
        self,
        transform: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply a read-modify-write transform under process and thread locks."""
        with self._locked():
            current = self._read_locked(recover=True)
            updated = transform(dict(current))
            if not isinstance(updated, dict):
                raise TypeError("frontend settings update must return an object")
            revision = current.get(REVISION_KEY, 0)
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                revision = 0
            updated[REVISION_KEY] = revision + 1
            self._atomic_write(updated, preserve_backup=True)
            return updated

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _thread_lock(self.path):
            with self.lock_path.open("a+b") as lock_file:
                _lock_file_handle(lock_file)
                try:
                    yield
                finally:
                    _unlock_file_handle(lock_file)

    def _read_locked(self, *, recover: bool) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return self._load_mapping(self.path)
        except (json.JSONDecodeError, TypeError, ValueError) as primary_error:
            if recover and self.backup_path.exists():
                try:
                    backup = self._load_mapping(self.backup_path)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass
                else:
                    self._atomic_write(backup, preserve_backup=False)
                    return backup
            raise FrontendSettingsCorruptError(
                f"frontend settings are corrupt: {self.path}"
            ) from primary_error

    @staticmethod
    def _load_mapping(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("frontend settings root must be an object")
        return value

    def _atomic_write(
        self,
        value: dict[str, Any],
        *,
        preserve_backup: bool,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if preserve_backup and self.path.exists():
            shutil.copyfile(self.path, self.backup_path)
            self._fsync_file(self.backup_path)
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_file(temp_path, self.path)
            self._fsync_directory(self.path.parent)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        try:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        except OSError:
            return

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            directory_fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            return


def _lock_file_handle(handle: Any) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            _ensure_lock_byte(handle)
            handle.seek(0)
            for _ in range(400):
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.025)
            else:
                raise TimeoutError("timed out acquiring frontend settings lock")
        except ImportError:
            return
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        return


def _unlock_file_handle(handle: Any) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            return
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        return


def _ensure_lock_byte(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()


def _replace_file(source: Path, destination: Path) -> None:
    for attempt in range(40):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.025)
