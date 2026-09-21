from __future__ import annotations

import copy
import errno
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import SCHEMA_VERSION, normalize_record, sanitize_change_request_id, utc_now


STORE_ENV_VAR = "RUMI_DEFAULTSPACK_CHANGE_REQUEST_STORE_PATH"
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[str, threading.RLock] = {}
_RECORD_LOCK_REGISTRY: dict[str, threading.RLock] = {}
_MAX_MUTATION_RECEIPTS = 128


class ChangeRequestRevisionConflict(ValueError):
    """The mutation was composed against a record that is no longer current."""

    code = "CHANGE_REQUEST_REVISION_CONFLICT"


class ChangeRequestIdempotencyConflict(ValueError):
    """An idempotency key was reused for a different change-request mutation."""

    code = "CHANGE_REQUEST_IDEMPOTENCY_CONFLICT"


def default_storage_path() -> Path:
    override = os.environ.get(STORE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "change_requests.json"


def _lock_for_path(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCK_REGISTRY[key] = lock
        return lock


def _lock_for_record(path: Path, change_request_id: str) -> threading.RLock:
    key = f"{path.expanduser().resolve()}::{change_request_id}"
    with _LOCK_REGISTRY_GUARD:
        lock = _RECORD_LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.RLock()
            _RECORD_LOCK_REGISTRY[key] = lock
        return lock


class ChangeRequestStore:
    def __init__(self, storage_path: str | os.PathLike[str] | None = None) -> None:
        self._storage_path = Path(storage_path).expanduser() if storage_path else default_storage_path()
        self._lock = _lock_for_path(self._storage_path)

    @property
    def storage_path(self) -> Path:
        return self._storage_path

    def check_log_path(self, change_request_id: str, check_id: str) -> Path:
        cr_id = sanitize_change_request_id(change_request_id)
        check_segment = _sanitize_artifact_segment(check_id, label="check id")
        return self._storage_path.parent / "change_request_logs" / cr_id / "checks" / f"{check_segment}.log"

    def write_check_log(self, change_request_id: str, check_id: str, log_text: str) -> Path:
        path = self.check_log_path(change_request_id, check_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(log_text or ""))
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_atomic_file(Path(tmp_name), path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    def list(self) -> list[dict[str, Any]]:
        data = self._load()
        records = [copy.deepcopy(record) for record in data["change_requests"].values()]
        records.sort(key=lambda item: (item.get("updated_at") or "", item.get("created_at") or ""), reverse=True)
        return records

    def get(self, change_request_id: str) -> dict[str, Any] | None:
        cr_id = sanitize_change_request_id(change_request_id)
        data = self._load()
        record = data["change_requests"].get(cr_id)
        return copy.deepcopy(record) if record else None

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_record(record)
        with self._lock:
            data = self._load_unlocked()
            if normalized["id"] in data["change_requests"]:
                raise ValueError("change request id already exists: " + normalized["id"])
            data["change_requests"][normalized["id"]] = normalized
            data["updated_at"] = utc_now()
            self._save_unlocked(data)
        return copy.deepcopy(normalized)

    def update(self, change_request_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        cr_id = sanitize_change_request_id(change_request_id)
        updates = dict(updates or {})
        with self._lock:
            data = self._load_unlocked()
            record = data["change_requests"].get(cr_id)
            if record is None:
                raise KeyError(cr_id)
            record.update(updates)
            record["updated_at"] = utc_now()
            normalized = normalize_record(record)
            data["change_requests"][cr_id] = normalized
            data["updated_at"] = normalized["updated_at"]
            self._save_unlocked(data)
        return copy.deepcopy(normalized)

    def mutate(self, change_request_id: str, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        cr_id = sanitize_change_request_id(change_request_id)
        with self._lock:
            data = self._load_unlocked()
            record = data["change_requests"].get(cr_id)
            if record is None:
                raise KeyError(cr_id)
            draft = copy.deepcopy(record)
            result = mutator(draft)
            next_record = result if isinstance(result, dict) else draft
            next_record["updated_at"] = utc_now()
            normalized = normalize_record(next_record)
            data["change_requests"][cr_id] = normalized
            data["updated_at"] = normalized["updated_at"]
            self._save_unlocked(data)
        return copy.deepcopy(normalized)

    def mutate_with_revision(
        self,
        change_request_id: str,
        *,
        expected_revision: int,
        expected_snapshot_working_tree_hash: str,
        expected_current_working_tree_hash: str,
        current_working_tree_hash: str,
        idempotency_key: str,
        fingerprint: str,
        mutator: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically apply a revision-bound mutation or replay its prior result.

        The fingerprint is deliberately server-generated by the caller and binds a
        key to its operation and payload.  A client cannot turn a duplicate key
        into a new mutation by changing only the action payload.
        """
        cr_id = sanitize_change_request_id(change_request_id)
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise ChangeRequestRevisionConflict("expected_revision is required")
        if not expected_snapshot_working_tree_hash or not expected_current_working_tree_hash:
            raise ChangeRequestRevisionConflict("expected review snapshot and current tree hashes are required")
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency_key is required and must be at most 256 characters")

        record_lock = _lock_for_record(self._storage_path, cr_id)
        with record_lock:
            with self._lock:
                data = self._load_unlocked()
                record = data["change_requests"].get(cr_id)
                if record is None:
                    raise KeyError(cr_id)

                receipts = record.get("mutation_receipts")
                receipts = receipts if isinstance(receipts, dict) else {}
                previous = receipts.get(idempotency_key)
                if isinstance(previous, dict):
                    if previous.get("fingerprint") != fingerprint:
                        raise ChangeRequestIdempotencyConflict("idempotency_key was already used for a different mutation")
                    result = previous.get("result")
                    return copy.deepcopy(record), copy.deepcopy(result if isinstance(result, dict) else {}), True

                snapshot = record.get("latest_snapshot") if isinstance(record.get("latest_snapshot"), dict) else {}
                snapshot_hash = str(snapshot.get("working_tree_hash") or "")
                revision = record.get("revision")
                revision = revision if isinstance(revision, int) and revision > 0 else 1
                if (
                    revision != expected_revision
                    or snapshot_hash != expected_snapshot_working_tree_hash
                    or current_working_tree_hash != expected_current_working_tree_hash
                ):
                    raise ChangeRequestRevisionConflict("review changed or working tree no longer matches the reviewed snapshot")

                # Work from an immutable snapshot. The per-record lock serializes
                # mutations for this review while the store lock is released, so
                # slow checks and Git operations cannot block unrelated reviews.
                draft = copy.deepcopy(record)

            result_record, result = mutator(draft)
            next_record = result_record if isinstance(result_record, dict) else draft
            with self._lock:
                data = self._load_unlocked()
                current = data["change_requests"].get(cr_id)
                if current is None:
                    raise KeyError(cr_id)
                current_revision = current.get("revision")
                current_revision = current_revision if isinstance(current_revision, int) and current_revision > 0 else 1
                current_snapshot = current.get("latest_snapshot") if isinstance(current.get("latest_snapshot"), dict) else {}
                if current_revision != revision or str(current_snapshot.get("working_tree_hash") or "") != snapshot_hash:
                    raise ChangeRequestRevisionConflict("review changed while the mutation was running")

                next_record["revision"] = revision + 1
                next_record["updated_at"] = utc_now()
                next_receipts = next_record.get("mutation_receipts")
                next_receipts = dict(next_receipts) if isinstance(next_receipts, dict) else {}
                next_receipts[idempotency_key] = {"fingerprint": fingerprint, "result": copy.deepcopy(result)}
                if len(next_receipts) > _MAX_MUTATION_RECEIPTS:
                    oldest = next(iter(next_receipts))
                    next_receipts.pop(oldest, None)
                next_record["mutation_receipts"] = next_receipts
                normalized = normalize_record(next_record)
                data["change_requests"][cr_id] = normalized
                data["updated_at"] = normalized["updated_at"]
                self._save_unlocked(data)
            return copy.deepcopy(normalized), copy.deepcopy(result), False

    def _load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_payload()
        except Exception:
            return self._empty_payload()
        return self._normalize_payload(payload)

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "change_requests": {},
        }

    def _normalize_payload(self, payload: Any) -> dict[str, Any]:
        data = self._empty_payload()
        if not isinstance(payload, dict):
            return data
        raw_records = payload.get("change_requests", {})
        if isinstance(raw_records, list):
            raw_records = {
                str(item.get("id")): item
                for item in raw_records
                if isinstance(item, dict) and item.get("id")
            }
        if isinstance(raw_records, dict):
            for key, raw in raw_records.items():
                if not isinstance(raw, dict):
                    continue
                raw = dict(raw)
                raw.setdefault("id", key)
                try:
                    record = normalize_record(raw)
                except ValueError:
                    continue
                data["change_requests"][record["id"]] = record
        data["schema_version"] = int(payload.get("schema_version") or SCHEMA_VERSION)
        data["updated_at"] = payload.get("updated_at")
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._storage_path.parent),
            prefix="." + self._storage_path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_atomic_file(Path(tmp_name), self._storage_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _is_transient_replace_error(exc: OSError) -> bool:
        winerror = getattr(exc, "winerror", None)
        errno_value = getattr(exc, "errno", None)
        if isinstance(exc, PermissionError):
            return True
        if winerror in {5, 32}:
            return True
        if errno_value in {errno.EACCES, errno.EBUSY, errno.EPERM}:
            return True
        message = str(exc).lower()
        return "access is denied" in message or "permission denied" in message

    def _replace_atomic_file(self, tmp_path: Path, path: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(8):
            try:
                tmp_path.replace(path)
                return
            except OSError as exc:
                last_error = exc
                if not self._is_transient_replace_error(exc) or attempt >= 7:
                    break
                time.sleep(min(0.05 * (2 ** attempt), 0.5))
        if last_error is not None:
            raise last_error


def _sanitize_artifact_segment(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not text or any(ch not in allowed for ch in text):
        raise ValueError(label + " is invalid")
    return text
