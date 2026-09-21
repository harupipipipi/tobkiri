from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_RECORDS = 10_000
_INIT_LOCK_GUARD = threading.Lock()
_INIT_LOCKS: dict[str, threading.Lock] = {}


def _init_lock(path: Path) -> threading.Lock:
    key = str(path.expanduser().resolve())
    with _INIT_LOCK_GUARD:
        return _INIT_LOCKS.setdefault(key, threading.Lock())


class IdempotencyConflictError(ValueError):
    """Raised when an operation key is reused for a different request payload."""


@dataclass(frozen=True)
class IdempotencyClaim:
    state: str
    status: str
    events: list[dict[str, Any]]


def operation_key(input_data: dict[str, Any]) -> str:
    """Validate and return the optional client operation key."""
    value = str(input_data.get("idempotency_key") or "").strip()
    if value and not _KEY_RE.fullmatch(value):
        raise ValueError(
            "idempotency_key must be 8-128 characters using letters, "
            "numbers, . _ : or -"
        )
    return value


def payload_hash(input_data: dict[str, Any]) -> str:
    """Hash the semantic request body without its delivery identifier."""
    payload = dict(input_data)
    payload.pop("idempotency_key", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def operation_scope(
    input_data: dict[str, Any], context: dict[str, Any] | None
) -> str:
    """Build an auth, conversation, workspace, and session isolated scope."""
    context = context if isinstance(context, dict) else {}
    principal = context.get("_authenticated_principal")
    principal_id = ""
    if isinstance(principal, dict):
        principal_id = str(principal.get("principal_id") or "").strip()
    principal_id = principal_id or str(
        context.get("principal_id") or context.get("authority_principal_id") or "local"
    ).strip()
    metadata = input_data.get("metadata")
    if not isinstance(metadata, dict):
        message = input_data.get("message")
        metadata = message.get("metadata") if isinstance(message, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    params = (
        input_data.get("params")
        if isinstance(input_data.get("params"), dict)
        else {}
    )
    workspace_id = str(
        input_data.get("workspace_id")
        or params.get("workspace_id")
        or metadata.get("workspace_id")
        or context.get("workspace_id")
        or ""
    ).strip()
    session_id = str(
        input_data.get("session_id")
        or params.get("session_id")
        or metadata.get("session_id")
        or context.get("session_id")
        or ""
    ).strip()
    raw = "\x1f".join(
        (
            principal_id,
            str(input_data.get("conversation_id") or "").strip(),
            workspace_id,
            session_id,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ChatIdempotencyStore:
    """Persist chat operation claims and replayable event sequences in SQLite."""

    def __init__(self, path: Path | None = None) -> None:
        override = os.getenv("RUMI_CHAT_IDEMPOTENCY_DB")
        self.path = path or (Path(override).expanduser() if override else (
            Path(__file__).resolve().parents[2]
            / "user_data"
            / "shared"
            / "chat_idempotency.sqlite3"
        ))

    def claim(self, scope: str, key: str, digest: str) -> IdempotencyClaim:
        """Atomically claim a key or return its current replay state."""
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, now)
            row = connection.execute(
                "SELECT payload_hash, status, events_json FROM operations "
                "WHERE scope = ? AND operation_key = ?",
                (scope, key),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise IdempotencyConflictError(
                        "idempotency_key was already used for a different chat payload"
                    )
                events = json.loads(row[2]) if row[2] else []
                return IdempotencyClaim("replay", str(row[1]), events)
            connection.execute(
                "INSERT INTO operations "
                "(scope, operation_key, payload_hash, status, events_json, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, 'in_progress', '[]', ?, ?)",
                (scope, key, digest, now, now),
            )
            return IdempotencyClaim("claimed", "in_progress", [])

    def finish(
        self,
        scope: str,
        key: str,
        digest: str,
        status: str,
        events: list[dict[str, Any]],
    ) -> None:
        """Persist the terminal status and exact replay event sequence."""
        encoded = json.dumps(
            events, ensure_ascii=False, separators=(",", ":"), default=str
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET status = ?, events_json = ?, updated_at = ? "
                "WHERE scope = ? AND operation_key = ? AND payload_hash = ?",
                (status, encoded, time.time(), scope, key, digest),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            with _init_lock(self.path):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS operations ("
                    "scope TEXT NOT NULL, operation_key TEXT NOT NULL, "
                    "payload_hash TEXT NOT NULL, status TEXT NOT NULL, "
                    "events_json TEXT NOT NULL, created_at REAL NOT NULL, "
                    "updated_at REAL NOT NULL, PRIMARY KEY (scope, operation_key))"
                )
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _prune(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM operations WHERE updated_at < ?",
            (now - _RETENTION_SECONDS,),
        )
        connection.execute(
            "DELETE FROM operations WHERE rowid IN ("
            "SELECT rowid FROM operations ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
            (_MAX_RECORDS,),
        )


def reserve_chat_operation(
    input_data: dict[str, Any], context: dict[str, Any] | None
) -> dict[str, Any]:
    """Claim a keyed request before an HTTP streaming response is committed."""
    updated = dict(context or {}) if isinstance(context, dict) else {}
    key = operation_key(input_data)
    if not key:
        return updated
    scope = operation_scope(input_data, updated)
    digest = payload_hash(input_data)
    claim = ChatIdempotencyStore().claim(scope, key, digest)
    updated["_chat_idempotency_reservation"] = {
        "key": key,
        "scope": scope,
        "digest": digest,
        "claim": claim,
    }
    return updated
