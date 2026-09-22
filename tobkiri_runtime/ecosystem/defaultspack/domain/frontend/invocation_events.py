from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EVENT_TYPES = {
    "accepted",
    "validating",
    "waiting_for_lock",
    "approval_required",
    "queued",
    "started",
    "progress",
    "partial_result",
    "state_committed",
    "completed",
    "failed",
    "cancelled",
    "conflicted",
    "expired",
}
TERMINAL_EVENT_TYPES = {"completed", "failed", "cancelled", "conflicted", "expired"}
_SECRET_FRAGMENTS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(?:x-auth|api-key)\s*:\s*[^\s'\"]+"),
    re.compile(
        r"-----BEGIN [^-]+ PRIVATE KEY-----.*?"
        r"-----END [^-]+ PRIVATE KEY-----",
        re.DOTALL,
    ),
)


class InvocationEventError(ValueError):
    """Raised when an invocation event violates the progress contract."""


class InvocationEventStore:
    """Persist ordered, resumable, secret-redacted invocation events."""

    def __init__(
        self,
        path: Path,
        *,
        max_payload_bytes: int = 64 * 1024,
        max_result_bytes: int = 1024 * 1024,
        retention_days: int = 30,
        lease_seconds: int = 300,
    ) -> None:
        self.path = path
        self.max_payload_bytes = max_payload_bytes
        self.max_result_bytes = max_result_bytes
        self.retention_days = retention_days
        self.lease_seconds = lease_seconds
        if max_result_bytes < 1024 or max_result_bytes > 16 * 1024 * 1024:
            raise InvocationEventError(
                "max_result_bytes must be between 1024 and 16777216"
            )
        if lease_seconds < 10 or lease_seconds > 3600:
            raise InvocationEventError("lease_seconds must be between 10 and 3600")
        if (
            isinstance(retention_days, bool)
            or retention_days < 1
            or retention_days > 365
        ):
            raise InvocationEventError("retention_days must be between 1 and 365")
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        _restrict_sqlite_files(self.path)
        self.prune(
            before=(datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(),
            terminal_only=True,
        )

    def append(
        self,
        invocation_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        owner_key: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Append one event and allocate its monotonic invocation sequence."""

        normalized_id = str(invocation_id or "").strip()
        if not normalized_id or len(normalized_id) > 256:
            raise InvocationEventError("invocation_id must be 1-256 characters")
        normalized_type = str(event_type or "").strip()
        if normalized_type not in EVENT_TYPES:
            raise InvocationEventError(f"unsupported event type: {normalized_type}")

        redacted_payload = _redact(payload or {})
        encoded = _canonical_json(redacted_payload)
        if len(encoded.encode("utf-8")) > self.max_payload_bytes:
            raise InvocationEventError("event payload exceeds the configured size limit")
        occurred_at = timestamp or datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if owner_key is None:
                owners = connection.execute(
                    """
                    SELECT owner_key
                    FROM command_invocations
                    WHERE invocation_id = ?
                    LIMIT 2
                    """,
                    (normalized_id,),
                ).fetchall()
                if len(owners) > 1:
                    raise InvocationEventError(
                        "owner_key is required for an ambiguous invocation_id"
                    )
                normalized_owner = (
                    _owner_key(str(owners[0][0])) if owners else "local"
                )
            else:
                normalized_owner = _owner_key(owner_key)
            terminal = connection.execute(
                """
                SELECT event_type
                FROM invocation_events
                WHERE owner_key = ? AND invocation_id = ?
                  AND event_type IN ('completed', 'failed', 'cancelled', 'conflicted', 'expired')
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (normalized_owner, normalized_id),
            ).fetchone()
            if terminal is not None:
                raise InvocationEventError(
                    f"invocation already terminated with {terminal[0]}"
                )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM invocation_events
                WHERE owner_key = ? AND invocation_id = ?
                """,
                (normalized_owner, normalized_id),
            ).fetchone()
            sequence = int(row[0])
            connection.execute(
                """
                INSERT INTO invocation_events (
                    owner_key, invocation_id, sequence, event_type,
                    occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_owner,
                    normalized_id,
                    sequence,
                    normalized_type,
                    occurred_at,
                    encoded,
                ),
            )
            connection.commit()
        return {
            "invocation_id": normalized_id,
            "sequence": sequence,
            "type": normalized_type,
            "timestamp": occurred_at,
            "payload": redacted_payload,
        }

    def claim(
        self,
        invocation_id: str,
        payload: dict[str, Any],
        *,
        owner_key: str = "local",
        request_fingerprint: str = "",
    ) -> bool:
        """Atomically create the first accepted event for one invocation."""

        normalized_id = str(invocation_id or "").strip()
        if not normalized_id or len(normalized_id) > 256:
            raise InvocationEventError("invocation_id must be 1-256 characters")
        redacted_payload = _redact(payload)
        encoded = _canonical_json(redacted_payload)
        if len(encoded.encode("utf-8")) > self.max_payload_bytes:
            raise InvocationEventError("event payload exceeds the configured size limit")
        occurred_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT 1
                FROM command_invocations
                WHERE owner_key = ? AND invocation_id = ?
                LIMIT 1
                """,
                (_owner_key(owner_key), normalized_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO command_invocations (
                    invocation_id, owner_key, request_fingerprint, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    normalized_id,
                    _owner_key(owner_key),
                    str(request_fingerprint or ""),
                    occurred_at,
                    occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO invocation_events (
                    owner_key, invocation_id, sequence, event_type,
                    occurred_at, payload_json
                ) VALUES (?, ?, 1, 'accepted', ?, ?)
                """,
                (_owner_key(owner_key), normalized_id, occurred_at, encoded),
            )
            connection.commit()
        return True

    def claim_resume(
        self,
        invocation_id: str,
        *,
        owner_key: str,
        request_fingerprint: str,
        lease_id: str,
    ) -> bool:
        """Atomically claim one approval continuation."""

        current = datetime.now(timezone.utc)
        now = current.isoformat()
        stale_before = (current - timedelta(seconds=self.lease_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE command_invocations
                SET state = 'resuming', lease_id = ?, updated_at = ?
                WHERE invocation_id = ?
                  AND owner_key = ?
                  AND request_fingerprint = ?
                  AND (
                    state = 'approval_required'
                    OR (state = 'resuming' AND updated_at < ?)
                  )
                """,
                (
                    str(lease_id),
                    now,
                    str(invocation_id),
                    _owner_key(owner_key),
                    str(request_fingerprint),
                    stale_before,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def set_state(
        self,
        invocation_id: str,
        state: str,
        *,
        owner_key: str,
        result: dict[str, Any] | None = None,
        approval_request_id: str | None = None,
        expected_states: set[str] | None = None,
        lease_id: str | None = None,
    ) -> None:
        """Durably settle invocation state/result independently of audit events."""

        safe_result = _redact(result) if result is not None else None
        encoded_result = _canonical_json(safe_result) if safe_result is not None else None
        if (
            encoded_result is not None
            and len(encoded_result.encode("utf-8")) > self.max_result_bytes
        ):
            raise InvocationEventError(
                "invocation result exceeds the configured size limit"
            )
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            expected = sorted(expected_states or [])
            state_clause = (
                f"AND state IN ({','.join('?' for _ in expected)})"
                if expected
                else ""
            )
            lease_clause = "AND lease_id = ?" if lease_id is not None else ""
            parameters: list[Any] = [
                str(state),
                encoded_result,
                approval_request_id,
                now,
                str(invocation_id),
                _owner_key(owner_key),
                *expected,
            ]
            if lease_id is not None:
                parameters.append(str(lease_id))
            cursor = connection.execute(
                f"""
                UPDATE command_invocations
                SET state = ?, result_json = ?, approval_request_id = ?,
                    lease_id = NULL, updated_at = ?
                WHERE invocation_id = ? AND owner_key = ?
                {state_clause}
                {lease_clause}
                """,
                tuple(parameters),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise InvocationEventError("invocation state transition conflict")

    def recover_stale(
        self,
        invocation_id: str,
        *,
        owner_key: str,
    ) -> str | None:
        """Recover an abandoned lease without repeating an uncertain side effect."""

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.lease_seconds)
        ).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        unknown_result = _canonical_json(
            {
                "api_version": "tobkiri.commands/v1",
                "operation_id": str(invocation_id),
                "status": "failed",
                "state_changes": [],
                "error": {
                    "code": "EXECUTION_OUTCOME_UNKNOWN",
                    "message": (
                        "execution lease expired after side-effect dispatch; "
                        "automatic retry is unsafe"
                    ),
                },
            }
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resumed = connection.execute(
                """
                UPDATE command_invocations
                SET state = 'approval_required', lease_id = NULL, updated_at = ?
                WHERE invocation_id = ? AND owner_key = ?
                  AND state = 'resuming' AND updated_at < ?
                """,
                (now, str(invocation_id), _owner_key(owner_key), cutoff),
            )
            if resumed.rowcount == 1:
                connection.commit()
                return "approval_required"
            executing = connection.execute(
                """
                UPDATE command_invocations
                SET state = 'failed', result_json = ?, lease_id = NULL,
                    updated_at = ?
                WHERE invocation_id = ? AND owner_key = ?
                  AND state = 'executing' AND updated_at < ?
                """,
                (
                    unknown_result,
                    now,
                    str(invocation_id),
                    _owner_key(owner_key),
                    cutoff,
                ),
            )
            connection.commit()
            return "failed" if executing.rowcount == 1 else None

    def mark_executing(
        self,
        invocation_id: str,
        *,
        owner_key: str,
        expected_state: str,
        lease_id: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        lease_clause = "AND lease_id = ?" if lease_id is not None else ""
        parameters: list[Any] = [
            now,
            str(invocation_id),
            _owner_key(owner_key),
            str(expected_state),
        ]
        if lease_id is not None:
            parameters.append(str(lease_id))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE command_invocations
                SET state = 'executing', updated_at = ?
                WHERE invocation_id = ? AND owner_key = ? AND state = ?
                {lease_clause}
                """,
                tuple(parameters),
            )
            connection.commit()
            return cursor.rowcount == 1

    def stored(
        self,
        invocation_id: str,
        *,
        owner_key: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_fingerprint, state, result_json, approval_request_id
                FROM command_invocations
                WHERE invocation_id = ? AND owner_key = ?
                """,
                (str(invocation_id), _owner_key(owner_key)),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_fingerprint": str(row[0] or ""),
            "state": str(row[1]),
            "result": json.loads(str(row[2])) if row[2] else None,
            "approval_request_id": str(row[3] or "") or None,
        }

    def pending_approvals(self, *, owner_key: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT invocation_id, result_json, approval_request_id, updated_at
                FROM command_invocations
                WHERE owner_key = ? AND state = 'approval_required'
                ORDER BY updated_at ASC
                """,
                (_owner_key(owner_key),),
            ).fetchall()
        return [
            {
                "invocation_id": str(row[0]),
                "result": json.loads(str(row[1])) if row[1] else None,
                "approval_request_id": str(row[2] or ""),
                "updated_at": str(row[3]),
            }
            for row in rows
        ]

    def resume(
        self,
        invocation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
        owner_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return events after a Last-Event-ID compatible sequence."""

        if isinstance(after_sequence, bool) or after_sequence < 0:
            raise InvocationEventError("after_sequence must be a non-negative integer")
        if isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise InvocationEventError("limit must be between 1 and 1000")
        normalized_id = str(invocation_id or "").strip()
        owner_clause = ""
        params: list[Any] = [normalized_id, after_sequence]
        if owner_key is not None:
            owner_clause = "AND owner_key = ?"
            params.append(_owner_key(owner_key))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT sequence, event_type, occurred_at, payload_json
                FROM invocation_events
                WHERE invocation_id = ? AND sequence > ?
                {owner_clause}
                ORDER BY sequence ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "invocation_id": normalized_id,
                "sequence": int(row[0]),
                "type": str(row[1]),
                "timestamp": str(row[2]),
                "payload": json.loads(str(row[3])),
            }
            for row in rows
        ]

    def snapshot(
        self,
        invocation_id: str,
        *,
        owner_key: str | None = None,
    ) -> dict[str, Any]:
        """Return the latest event and terminal state for reconnection."""

        normalized_id = str(invocation_id or "").strip()
        owner_clause = ""
        params: list[Any] = [normalized_id]
        if owner_key is not None:
            owner_clause = "AND owner_key = ?"
            params.append(_owner_key(owner_key))
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT sequence, event_type, occurred_at, payload_json
                FROM invocation_events
                WHERE invocation_id = ?
                {owner_clause}
                ORDER BY sequence DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        latest = (
            {
                "invocation_id": normalized_id,
                "sequence": int(row[0]),
                "type": str(row[1]),
                "timestamp": str(row[2]),
                "payload": json.loads(str(row[3])),
            }
            if row is not None
            else None
        )
        return {
            "invocation_id": str(invocation_id or "").strip(),
            "last_sequence": int(latest["sequence"]) if latest else 0,
            "status": str(latest["type"]) if latest else "unknown",
            "terminal": bool(latest and latest["type"] in TERMINAL_EVENT_TYPES),
            "latest": latest,
        }

    def prune(self, *, before: str, terminal_only: bool = True) -> int:
        """Delete retained events older than an ISO timestamp."""

        query = "DELETE FROM invocation_events WHERE occurred_at < ?"
        params: tuple[Any, ...] = (before,)
        if terminal_only:
            query = """
                DELETE FROM invocation_events
                WHERE occurred_at < ?
                  AND EXISTS (
                    SELECT 1
                    FROM invocation_events AS terminal
                    WHERE terminal.owner_key = invocation_events.owner_key
                      AND terminal.invocation_id = invocation_events.invocation_id
                      AND terminal.event_type IN (
                          'completed', 'failed', 'cancelled', 'conflicted', 'expired'
                      )
                  )
            """
        with self._lock, self._connect() as connection:
            cursor = connection.execute(query, params)
            connection.execute(
                """
                DELETE FROM command_invocations
                WHERE NOT EXISTS (
                    SELECT 1 FROM invocation_events
                    WHERE invocation_events.owner_key = command_invocations.owner_key
                      AND invocation_events.invocation_id = command_invocations.invocation_id
                )
                  AND state IN (
                    'succeeded', 'failed', 'cancelled', 'conflicted', 'expired'
                  )
                """
            )
            connection.commit()
            return int(cursor.rowcount)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        _restrict_sqlite_files(self.path)
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            _migrate_owner_scoped_tables(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS invocation_events (
                    owner_key TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (owner_key, invocation_id, sequence)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS command_invocations (
                    owner_key TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    approval_request_id TEXT,
                    lease_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_key, invocation_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS invocation_events_retention
                ON invocation_events (occurred_at, owner_key, invocation_id)
                """
            )
            connection.commit()


def _migrate_owner_scoped_tables(connection: sqlite3.Connection) -> None:
    event_columns = {
        str(row[1]) for row in connection.execute(
            "PRAGMA table_info(invocation_events)"
        ).fetchall()
    }
    invocation_info = connection.execute(
        "PRAGMA table_info(command_invocations)"
    ).fetchall()
    invocation_columns = {str(row[1]) for row in invocation_info}
    invocation_pk = [
        str(item[1])
        for item in sorted(invocation_info, key=lambda row: int(row[5] or 0))
        if int(item[5] or 0) > 0
    ]
    if not event_columns and not invocation_info:
        return
    if "owner_key" in event_columns and invocation_pk == [
        "owner_key",
        "invocation_id",
    ]:
        if "lease_id" not in invocation_columns:
            connection.execute(
                "ALTER TABLE command_invocations ADD COLUMN lease_id TEXT"
            )
            connection.commit()
        return

    connection.execute("BEGIN IMMEDIATE")
    if event_columns:
        connection.execute(
            "ALTER TABLE invocation_events RENAME TO invocation_events_legacy_owner"
        )
    if invocation_info:
        connection.execute(
            "ALTER TABLE command_invocations RENAME TO command_invocations_legacy_owner"
        )
    connection.execute(
        """
        CREATE TABLE command_invocations (
            owner_key TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            result_json TEXT,
            approval_request_id TEXT,
            lease_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_key, invocation_id)
        )
        """
    )
    if invocation_info:
        owner_expression = (
            "owner_key" if "owner_key" in invocation_columns else "'local'"
        )
        fingerprint_expression = (
            "request_fingerprint"
            if "request_fingerprint" in invocation_columns
            else "''"
        )
        result_expression = (
            "result_json" if "result_json" in invocation_columns else "NULL"
        )
        approval_expression = (
            "approval_request_id"
            if "approval_request_id" in invocation_columns
            else "NULL"
        )
        lease_expression = (
            "lease_id" if "lease_id" in invocation_columns else "NULL"
        )
        connection.execute(
            f"""
            INSERT INTO command_invocations
            SELECT {owner_expression}, invocation_id, {fingerprint_expression}, state,
                   {result_expression}, {approval_expression}, {lease_expression},
                   created_at, updated_at
            FROM command_invocations_legacy_owner
            """
        )
    connection.execute(
        """
        CREATE TABLE invocation_events (
            owner_key TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (owner_key, invocation_id, sequence)
        )
        """
    )
    if event_columns:
        if "owner_key" in event_columns:
            connection.execute(
                """
                INSERT INTO invocation_events
                SELECT owner_key, invocation_id, sequence, event_type,
                       occurred_at, payload_json
                FROM invocation_events_legacy_owner
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO invocation_events
                SELECT COALESCE(
                           (
                               SELECT owner_key
                               FROM command_invocations_legacy_owner
                               WHERE command_invocations_legacy_owner.invocation_id =
                                     invocation_events_legacy_owner.invocation_id
                               LIMIT 1
                           ),
                           'local'
                       ),
                       invocation_id, sequence, event_type, occurred_at, payload_json
                FROM invocation_events_legacy_owner
                """
            )
    connection.execute("DROP TABLE IF EXISTS invocation_events_legacy_owner")
    connection.execute("DROP TABLE IF EXISTS command_invocations_legacy_owner")
    connection.commit()


def _owner_key(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512:
        raise InvocationEventError("owner_key must be 1-512 characters")
    return normalized


def _restrict_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            lowered = normalized_key.lower()
            if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
                result[normalized_key] = "[REDACTED]"
            else:
                result[normalized_key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_VALUE_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    return value
