from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from copy import deepcopy
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
REVISION_KEY = "_settings_revision"
STATE_REVISIONS_KEY = "_state_revisions"
MUTATION_RECEIPTS_KEY = "_mutation_receipts"
MAX_MUTATION_RECEIPTS = 64


class FrontendSettingsRevisionConflict(RuntimeError):
    """Raised when a state mutation targets an obsolete state revision."""

    def __init__(self, state_ref: str, expected: int, actual: int) -> None:
        super().__init__(
            f"state revision conflict for {state_ref}: expected {expected}, current {actual}"
        )
        self.state_ref = state_ref
        self.expected = expected
        self.actual = actual


class FrontendSettingsIdempotencyConflict(RuntimeError):
    """Raised when an idempotency key is reused for a different mutation."""


def defaultspack_frontend_settings_path(pack_root: Path | None = None) -> Path:
    """Return the durable settings path for a Defaultspack installation.

    Managed desktop packs are unpacked into a replaceable application bundle.
    The launcher supplies ``RUMI_USER_DATA`` for state that must survive a
    bundle update; an explicit path still takes precedence for tests.
    """
    override = os.environ.get("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if pack_root is not None:
        return Path(pack_root).expanduser() / "user_data" / "shared" / "frontend_settings.json"

    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return (
            Path(user_data).expanduser()
            / "defaultspack"
            / "shared"
            / "frontend_settings.json"
        )
    return (
        Path(__file__).resolve().parents[1]
        / "user_data"
        / "shared"
        / "frontend_settings.json"
    )


class FrontendSettingsCorruptError(ValueError):
    """Raised when neither the settings document nor its backup is readable."""


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _acquire_file_lock(lock_file: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("no supported file-locking implementation is available")


def _release_file_lock(lock_file: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)


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

    def mutate_state(
        self,
        state_ref: str,
        transform: Callable[
            [dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]
        ],
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Atomically mutate one logical state resource.

        State revisions are independent from the whole settings-document
        revision. Optional idempotency receipts live in the same atomic file so
        a retried transport cannot apply the mutation twice.
        """
        normalized_ref = str(state_ref or "").strip()
        if not normalized_ref:
            raise ValueError("state_ref is required")
        normalized_key = str(idempotency_key or "").strip()
        if normalized_key and not 8 <= len(normalized_key) <= 256:
            raise ValueError("idempotency_key must be 8-256 characters")

        with self._locked():
            current = self._read_locked(recover=True)
            receipts = current.get(MUTATION_RECEIPTS_KEY, {})
            if not isinstance(receipts, dict):
                receipts = {}
            if normalized_key:
                previous = receipts.get(normalized_key)
                if isinstance(previous, dict):
                    if str(previous.get("fingerprint") or "") != request_fingerprint:
                        raise FrontendSettingsIdempotencyConflict(
                            "idempotency_key was already used for a different mutation"
                        )
                    previous_result = previous.get("result")
                    if isinstance(previous_result, dict):
                        replay = deepcopy(previous_result)
                        replay["idempotent_replay"] = True
                        return replay

            revisions = current.get(STATE_REVISIONS_KEY, {})
            if not isinstance(revisions, dict):
                revisions = {}
            current_revision = revisions.get(normalized_ref, 0)
            if (
                not isinstance(current_revision, int)
                or isinstance(current_revision, bool)
                or current_revision < 0
            ):
                current_revision = 0
            if expected_revision is not None and expected_revision != current_revision:
                raise FrontendSettingsRevisionConflict(
                    normalized_ref, expected_revision, current_revision
                )

            updated, result = transform(deepcopy(current))
            if not isinstance(updated, dict) or not isinstance(result, dict):
                raise TypeError("state mutation must return settings and result objects")
            next_state_revision = current_revision + 1
            next_revisions = dict(revisions)
            next_revisions[normalized_ref] = next_state_revision
            updated[STATE_REVISIONS_KEY] = next_revisions

            document_revision = current.get(REVISION_KEY, 0)
            if (
                not isinstance(document_revision, int)
                or isinstance(document_revision, bool)
                or document_revision < 0
            ):
                document_revision = 0
            updated[REVISION_KEY] = document_revision + 1

            settled = deepcopy(result)
            settled["state_ref"] = normalized_ref
            settled["revision"] = next_state_revision
            settled["document_revision"] = updated[REVISION_KEY]
            settled["idempotent_replay"] = False

            if normalized_key:
                next_receipts = dict(receipts)
                next_receipts.pop(normalized_key, None)
                next_receipts[normalized_key] = {
                    "fingerprint": request_fingerprint,
                    "result": deepcopy(settled),
                }
                while len(next_receipts) > MAX_MUTATION_RECEIPTS:
                    next_receipts.pop(next(iter(next_receipts)))
                updated[MUTATION_RECEIPTS_KEY] = next_receipts

            self._atomic_write(updated, preserve_backup=True)
            return settled

    def state_revision(self, state_ref: str) -> int:
        value = self.read().get(STATE_REVISIONS_KEY, {})
        if not isinstance(value, dict):
            return 0
        revision = value.get(str(state_ref or "").strip(), 0)
        return revision if isinstance(revision, int) and not isinstance(revision, bool) else 0

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
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(fd, mode)
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
