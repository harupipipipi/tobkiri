from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.settings_state import (
    FrontendSettingsCorruptError,
    FrontendSettingsIdempotencyConflict,
    FrontendSettingsRevisionConflict,
    LEGACY_WRITER_STATES,
    MAX_MUTATION_RECEIPTS,
    MAX_OWNER_MIGRATIONS,
    MUTATION_RECEIPTS_KEY,
    OWNER_MIGRATIONS_KEY,
    REVISION_KEY,
    STATE_REVISIONS_KEY,
    settings_state_revision,
)


_OWNER_CONTROL_KEYS = (
    REVISION_KEY,
    STATE_REVISIONS_KEY,
    MUTATION_RECEIPTS_KEY,
    OWNER_MIGRATIONS_KEY,
)


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_LOCK_POLL_SECONDS = 0.025


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
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: int,
        lock_cancellation: Any | None = None,
        lock_deadline: float | None = None,
        lock_fence: Callable[[], None] | None = None,
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
        with self._locked(
            cancellation=lock_cancellation,
            deadline=lock_deadline,
            fence=lock_fence,
        ):
            # Recovery is a separate write operation. A stale CAS must not
            # repair/replace the primary before deciding its revision conflict.
            current = self._read_locked(recover=False)
            revision = current.get(REVISION_KEY, 0)
            if type(revision) is not int or revision < 0:
                raise FrontendSettingsCorruptError("settings document revision is invalid")
            if revision != expected_revision:
                raise FrontendSettingsRevisionConflict("settings.document", expected_revision, revision)
            for key in _OWNER_CONTROL_KEYS:
                # Absent keys may stay absent, but deletion or replacement of
                # an existing control field is not a document update.
                if (key in candidate) != (key in current) or json.dumps(
                    candidate.get(key), sort_keys=True, allow_nan=False
                ) != json.dumps(current.get(key), sort_keys=True, allow_nan=False):
                    raise ValueError("settings document cannot change owner metadata")
            _assert_lock_wait_active(
                cancellation=lock_cancellation,
                deadline=lock_deadline,
                fence=lock_fence,
            )
            candidate[REVISION_KEY] = revision + 1
            self._atomic_write(candidate, preserve_backup=True)
            return candidate

    def compare_and_swap_fields(
        self,
        changes: Mapping[str, Mapping[str, Any]],
        *,
        allowed_fields: Mapping[str, frozenset[str]],
        expected_revision: int,
        lock_cancellation: Any | None = None,
        lock_deadline: float | None = None,
        lock_fence: Callable[[], None] | None = None,
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
                or section in _OWNER_CONTROL_KEYS
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
        lock_options: dict[str, Any] = {}
        if lock_cancellation is not None:
            lock_options["lock_cancellation"] = lock_cancellation
        if lock_deadline is not None:
            lock_options["lock_deadline"] = lock_deadline
        if lock_fence is not None:
            lock_options["lock_fence"] = lock_fence
        committed = self.compare_and_swap_document(
            snapshot,
            expected_revision=expected_revision,
            **lock_options,
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
            for key in _OWNER_CONTROL_KEYS:
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

    def adopt_legacy_document(
        self,
        *,
        migration_id: Any,
        legacy_writer_state: Any,
        expected_source_revision: Any,
        expected_destination_revision: Any = None,
        legacy_path: Path | None = None,
        lock_cancellation: Any | None = None,
        lock_deadline: float | None = None,
        lock_fence: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Adopt one legacy settings document as this owner's document.

        This is the reviewed typed storage-owner cutover. The caller must be
        trusted Host/maintenance code: ``legacy_writer_state`` is the required
        attestation that old writers are stopped or no longer authorized,
        ``expected_source_revision`` pins the adopted document, the optional
        ``expected_destination_revision`` pins this owner's current document,
        and ``migration_id`` gives durable replay protection. The operation
        never repairs a corrupt source, accepts no request fields and is not
        a request-reachable API. A same-path transfer records the cutover in
        the shared document; an explicit distinct ``legacy_path`` is adopted
        under its own store lock and then retired beside its bytes so a stale
        writer cannot silently continue the original document.
        """
        normalized_id = str(migration_id or "").strip()
        if not 8 <= len(normalized_id) <= 256:
            raise ValueError("migration_id must be 8-256 characters")
        if legacy_writer_state not in LEGACY_WRITER_STATES:
            raise PermissionError("legacy writer precondition is not established")
        if type(expected_source_revision) is not int or expected_source_revision < 0:
            raise ValueError(
                "legacy document revision must be an exact nonnegative integer"
            )
        if expected_destination_revision is not None and (
            type(expected_destination_revision) is not int
            or expected_destination_revision < 0
        ):
            raise ValueError(
                "destination document revision must be an exact nonnegative integer"
            )
        source = self.path if legacy_path is None else Path(legacy_path)
        candidate = source.expanduser()
        if candidate.is_symlink():
            # Checked before resolve(): a link must never collapse into a
            # silent same-path transfer or leak the adoption target.
            raise ValueError("legacy settings path must not be a symbolic link")
        resolved_source = candidate.resolve()
        resolved_owner = self.path.expanduser().resolve()
        if resolved_source in {
            self.backup_path.expanduser().resolve(),
            self.lock_path.expanduser().resolve(),
        }:
            raise ValueError("legacy settings path selects owner control files")
        same_path = resolved_source == resolved_owner
        fingerprint = canonical_digest(
            {
                "migration_id": normalized_id,
                "legacy_writer_state": legacy_writer_state,
                "expected_source_revision": expected_source_revision,
                "expected_destination_revision": expected_destination_revision,
                "legacy_path": str(resolved_source),
            }
        )

        def migrations_of(document: Mapping[str, Any]) -> dict[str, Any]:
            records = document.get(OWNER_MIGRATIONS_KEY, {})
            if not isinstance(records, dict):
                raise FrontendSettingsCorruptError(
                    "settings owner migrations are corrupt"
                )
            return records

        def replay_record(document: Mapping[str, Any]) -> dict[str, Any] | None:
            record = migrations_of(document).get(normalized_id)
            if record is None:
                return None
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("result"), dict)
            ):
                raise FrontendSettingsCorruptError(
                    "settings owner migration record is corrupt"
                )
            if record.get("fingerprint") != fingerprint:
                raise FrontendSettingsIdempotencyConflict(
                    "migration_id was already used for a different migration"
                )
            replay = deepcopy(record["result"])
            replay["idempotent_replay"] = True
            # Retirement happens after the durable commit, so its outcome is
            # not part of the record; it is re-derived from the source path
            # (always ``None`` for a same-path transfer, which retires
            # nothing).
            replay["legacy_retired"] = (
                None if same_path else not resolved_source.exists()
            )
            return replay

        # A recorded migration replays from the owner record alone, even after
        # a distinct source was retired; source availability is checked only
        # for a first-time adoption below.
        early = replay_record(self.read_snapshot())
        if early is not None:
            return early

        def read_source_locked(
            current: Mapping[str, Any],
        ) -> tuple[dict[str, Any], str]:
            """Read and pin the legacy document inside the owner lock.

            A distinct source is additionally covered by its own store lock
            (owner first, then source — a fixed order no other path reverses),
            so a still-running same-store writer holds the migration off or
            fails its deadline instead of being copied mid-write.
            """
            if same_path:
                if not self.path.is_file():
                    raise ValueError("legacy settings document is unavailable")
                raw = self.path.read_bytes()
                # ``current`` was already decoded under this same lock; the
                # bytes are read once here only for the evidence digest.
                return dict(current), "sha256:" + hashlib.sha256(raw).hexdigest()
            legacy_store = FrontendSettingsStore(resolved_source)
            with legacy_store._locked(
                cancellation=lock_cancellation,
                deadline=lock_deadline,
                fence=lock_fence,
            ):
                raw = resolved_source.read_bytes()
                try:
                    document = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise FrontendSettingsCorruptError(
                        "legacy settings document is corrupt"
                    ) from error
                if not isinstance(document, dict):
                    raise FrontendSettingsCorruptError(
                        "legacy settings document is corrupt"
                    )
                return document, "sha256:" + hashlib.sha256(raw).hexdigest()

        with self._locked(
            cancellation=lock_cancellation,
            deadline=lock_deadline,
            fence=lock_fence,
        ):
            current = self._read_locked(recover=False)
            replay = replay_record(current)
            if replay is not None:
                return replay
            owner_revision = current.get(REVISION_KEY, 0)
            if type(owner_revision) is not int or owner_revision < 0:
                raise FrontendSettingsCorruptError(
                    "settings document revision is invalid"
                )
            if (
                expected_destination_revision is not None
                and owner_revision != expected_destination_revision
            ):
                raise FrontendSettingsRevisionConflict(
                    "settings.document",
                    expected_destination_revision,
                    owner_revision,
                )
            # The destination conflict is decided before source availability:
            # a caller holding the wrong destination revision gets the honest
            # revision conflict, not a source-path error.
            if not same_path and not resolved_source.is_file():
                raise ValueError("legacy settings document is unavailable")
            document, source_digest = read_source_locked(current)
            source_revision = document.get(REVISION_KEY, 0)
            if type(source_revision) is not int or source_revision < 0:
                raise FrontendSettingsCorruptError(
                    "legacy document revision is invalid"
                )
            if source_revision != expected_source_revision:
                raise FrontendSettingsRevisionConflict(
                    "settings.document", expected_source_revision, source_revision
                )
            adopted = deepcopy(document)
            result = {
                "migration_id": normalized_id,
                "mode": "same_path" if same_path else "adopted",
                "source_digest": source_digest,
                "source_revision": source_revision,
                "document_revision": owner_revision + 1,
                "migrated": True,
            }
            records = {
                **migrations_of(document),
                **migrations_of(current),
                normalized_id: {
                    "fingerprint": fingerprint,
                    "legacy_writer_state": legacy_writer_state,
                    "result": deepcopy(result),
                },
            }
            while len(records) > MAX_OWNER_MIGRATIONS:
                records.pop(next(iter(records)))
            adopted[OWNER_MIGRATIONS_KEY] = records
            adopted[REVISION_KEY] = owner_revision + 1
            _assert_lock_wait_active(
                cancellation=lock_cancellation,
                deadline=lock_deadline,
                fence=lock_fence,
            )
            self._atomic_write(adopted, preserve_backup=True)

        response = {**result, "idempotent_replay": False}
        if same_path:
            response["legacy_retired"] = None
        else:
            # The commit is durable before the source is fenced. A retire
            # failure is reported, never silently hidden or rolled back.
            retired = resolved_source.with_name(
                f"{resolved_source.name}.migrated-{source_digest.removeprefix('sha256:')}"
            )
            try:
                os.replace(resolved_source, retired)
            except OSError:
                response["legacy_retired"] = False
            else:
                response["legacy_retired"] = True
        return response

    @contextmanager
    def _locked(
        self,
        *,
        cancellation: Any | None = None,
        deadline: float | None = None,
        fence: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = _thread_lock(self.path)
        _acquire_thread_lock(
            thread_lock,
            cancellation=cancellation,
            deadline=deadline,
            fence=fence,
        )
        try:
            _assert_lock_wait_active(
                cancellation=cancellation, deadline=deadline, fence=fence,
            )
            with self.lock_path.open("a+b") as lock_file:
                _lock_file_handle(
                    lock_file,
                    cancellation=cancellation,
                    deadline=deadline,
                    fence=fence,
                )
                try:
                    _assert_lock_wait_active(
                        cancellation=cancellation, deadline=deadline, fence=fence,
                    )
                    yield
                finally:
                    _unlock_file_handle(lock_file)
        finally:
            thread_lock.release()

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


def _assert_lock_wait_active(
    *,
    cancellation: Any | None,
    deadline: float | None,
    fence: Callable[[], None] | None,
) -> None:
    if cancellation is not None and cancellation.is_set():
        raise InterruptedError("frontend settings lock wait was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("frontend settings lock deadline exceeded")
    if fence is not None:
        fence()


def _lock_wait_delay(deadline: float | None) -> float:
    if deadline is None:
        return _LOCK_POLL_SECONDS
    return max(0.0, min(_LOCK_POLL_SECONDS, deadline - time.monotonic()))


def _acquire_thread_lock(
    lock: threading.RLock,
    *,
    cancellation: Any | None,
    deadline: float | None,
    fence: Callable[[], None] | None,
) -> None:
    if cancellation is None and deadline is None:
        lock.acquire()
        return
    while True:
        _assert_lock_wait_active(
            cancellation=cancellation, deadline=deadline, fence=fence,
        )
        if lock.acquire(timeout=_lock_wait_delay(deadline)):
            return


def _lock_file_handle(
    handle: Any,
    *,
    cancellation: Any | None = None,
    deadline: float | None = None,
    fence: Callable[[], None] | None = None,
) -> None:
    if sys.platform == "win32":
        import msvcrt

        _ensure_lock_byte(handle)
        handle.seek(0)
        attempts = 0
        while True:
            _assert_lock_wait_active(
                cancellation=cancellation, deadline=deadline, fence=fence,
            )
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                attempts += 1
                if cancellation is None and deadline is None and attempts >= 400:
                    raise TimeoutError("timed out acquiring frontend settings lock")
                time.sleep(_lock_wait_delay(deadline))
    import fcntl

    # A missing/failed OS lock must never allow recovery or mutation to run.
    if cancellation is None and deadline is None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    while True:
        _assert_lock_wait_active(
            cancellation=cancellation, deadline=deadline, fence=fence,
        )
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            time.sleep(_lock_wait_delay(deadline))


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
