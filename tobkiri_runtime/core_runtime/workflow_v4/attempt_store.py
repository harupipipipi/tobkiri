"""Encrypted Host-only Workflow attempt journal and conservative CAS fences."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator, Mapping

from cryptography.fernet import Fernet, InvalidToken

from core_runtime.workflow_v4.models import WorkflowDenied, digest
from tobkiri_protocol.secure_persistence import SecureDirectory

_MAX_BYTES = 32 * 1024 * 1024


class WorkflowAttemptStoreV4:
    """Encrypt all private contexts/snapshots; serialize cross-process claims.

    No prepared snapshot or approval record is placed in the public Workflow
    state store. Advisory locks are released by the OS after a crash. A claimed
    or dispatched record survives that crash and cannot be dispatched again.
    """

    def __init__(self, path: Path) -> None:
        self._directory = SecureDirectory(path.parent)
        self._name = path.name
        self._lock_name = f"{path.name}.lock"
        self._key_name = f"{path.name}.key"
        self._thread_lock = threading.RLock()
        with self._locked():
            if not self._directory.exists(self._key_name):
                self._directory.write_bytes_atomic(self._key_name, Fernet.generate_key())
            descriptor = os.open(
                self._directory.root / self._key_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                self._directory.validate_open_file(self._key_name, descriptor)
                if os.fstat(descriptor).st_mode & 0o077:
                    raise WorkflowDenied("Workflow attempt key is not private")
                key = os.read(descriptor, 45)
                self._directory.validate_open_file(self._key_name, descriptor)
            finally:
                os.close(descriptor)
            self._cipher = Fernet(key)
            if not self._directory.exists(self._name):
                self._write({"attempts": {}, "pending": {}})
            self._read()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            descriptor = self._directory.open_lock(self._lock_name)
            deadline = time.monotonic() + 1.0
            locked = False
            try:
                while not locked:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(descriptor, 0, os.SEEK_SET)
                            getattr(msvcrt, "locking")(descriptor, getattr(msvcrt, "LK_NBLCK"), 1)
                        else:
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except OSError as error:
                        if time.monotonic() >= deadline:
                            raise WorkflowDenied("Workflow attempt state is busy") from error
                        time.sleep(0.01)
                self._directory.validate_open_file(self._lock_name, descriptor)
                yield
            finally:
                if locked:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        getattr(msvcrt, "locking")(descriptor, getattr(msvcrt, "LK_UNLCK"), 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        try:
            ciphertext = self._directory.read_bytes_bounded(self._name, max_bytes=_MAX_BYTES * 2)
            document = json.loads(self._cipher.decrypt(ciphertext))
            if not isinstance(document, dict) or set(document) != {"attempts", "pending"}:
                raise ValueError("invalid attempt journal")
            if any(not isinstance(document[key], dict) for key in document):
                raise ValueError("invalid attempt journal")
            return document
        except (InvalidToken, ValueError, OSError) as error:
            raise WorkflowDenied("Workflow attempt state is unavailable") from error

    def _write(self, document: Mapping[str, Any]) -> None:
        raw = json.dumps(
            document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(raw) > _MAX_BYTES:
            raise WorkflowDenied("Workflow attempt journal capacity is exhausted")
        self._directory.write_bytes_atomic(self._name, self._cipher.encrypt(raw))

    def get(self, reservation_id: str) -> tuple[int, dict[str, Any]]:
        """Read an authenticated private record without exposing it to a Pack."""
        with self._locked():
            row = self._read()["attempts"].get(reservation_id)
            if row is None:
                raise WorkflowDenied("Workflow attempt reservation is unavailable")
            return row["revision"], row["payload"]

    def insert(
        self, reservation_id: str, payload: Mapping[str, Any], *, max_active_attempts: int = 64
    ) -> bool:
        """Reserve exact request identity and prevent a second effect retry."""
        with self._locked():
            document = self._read()
            rows = document["attempts"]
            if reservation_id in rows:
                return False
            if (
                sum(not row["payload"].get("context_retired", False) for row in rows.values())
                >= max_active_attempts
            ):
                raise WorkflowDenied("Workflow active attempt capacity is exhausted")
            for row in rows.values():
                previous = row["payload"]
                if previous["idempotency_identity"] == payload["idempotency_identity"] and previous[
                    "state"
                ] not in {"cancelled", "stale"}:
                    raise WorkflowDenied("Workflow step requires reconciliation")
            rows[reservation_id] = {"revision": 1, "payload": dict(payload)}
            self._write(document)
            return True

    def cas(self, reservation_id: str, expected_revision: int, payload: Mapping[str, Any]) -> int:
        """Advance only one exact journal revision under an OS lock."""
        with self._locked():
            document = self._read()
            row = document["attempts"].get(reservation_id)
            if row is None or row["revision"] != expected_revision:
                raise WorkflowDenied("Workflow attempt state changed")
            row.update(revision=expected_revision + 1, payload=dict(payload))
            self._write(document)
            return expected_revision + 1

    def list_attempts(self) -> list[tuple[int, dict[str, Any]]]:
        """Return private records for conservative source-capture recovery."""
        with self._locked():
            return [(row["revision"], row["payload"]) for row in self._read()["attempts"].values()]

    def create_host_pending_effect(self, effect_id: str, payload: Mapping[str, Any]) -> int:
        """Persist a standard encrypted PendingEffectController record."""
        with self._locked():
            document = self._read()
            if effect_id in document["pending"]:
                raise WorkflowDenied("Workflow pending effect already exists")
            document["pending"][effect_id] = {"revision": 1, "payload": dict(payload)}
            self._write(document)
            return 1

    def get_host_pending_effect(self, effect_id: str) -> tuple[int, Mapping[str, Any]] | None:
        """Read only this controller's private encrypted pending namespace."""
        with self._locked():
            row = self._read()["pending"].get(effect_id)
            return None if row is None else (row["revision"], row["payload"])

    def compare_and_swap_host_pending_effect(
        self, effect_id: str, *, expected_revision: int, payload: Mapping[str, Any]
    ) -> int:
        """Apply the existing controller's one-shot lifecycle CAS."""
        with self._locked():
            document = self._read()
            row = document["pending"].get(effect_id)
            if row is None or row["revision"] != expected_revision:
                raise WorkflowDenied("Workflow pending effect changed")
            row.update(revision=expected_revision + 1, payload=dict(payload))
            self._write(document)
            return expected_revision + 1

    def list_host_pending_effects(self) -> list[tuple[int, Mapping[str, Any]]]:
        """Recover this controller's records without mixing other coordinators."""
        with self._locked():
            return [(row["revision"], row["payload"]) for row in self._read()["pending"].values()]


def attempt_identity(profile_id: str, request_id: str) -> str:
    """Derive an opaque Profile-scoped durable association, never authority."""
    return "workflow-attempt." + digest(
        {
            "profile_id": profile_id,
            "request_id": request_id,
        }
    ).removeprefix("sha256:")


class WorkflowCallerPendingPersistenceV4:
    """Filter recovery to one exact captured caller, sharing no foreign store."""

    def __init__(self, store: WorkflowAttemptStoreV4, caller_principal_id: str) -> None:
        self._store = store
        self._caller = caller_principal_id

    def _check(self, payload: Mapping[str, Any]) -> None:
        if payload.get("context", {}).get("caller_principal") != self._caller:
            raise WorkflowDenied("Workflow pending caller is unavailable")

    def create_host_pending_effect(self, effect_id: str, payload: Mapping[str, Any]) -> int:
        """Persist only this exact controller's standard encrypted record."""
        self._check(payload)
        return self._store.create_host_pending_effect(effect_id, payload)

    def get_host_pending_effect(self, effect_id: str) -> tuple[int, Mapping[str, Any]] | None:
        """Read only a record belonging to this exact captured caller."""
        result = self._store.get_host_pending_effect(effect_id)
        if result is not None:
            self._check(result[1])
        return result

    def compare_and_swap_host_pending_effect(
        self, effect_id: str, *, expected_revision: int, payload: Mapping[str, Any]
    ) -> int:
        """Keep caller identity constant while the existing controller CASes."""
        self._check(payload)
        self.get_host_pending_effect(effect_id)
        return self._store.compare_and_swap_host_pending_effect(
            effect_id,
            expected_revision=expected_revision,
            payload=payload,
        )

    def list_host_pending_effects(self) -> list[tuple[int, Mapping[str, Any]]]:
        """Recover only this coordinator principal's pending effects."""
        return [
            (revision, payload)
            for revision, payload in self._store.list_host_pending_effects()
            if payload.get("context", {}).get("caller_principal") == self._caller
        ]
