"""Explicitly rooted SQLite persistence for the existing turn lifecycle owner.

The captured Host provider uses this store. It never starts or restarts AI
execution. Restored nonterminal records require explicit reconciliation by the
future execution coordinator.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from core_runtime.profile_workspace import validate_profile_id
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict, TurnRuntime
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

_MAX_RECORD_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACTIONS = {
    "transition": {"status", "details"},
    "steer": {"guidance"},
    "handoff": {"target"},
    "consume_guidance": {"guidance_ids"},
    "cancel_guidance": {"guidance_id"},
}


class DurableTurnRuntime:
    """Retain turn/request identities with bounded reads and atomic updates."""

    def __init__(self, profile_id: str, *, user_data_root: Path, max_turns: int = 10000) -> None:
        """Capture an explicit owner root without opening or creating a database."""
        self.profile_id = validate_profile_id(profile_id)
        if type(max_turns) is not int or max_turns < 1:
            raise ValueError("turn capacity must be an exact positive integer")
        self.max_turns = max_turns
        if not Path(user_data_root).is_absolute():
            raise ValueError("turn owner root must be absolute")
        self.path = (
            Path(user_data_root)
            / "packs"
            / "rumi_turn_runtime_pack"
            / "profiles"
            / self.profile_id
            / "turns.sqlite3"
        )

    def begin_saved(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Bind validated saved input without starting or retrying execution."""
        initial = validate_saved_conversation_input(payload)
        request = initial["request"]
        identity = canonical_digest({
            "profile_id": self.profile_id, "turn_id": request["turn_id"],
        }).removeprefix("sha256:")
        return self.begin({
            "turn_id": request["turn_id"],
            "request_id": "saved-turn." + identity,
            "conversation_id": request["conversation_id"],
            "conversation_revision": request["conversation_revision"],
            "input_digest": canonical_digest(initial),
        })

    def claim_saved(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Acquire a saved turn once, without invoking or authorizing execution.

        Only a queued record can be claimed. The revision comparison and
        transition commit under the same SQLite lock in ``mutate``. A lost
        claim reply is deliberately not recoverable as another claim: callers
        must reconcile the existing turn instead of replaying side effects.
        Guidance racing with the claim also returns a non-claim snapshot; this
        method never spins or retries an ambiguous mutation.
        """
        record = self.begin_saved(payload)
        if record["status"] != "queued":
            return {"claimed": False, "turn": record}
        try:
            request = validate_saved_conversation_input(payload)["request"]
            message_ids = {
                role: "message:"
                + canonical_digest(
                    [request["conversation_id"], request["turn_id"], role]
                ).removeprefix("sha256:")
                for role in ("user", "assistant")
            }
            record = self.mutate(
                "transition",
                record["id"],
                expected_revision=record["revision"],
                status="running",
                details={
                    "phase": "saved_execution_claimed",
                    "user_message_id": message_ids["user"],
                    "assistant_message_id": message_ids["assistant"],
                },
            )
        except TurnConflict:
            # Revalidate the complete input binding on the readback too.
            return {"claimed": False, "turn": self.begin_saved(payload)}
        return {"claimed": True, "turn": record}

    def begin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Begin or recover the same persisted request without re-executing it."""
        allowed = {
            "profile_id",
            "turn_id",
            "request_id",
            "conversation_id",
            "conversation_revision",
            "input_digest",
        }
        if set(payload) - allowed or (
            "profile_id" in payload and payload["profile_id"] != self.profile_id
        ):
            raise PermissionError("turn begin does not match captured Profile")
        for field in ("turn_id", "request_id", "conversation_id"):
            if not isinstance(payload.get(field), str) or not _ID.fullmatch(payload[field]):
                raise ValueError("durable begin requires explicit stable identities")
        if "input_digest" in payload:
            _input_digest(payload["input_digest"])
        bound = {**payload, "profile_id": self.profile_id}
        # Validate before creating a new database, including the exact revision.
        candidate = TurnRuntime().begin(bound)
        if "input_digest" in payload:
            candidate["input_digest"] = payload["input_digest"]
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE request_id = ?",
                (candidate["request_id"],),
            ).fetchone()
            if row is not None:
                if self._record(row).get("input_digest") != candidate.get("input_digest"):
                    raise TurnConflict("turn input identity was rebound")
                runtime = self._restore(row)
                return runtime.begin(bound)
            if (
                connection.execute(
                    "SELECT 1 FROM turns WHERE id = ?", (candidate["id"],)
                ).fetchone()
                is not None
            ):
                raise TurnConflict("turn already exists")
            if connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] >= self.max_turns:
                raise TurnConflict("durable turn capacity is exhausted")
            self._save(connection, candidate)
            connection.commit()
            return candidate
        finally:
            connection.close()

    def get(self, turn_id: str) -> dict[str, Any] | None:
        """Read one persisted record; absent stores are not created by reads."""
        if not self.path.exists():
            return None
        self._check_path()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            connection.close()

    def list(self, *, limit: int = 100, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Read a bounded newest-first page without loading the entire history."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("turn list limit is invalid")
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id)
        ):
            raise ValueError("turn conversation filter is invalid")
        if not self.path.exists():
            return []
        self._check_path()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE (? IS NULL OR json_extract(body, '$.conversation_id') = ?) ORDER BY updated_at DESC, id LIMIT ?",
                (conversation_id, conversation_id, limit),
            ).fetchall()
            return [self._record(row) for row in rows]
        finally:
            connection.close()

    def mutate(
        self, action: str, turn_id: str, *, expected_revision: int,
        reject_saved_transition: bool = False, **arguments: Any
    ) -> dict[str, Any]:
        """Apply only existing lifecycle methods under one SQLite write lock."""
        if type(reject_saved_transition) is not bool:
            raise ValueError("turn mutation boundary is invalid")
        if action not in _ACTIONS or set(arguments) - _ACTIONS[action]:
            raise ValueError("durable turn mutation fields are invalid")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("turn revision must be an exact positive integer")
        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError("turn is unknown")
            if reject_saved_transition and action == "transition":
                existing = self._record(row)
                if existing.get("input_digest") and existing["request_id"].startswith("saved-turn."):
                    # The captured management adapter fixes this flag, never
                    # caller payload. Check under the write lock, so creation
                    # or pruning cannot race a separate preflight read.
                    raise PermissionError("saved turn status is coordinator-owned")
            runtime = self._restore(row)
            result = getattr(runtime, action)(
                turn_id, expected_revision=expected_revision, **arguments
            )
            record = runtime.get(turn_id)
            if record is None:
                raise ValueError("turn mutation lost its record")
            self._save(connection, record)
            connection.commit()
            return result
        finally:
            connection.close()

    def reconcile_saved(
        self, turn_id: str, *, expected_revision: int, input_digest: str,
        result_reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Commit coordinator-verified owner evidence without starting execution.

        This method is intentionally absent from the public lifecycle adapter.
        The coordinator must obtain the reference through its captured reader.
        """
        _input_digest(input_digest)
        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,),
            ).fetchone()
            if row is None:
                raise KeyError("turn is unknown")
            record = self._record(row)
            if (
                record.get("input_digest") != input_digest
                or not record["request_id"].startswith("saved-turn.")
                or record["status"] not in {"running", "waiting"}
            ):
                raise TurnConflict("saved receipt does not match the durable turn")
            runtime = self._restore(row)
            result = runtime.transition(
                turn_id, "completed", expected_revision=expected_revision,
                details={"result_reference": dict(result_reference), "reconciled": True},
                reconciled_saved=True,
            )
            self._save(connection, result)
            connection.commit()
            return result
        finally:
            connection.close()

    def _connect_write(self) -> sqlite3.Connection:
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            self.path.chmod(0o600)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, updated_at INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            return connection
        except Exception:
            connection.close()
            raise

    def _check_path(self) -> None:
        # Defense against accidentally redirected owner files. The captured
        # application-data tree must remain protected from hostile local writes;
        # this check is not a substitute for filesystem permissions.
        if any(path.is_symlink() for path in (self.path, *self.path.parents)):
            raise PermissionError("turn owner path must not contain symlinks")

    def _record(self, row: tuple[str, str, str]) -> dict[str, Any]:
        turn_id, request_id, body = row
        if len(body.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ValueError("turn record exceeds size limit")
        record = json.loads(body)
        if not isinstance(record, dict) or record.get("profile_id") != self.profile_id:
            raise ValueError("persisted turn Profile is invalid")
        if record.get("id") != turn_id or record.get("request_id") != request_id:
            raise ValueError("persisted turn identity does not match its index")
        if "input_digest" in record:
            _input_digest(record["input_digest"])
        for field in ("id", "request_id", "conversation_id"):
            value = record.get(field)
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("persisted turn identity is invalid")
        for field in ("revision", "conversation_revision", "created_at", "updated_at"):
            value = record.get(field)
            if type(value) is not int or value < 1:
                raise ValueError("persisted turn revision or timestamp is invalid")
        if record.get("status") not in {
            "queued",
            "running",
            "waiting",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("persisted turn status is invalid")
        for field in ("guidance", "events"):
            values = record.get(field)
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise ValueError("persisted turn sequence is invalid")
        return record

    def _restore(self, row: tuple[str, str, str]) -> TurnRuntime:
        record = self._record(row)
        runtime = TurnRuntime()
        # Same-owner restoration of one record; legacy global runtime pools and
        # terminal pruning are deliberately not the durable identity index.
        runtime._turns[record["id"]] = record
        runtime._request_ids[record["request_id"]] = record["id"]
        return runtime

    def _save(self, connection: sqlite3.Connection, record: Mapping[str, Any]) -> None:
        body = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ValueError("turn record exceeds size limit")
        connection.execute(
            "INSERT INTO turns(id, request_id, updated_at, body) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, body=excluded.body",
            (record["id"], record["request_id"], record["updated_at"], body),
        )


def _input_digest(value: object) -> None:
    """Validate an input identity, never an execution or approval credential."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("turn input digest is invalid")
