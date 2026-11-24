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

from core_runtime.hmac_key_manager import SigningKeyError
from core_runtime.private_file import prepare_private_file, verify_private_file
from core_runtime.workflow_v4.attempt_capacity import completion_headroom
from core_runtime.workflow_v4.models import WorkflowDenied, digest
from tobkiri_protocol.secure_persistence import SecureDirectory

_MAX_BYTES = 32 * 1024 * 1024


class WorkflowAttemptReservationMissing(WorkflowDenied):
    """The authenticated journal has no record for this reservation yet."""


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
            try:
                if not self._directory.exists(self._key_name):
                    if self._directory.exists(self._name):
                        raise WorkflowDenied("Workflow attempt key is missing for existing state")
                    self._directory.write_bytes_atomic(
                        self._key_name,
                        Fernet.generate_key(),
                        prepare_new_file=prepare_private_file,
                        create_only=True,
                    )
                key = self._directory.read_bytes_bounded(
                    self._key_name, max_bytes=44, verify_open_file=verify_private_file
                )
                self._cipher = Fernet(key)
            except (OSError, SigningKeyError, ValueError) as error:
                raise WorkflowDenied("Workflow attempt key is not private or valid") from error
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

    def _write(
        self, document: Mapping[str, Any], *, reserve_completion: bool = False
    ) -> None:
        raw = json.dumps(
            document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        # Only new admissions must fund all active records through completion.
        # The one-shot service/controller lifecycles consume this allowance.
        # Keep the raw cap for CAS too, without retroactively requiring legacy
        # journals to have reserved space they were never promised.
        headroom = completion_headroom(document) if reserve_completion else 0
        if len(raw) + headroom > _MAX_BYTES:
            raise WorkflowDenied("Workflow attempt journal capacity is exhausted")
        self._directory.write_bytes_atomic(self._name, self._cipher.encrypt(raw))

    def get(self, reservation_id: str) -> tuple[int, dict[str, Any]]:
        """Read an authenticated private record without exposing it to a Pack."""
        with self._locked():
            row = self._read()["attempts"].get(reservation_id)
            if row is None:
                raise WorkflowAttemptReservationMissing(
                    "Workflow attempt reservation is unavailable"
                )
            return row["revision"], row["payload"]

    def insert(self, reservation_id: str, payload: Mapping[str, Any]) -> bool:
        """Reserve exact request identity and prevent a second effect retry."""
        with self._locked():
            document = self._read()
            rows = document["attempts"]
            if reservation_id in rows:
                return False
            for row in rows.values():
                previous = row["payload"]
                if previous["idempotency_identity"] == payload["idempotency_identity"] and previous[
                    "state"
                ] not in {"cancelled", "stale"}:
                    raise WorkflowDenied("Workflow step requires reconciliation")
            rows[reservation_id] = {"revision": 1, "payload": dict(payload)}
            self._write(document, reserve_completion=True)
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
            self._write(document, reserve_completion=True)
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
