from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SECRET_FRAGMENTS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
}


class OfflineQueueError(ValueError):
    """Raised when an operation cannot safely enter the offline queue."""


class OfflineQueueConflict(OfflineQueueError):
    """Raised when an idempotency key is reused for another request."""


class OfflineOperationQueue:
    """Persist replayable desired-state mutations with explicit CAS metadata."""

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 30,
        max_queued_per_owner: int = 1000,
        max_request_bytes: int = 64 * 1024,
    ) -> None:
        self.path = path
        if (
            isinstance(retention_days, bool)
            or retention_days < 1
            or retention_days > 365
        ):
            raise OfflineQueueError("retention_days must be between 1 and 365")
        self._lock = threading.RLock()
        self.max_queued_per_owner = max_queued_per_owner
        self.max_request_bytes = max_request_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        _restrict_sqlite_files(self.path)
        self._prune_terminal(
            (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        )

    def enqueue(
        self,
        *,
        command: dict[str, Any],
        args: dict[str, Any],
        idempotency_key: str,
        expected_revision: int,
        owner_key: str = "local",
        pack_generation: int = 1,
    ) -> dict[str, Any]:
        """Queue an idempotent state mutation or return its existing receipt."""

        canonical_id = str(command.get("canonical_id") or "").strip()
        execution = (
            command.get("execution")
            if isinstance(command.get("execution"), dict)
            else {}
        )
        authorization = (
            command.get("authorization")
            if isinstance(command.get("authorization"), dict)
            else {}
        )
        offline = (
            execution.get("offline")
            if isinstance(execution.get("offline"), dict)
            else {}
        )
        if (
            execution.get("kind") != "state_mutation"
            or offline.get("queueable") is not True
            or offline.get("backend_authoritative") is not True
            or offline.get("semantics") != "set"
        ):
            raise OfflineQueueError(
                "operation is not registered as a backend-authoritative offline set"
            )
        if authorization.get("approval_required"):
            raise OfflineQueueError("approval-required commands may not be queued")
        mutation = (
            execution.get("mutation")
            if isinstance(execution.get("mutation"), dict)
            else {}
        )
        argument = str(mutation.get("argument") or "").strip()
        if mutation.get("when_present") != "set" or not argument:
            raise OfflineQueueError("queued mutations must use explicit set(value)")
        if argument not in args:
            raise OfflineQueueError(
                f"queued mutation requires the desired-state argument: {argument}"
            )
        if set(args) != {argument}:
            raise OfflineQueueError(
                "queued mutation contains fields outside its registered schema"
            )
        if _contains_secret(args):
            raise OfflineQueueError("secret-bearing operations may not be queued")
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise OfflineQueueError("expected_revision must be a non-negative integer")
        normalized_key = str(idempotency_key or "").strip()
        if len(normalized_key) < 8 or len(normalized_key) > 256:
            raise OfflineQueueError("idempotency_key must be 8-256 characters")

        request = {
            "command_ref": canonical_id,
            "args": args,
            "expected_revision": expected_revision,
            "idempotency_key": normalized_key,
            "pack_generation": int(pack_generation),
        }
        encoded = _canonical_json(request)
        if len(encoded.encode("utf-8")) > self.max_request_bytes:
            raise OfflineQueueError("offline request exceeds the size limit")
        request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        queue_id = f"offline_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = _owner_key(owner_key)
            existing = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json, created_at, updated_at
                FROM offline_operations
                WHERE owner_key = ? AND idempotency_key = ?
                """,
                (owner, normalized_key),
            ).fetchone()
            if existing is not None:
                if str(existing[1]) != request_hash:
                    raise OfflineQueueConflict(
                        "idempotency_key was reused for a different operation"
                    )
                connection.commit()
                return _row_to_record(existing)
            queued_count = connection.execute(
                """
                SELECT COUNT(*) FROM offline_operations
                WHERE owner_key = ?
                  AND state IN ('queued', 'replaying', 'effect_committing')
                """,
                (owner,),
            ).fetchone()
            if int(queued_count[0]) >= self.max_queued_per_owner:
                raise OfflineQueueError("offline queue quota exceeded")
            connection.execute(
                """
                INSERT INTO offline_operations (
                    queue_id, owner_key, idempotency_key, request_hash, state,
                    request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    queue_id,
                    owner,
                    normalized_key,
                    request_hash,
                    encoded,
                    now,
                    now,
                ),
            )
            connection.commit()
        return {
            "queue_id": queue_id,
            "request_hash": request_hash,
            "state": "queued",
            "request": request,
            "created_at": now,
            "updated_at": now,
        }

    def pending(
        self,
        *,
        limit: int = 100,
        owner_key: str = "local",
    ) -> list[dict[str, Any]]:
        """Return queued operations in deterministic replay order."""

        if isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise OfflineQueueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json, created_at, updated_at
                FROM offline_operations
                WHERE state = 'queued' AND owner_key = ?
                ORDER BY created_at ASC, queue_id ASC
                LIMIT ?
                """,
                (_owner_key(owner_key), limit),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def claim_pending(
        self,
        *,
        owner_key: str,
        worker_id: str,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Atomically lease queued or expired-replay rows."""

        if limit < 1 or limit > 1000:
            raise OfflineQueueError("limit must be between 1 and 1000")
        owner = _owner_key(owner_key)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(10, lease_seconds))).isoformat()
        lease_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT queue_id
                FROM offline_operations
                WHERE owner_key = ?
                  AND (
                    state = 'queued'
                    OR (state = 'replaying' AND lease_expires_at < ?)
                  )
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC, queue_id ASC
                LIMIT ?
                """,
                (owner, now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            for queue_id in ids:
                connection.execute(
                    """
                    UPDATE offline_operations
                    SET state = 'replaying', lease_id = ?, worker_id = ?,
                        claimed_at = ?, lease_expires_at = ?,
                        attempt_count = attempt_count + 1, updated_at = ?
                    WHERE queue_id = ? AND owner_key = ?
                    """,
                    (
                        lease_id,
                        str(worker_id),
                        now.isoformat(),
                        expires,
                        now.isoformat(),
                        queue_id,
                        owner,
                    ),
                )
            claimed = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json, created_at, updated_at
                FROM offline_operations
                WHERE owner_key = ? AND lease_id = ?
                ORDER BY created_at ASC, queue_id ASC
                """,
                (owner, lease_id),
            ).fetchall()
            connection.commit()
        result = [_row_to_record(row) for row in claimed]
        for record in result:
            record["lease_id"] = lease_id
        return result

    def record_result(
        self,
        queue_id: str,
        *,
        state: str,
        result: dict[str, Any],
        owner_key: str = "local",
        lease_id: str,
    ) -> dict[str, Any]:
        """Record a replay terminal result without crossing the effect barrier.

        Results from ``effect_committing`` remain valid after lease expiry: the
        lease identifies the worker that owns the non-retryable effect, and no
        worker can reclaim that state. A concurrent reconciliation process may
        instead terminalize an expired record as ``reconciliation_required``.
        """

        if state not in {"completed", "conflicted", "cancelled", "failed"}:
            raise OfflineQueueError("invalid terminal queue state")
        now = datetime.now(timezone.utc).isoformat()
        encoded_result = _canonical_json(_redact(result))
        permitted_states = ("effect_committing",)
        if state in {"cancelled", "conflicted", "failed"}:
            permitted_states = ("replaying", "effect_committing")
        placeholders = ", ".join("?" for _ in permitted_states)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE offline_operations
                SET state = ?, result_json = ?, updated_at = ?
                WHERE queue_id = ? AND owner_key = ?
                  AND state IN ({placeholders}) AND lease_id = ?
                """,
                (
                    state,
                    encoded_result,
                    now,
                    queue_id,
                    _owner_key(owner_key),
                    *permitted_states,
                    str(lease_id),
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT queue_id, request_hash, state, request_json,
                           created_at, updated_at, result_json
                    FROM offline_operations
                    WHERE queue_id = ? AND owner_key = ?
                    """,
                    (queue_id, _owner_key(owner_key)),
                ).fetchone()
                connection.commit()
                if row is not None and str(row[2]) == "cancelled":
                    return _row_to_record(row)
                raise OfflineQueueError("queued operation was not found or is terminal")
            row = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json,
                       created_at, updated_at, result_json
                FROM offline_operations
                WHERE queue_id = ? AND owner_key = ?
                """,
                (queue_id, _owner_key(owner_key)),
            ).fetchone()
            connection.commit()
        return _row_to_record(row)

    def begin_effect_commit(
        self,
        queue_id: str,
        *,
        owner_key: str,
        lease_id: str,
    ) -> dict[str, Any]:
        """Atomically cross the durable barrier immediately before an effect.

        A cancellation that commits first changes the record to ``cancelled``.
        Once this transition commits, replay must invoke the backend exactly once;
        later cancellation requests are durably marked but explicitly too late.
        """

        now = datetime.now(timezone.utc).isoformat()
        owner = _owner_key(owner_key)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE offline_operations
                SET state = 'effect_committing', effect_committing_at = ?,
                    updated_at = ?
                WHERE queue_id = ? AND owner_key = ?
                  AND state = 'replaying' AND lease_id = ?
                  AND cancel_requested = 0
                  AND lease_expires_at >= ?
                """,
                (
                    now,
                    now,
                    str(queue_id),
                    owner,
                    str(lease_id),
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json,
                       created_at, updated_at, result_json
                FROM offline_operations
                WHERE queue_id = ? AND owner_key = ?
                """,
                (str(queue_id), owner),
            ).fetchone()
            connection.commit()
        if row is None:
            raise OfflineQueueError("queued operation was not found")
        record = _row_to_record(row)
        if cursor.rowcount == 1:
            return {"status": "effect_committing", "queue": record}
        if record["state"] == "cancelled":
            return {"status": "cancelled", "queue": record}
        raise OfflineQueueError(
            "queued operation cannot enter the effect commit barrier"
        )

    def reconcile_expired_effect_commits(
        self,
        *,
        owner_key: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Expose expired effect barriers without replaying an unknown effect.

        An ``effect_committing`` record means a backend invocation may already
        have happened. A worker crash therefore becomes an explicit
        reconciliation task instead of an automatic retry that could duplicate
        the effect.
        """

        if isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise OfflineQueueError("limit must be between 1 and 1000")
        now = datetime.now(timezone.utc).isoformat()
        owner = _owner_key(owner_key)
        encoded_result = _canonical_json(
            {
                "api_version": "tobkiri.commands/v1",
                "status": "reconciliation_required",
                "state_changes": [],
                "error": {
                    "code": "EFFECT_OUTCOME_UNKNOWN",
                    "message": (
                        "effect commit lease expired before its outcome "
                        "was recorded"
                    ),
                },
            }
        )
        records: list[dict[str, Any]] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT queue_id
                FROM offline_operations
                WHERE owner_key = ? AND state = 'effect_committing'
                  AND lease_expires_at < ?
                ORDER BY created_at ASC, queue_id ASC
                LIMIT ?
                """,
                (owner, now, limit),
            ).fetchall()
            for row in rows:
                queue_id = str(row[0])
                cursor = connection.execute(
                    """
                    UPDATE offline_operations
                    SET state = 'reconciliation_required', result_json = ?,
                        updated_at = ?
                    WHERE queue_id = ? AND owner_key = ?
                      AND state = 'effect_committing'
                      AND lease_expires_at < ?
                    """,
                    (encoded_result, now, queue_id, owner, now),
                )
                if cursor.rowcount != 1:
                    continue
                current = connection.execute(
                    """
                    SELECT queue_id, request_hash, state, request_json,
                           created_at, updated_at, result_json
                    FROM offline_operations
                    WHERE queue_id = ? AND owner_key = ?
                    """,
                    (queue_id, owner),
                ).fetchone()
                if current is not None:
                    records.append(_row_to_record(current))
            connection.commit()
        return records

    def cancellation_requested(
        self,
        queue_id: str,
        *,
        owner_key: str,
        lease_id: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cancel_requested
                FROM offline_operations
                WHERE queue_id = ? AND owner_key = ?
                  AND lease_id = ?
                """,
                (str(queue_id), _owner_key(owner_key), str(lease_id)),
            ).fetchone()
        return bool(row and int(row[0] or 0))

    def renew_lease(
        self,
        queue_id: str,
        *,
        owner_key: str,
        lease_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        now = datetime.now(timezone.utc)
        expires = (
            now + timedelta(seconds=max(10, min(3600, lease_seconds)))
        ).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE offline_operations
                SET lease_expires_at = ?, updated_at = ?
                WHERE queue_id = ? AND owner_key = ?
                  AND state = 'replaying' AND lease_id = ?
                """,
                (
                    expires,
                    now.isoformat(),
                    str(queue_id),
                    _owner_key(owner_key),
                    str(lease_id),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def cancel(self, queue_id: str, *, owner_key: str) -> dict[str, Any]:
        """Cancel before the barrier or report a durable too-late outcome."""

        now = datetime.now(timezone.utc).isoformat()
        owner = _owner_key(owner_key)
        cancelled_result = _canonical_json(
            {
                "api_version": "tobkiri.commands/v1",
                "status": "cancelled",
                "state_changes": [],
            }
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json,
                       created_at, updated_at, result_json, effect_committing_at
                FROM offline_operations
                WHERE queue_id = ? AND owner_key = ?
                """,
                (str(queue_id), owner),
            ).fetchone()
            if row is None:
                connection.commit()
                return {"status": "not_found", "too_late": False}
            state = str(row[2])
            crossed_barrier = bool(row[7])
            if state in {"queued", "replaying"}:
                connection.execute(
                    """
                    UPDATE offline_operations
                    SET state = 'cancelled', cancel_requested = 1,
                        result_json = ?, updated_at = ?
                    WHERE queue_id = ? AND owner_key = ?
                      AND state IN ('queued', 'replaying')
                    """,
                    (cancelled_result, now, str(queue_id), owner),
                )
                outcome = {"status": "cancelled", "too_late": False}
            elif state == "cancelled":
                outcome = {"status": "cancelled", "too_late": False}
            elif state == "effect_committing" or crossed_barrier:
                connection.execute(
                    """
                    UPDATE offline_operations
                    SET cancel_requested = 1, updated_at = ?
                    WHERE queue_id = ? AND owner_key = ?
                    """,
                    (now, str(queue_id), owner),
                )
                outcome = {"status": "too_late", "too_late": True}
            else:
                outcome = {"status": "not_cancellable", "too_late": False}
            current = connection.execute(
                """
                SELECT queue_id, request_hash, state, request_json,
                       created_at, updated_at, result_json
                FROM offline_operations
                WHERE queue_id = ? AND owner_key = ?
                """,
                (str(queue_id), owner),
            ).fetchone()
            connection.commit()
        return {**outcome, "queue": _row_to_record(current)}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        _restrict_sqlite_files(self.path)
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS offline_operations (
                    queue_id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    lease_id TEXT,
                    worker_id TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    effect_committing_at TEXT,
                    UNIQUE (owner_key, idempotency_key)
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(offline_operations)"
                ).fetchall()
            }
            migrations = {
                "owner_key": "TEXT NOT NULL DEFAULT 'local'",
                "lease_id": "TEXT",
                "worker_id": "TEXT",
                "claimed_at": "TEXT",
                "lease_expires_at": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "TEXT",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "effect_committing_at": "TEXT",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE offline_operations ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS offline_operations_replay
                ON offline_operations (state, created_at, queue_id)
                """
            )
            connection.commit()

    def _prune_terminal(self, before: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM offline_operations
                WHERE updated_at < ?
                  AND state IN (
                      'completed', 'conflicted', 'cancelled', 'failed',
                      'reconciliation_required'
                  )
                """,
                (before,),
            )
            connection.commit()
            return int(cursor.rowcount)


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    record = {
        "queue_id": str(row[0]),
        "request_hash": str(row[1]),
        "state": str(row[2]),
        "request": json.loads(str(row[3])),
        "created_at": str(row[4]),
        "updated_at": str(row[5]),
    }
    if len(row) > 6 and row[6] is not None:
        record["result"] = json.loads(str(row[6]))
    return record


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            any(fragment in str(key).lower() for fragment in _SECRET_FRAGMENTS)
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(
                    fragment in str(key).lower()
                    for fragment in _SECRET_FRAGMENTS
                )
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _owner_key(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512:
        raise OfflineQueueError("owner_key must be 1-512 characters")
    return normalized


def _restrict_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue
