from __future__ import annotations

import json
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
from copy import deepcopy
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from tobkiri_protocol.settings_state import (
    FrontendSettingsCorruptError,
    FrontendSettingsIdempotencyConflict,
    FrontendSettingsRevisionConflict,
    MAX_MUTATION_RECEIPTS,
    MUTATION_RECEIPTS_KEY,
    REVISION_KEY,
    STATE_REVISIONS_KEY,
    settings_state_revision,
)


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

    def read(self, *, preserve_corrupt: bool = False) -> dict[str, Any]:
        """Read settings, recovering a corrupt primary document from backup."""
        with self._locked():
            try:
                return self._read_locked(recover=True)
            except FrontendSettingsCorruptError:
                if preserve_corrupt:
                    self._preserve_corrupt_locked()
                raise

    def _preserve_corrupt_locked(self) -> None:
        """Keep unreadable bytes under the same lock as recovery and updates."""
        try:
            content = self.path.read_bytes()
        except OSError:
            return
        digest = hashlib.sha256(content).hexdigest()
        backup = self.path.with_name(f"{self.path.name}.corrupt-{digest}.bak")
        if backup.exists():
            if backup.read_bytes() != content:
                raise FrontendSettingsCorruptError("corrupt settings backup differs")
            return
        mode = self.path.stat().st_mode & 0o777
        self._atomic_write_bytes(backup, content, mode=mode)

    def read_snapshot(self) -> dict[str, Any]:
        """Read an atomic snapshot without locking files, repair or migration.

        Writers publish complete documents with atomic replacement. This read
        therefore observes either revision without needing a filesystem write.
        Recovery remains an explicit write-capable operation, never a side
        effect of a read-only UI contract.
        """
        try:
            return self._load_mapping(self.path)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise FrontendSettingsCorruptError(
                "frontend settings snapshot is corrupt"
            ) from error

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

    def compare_and_swap_document(
        self, document: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        """Commit JSON values computed outside the owner at an exact revision.

        This is the data-only replacement for passing update callbacks across
        a future settings-owner contract. The owner retains its revision and
        state/receipt metadata; callers cannot replace those control fields.
        This method neither grants authority nor performs a live owner cutover.
        """
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("settings document revision must be an exact nonnegative integer")
        if not isinstance(document, Mapping):
            raise ValueError("settings document must be an object")
        candidate = deepcopy(dict(document))
        # Reject non-JSON values and non-finite numbers before opening storage.
        encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
        if decoded != candidate:
            raise ValueError("settings document requires JSON keys and values")
        candidate = decoded
        with self._locked():
            # Recovery is a separate write operation. A stale CAS must not
            # repair/replace the primary before deciding its revision conflict.
            current = self._read_locked(recover=False)
            revision = current.get(REVISION_KEY, 0)
            if type(revision) is not int or revision < 0:
                raise FrontendSettingsCorruptError("settings document revision is invalid")
            if revision != expected_revision:
                raise FrontendSettingsRevisionConflict("settings.document", expected_revision, revision)
            for key in (REVISION_KEY, STATE_REVISIONS_KEY, MUTATION_RECEIPTS_KEY):
                # Absent keys may stay absent, but deletion or replacement of
                # an existing control field is not a document update.
                if (key in candidate) != (key in current) or json.dumps(
                    candidate.get(key), sort_keys=True, allow_nan=False
                ) != json.dumps(current.get(key), sort_keys=True, allow_nan=False):
                    raise ValueError("settings document cannot change owner metadata")
            candidate[REVISION_KEY] = revision + 1
            self._atomic_write(candidate, preserve_backup=True)
            return candidate

    def compare_and_swap_fields(
        self,
        changes: Mapping[str, Mapping[str, Any]],
        *,
        allowed_fields: Mapping[str, frozenset[str]],
        expected_revision: int,
    ) -> dict[str, Any]:
        """Merge an owner-authorized field patch without exporting private state.

        ``allowed_fields`` is trusted owner policy, never a request parameter.
        Field value schemas and caller authorization belong to the captured
        operation. A read disclosure allowlist alone is not write authorization.
        This primitive does not register that operation or retry lost replies.
        """
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("settings document revision must be an exact nonnegative integer")
        if not isinstance(changes, Mapping) or not changes:
            raise ValueError("settings changes must be a nonempty object")
        patch = deepcopy(dict(changes))
        for section, fields in patch.items():
            if (
                not isinstance(section, str)
                or section in {REVISION_KEY, STATE_REVISIONS_KEY, MUTATION_RECEIPTS_KEY}
                or section not in allowed_fields
                or not isinstance(fields, dict)
                or not fields
                or any(
                    not isinstance(field, str) or field not in allowed_fields[section]
                    for field in fields
                )
            ):
                raise PermissionError("settings field is outside owner write policy")
        decoded = json.loads(json.dumps(patch, allow_nan=False))
        if decoded != patch:
            raise ValueError("settings changes require JSON keys and values")
        snapshot = self.read_snapshot()
        revision = snapshot.get(REVISION_KEY, 0)
        if type(revision) is not int or revision < 0:
            raise FrontendSettingsCorruptError("settings document revision is invalid")
        if revision != expected_revision:
            raise FrontendSettingsRevisionConflict("settings.document", expected_revision, revision)
        for section, fields in decoded.items():
            current = snapshot.get(section, {})
            if not isinstance(current, dict):
                raise FrontendSettingsCorruptError("settings section is invalid")
            snapshot[section] = {**current, **fields}
        committed = self.compare_and_swap_document(
            snapshot, expected_revision=expected_revision,
        )
        return {"values": decoded, "document_revision": committed[REVISION_KEY]}

    def compare_and_swap_state(
        self,
        state_ref: str,
        document: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        expected_document_revision: int,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Commit data-only state proposals using existing receipt semantics.

        Only this owner creates the local transform. No callback is received
        from a future contract caller. Receipt replay is checked by mutate_state
        before the document CAS, preserving an acknowledged retry's result.
        """
        if type(expected_document_revision) is not int or expected_document_revision < 0:
            raise ValueError("settings document revision must be an exact nonnegative integer")
        candidate = deepcopy(dict(document))
        response = deepcopy(dict(result))
        decoded = json.loads(json.dumps([candidate, response], allow_nan=False))
        if decoded != [candidate, response]:
            raise ValueError("settings state proposal requires JSON keys and values")
        candidate, response = decoded

        def apply(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            revision = current.get(REVISION_KEY, 0)
            if type(revision) is not int or revision < 0:
                raise FrontendSettingsCorruptError("settings document revision is invalid")
            if revision != expected_document_revision:
                raise FrontendSettingsRevisionConflict(
                    "settings.document", expected_document_revision, revision
                )
            for key in (REVISION_KEY, STATE_REVISIONS_KEY, MUTATION_RECEIPTS_KEY):
                if (key in candidate) != (key in current) or json.dumps(
                    candidate.get(key), sort_keys=True, allow_nan=False
                ) != json.dumps(current.get(key), sort_keys=True, allow_nan=False):
                    raise ValueError("settings document cannot change owner metadata")
            return candidate, response

        return self.mutate_state(
            state_ref, apply, expected_revision=expected_revision,
            idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
        )

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
                raise FrontendSettingsCorruptError("settings mutation receipts are corrupt")
            if normalized_key and normalized_key in receipts:
                previous = receipts[normalized_key]
                if not isinstance(previous, dict) or not isinstance(
                    previous.get("result"), dict
                ):
                    raise FrontendSettingsCorruptError("settings mutation receipt is corrupt")
                previous_result = previous["result"]
                if (
                    previous.get("fingerprint") != request_fingerprint
                    or previous_result.get("state_ref") != normalized_ref
                ):
                    raise FrontendSettingsIdempotencyConflict(
                        "idempotency_key was already used for a different mutation"
                    )
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
        return settings_state_revision(self.read(), state_ref)

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
        content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._atomic_write_bytes(self.path, content, mode=mode)

    def _atomic_write_bytes(self, path: Path, content: bytes, *, mode: int) -> None:
        """Publish owner-selected bytes without a second UI persistence path."""
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(handle.fileno(), mode)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_file(temp_path, path)
            self._fsync_directory(path.parent)
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
    if sys.platform == "win32":
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
        return
    import fcntl

    # A missing/failed OS lock must never allow recovery or mutation to run.
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file_handle(handle: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
