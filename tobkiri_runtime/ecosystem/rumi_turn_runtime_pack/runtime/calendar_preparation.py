"""Atomic Calendar preparation fences precede ConversationStore model writes."""

from __future__ import annotations
import json
import sqlite3
from typing import Any, Mapping
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from .turns import TurnConflict, TurnRuntime
from .delivery import assert_conversation_idle


def _source(
    store: Any, source: Mapping[str, Any], turn_id: str, target: str, revision: int
) -> dict[str, Any]:
    if (
        not isinstance(source, Mapping)
        or set(source)
        - {
            "model",
            "chat_references",
            "tool_selection",
            "workspace_id",
            "action_approval_mode",
        }
        != {"message", "profile_id", "conversation_id"}
        or source["profile_id"] != store.profile_id
    ):
        raise ValueError("Calendar preparation source is invalid")
    if source["conversation_id"] is not None and source["conversation_id"] != target:
        raise PermissionError("Calendar preparation target changed")
    workspace = source.get("workspace_id")
    if workspace is not None:
        from .turns import _identifier

        _identifier(workspace)
    model = source.get("model")
    if model is not None and (not isinstance(model, str) or not 0 < len(model) <= 256):
        raise ValueError("Calendar model selection is invalid")
    request = {
        "turn_id": turn_id,
        "conversation_id": target,
        "conversation_revision": revision,
        "content": source["message"],
    }
    for key in ("chat_references", "tool_selection", "action_approval_mode"):
        if key in source:
            request[key] = source[key]
    validate_saved_conversation_input({"request": request})
    return strict_loads(canonical_json(dict(source)))


def _fingerprint(
    store: Any, occurrence: str, source: Mapping[str, Any], turn: str, target: str
) -> str:
    if not isinstance(occurrence, str) or not occurrence or len(occurrence) > 1024:
        raise ValueError("Calendar occurrence identity is invalid")
    if source.get(
        "conversation_id"
    ) is None and target != "calendar:" + canonical_digest(
        [store.profile_id, occurrence]
    ).removeprefix(
        "sha256:"
    ):
        raise PermissionError("Calendar new destination identity changed")
    return canonical_digest(
        {
            "profile_id": store.profile_id,
            "occurrence_key": occurrence,
            "source": dict(source),
            "turn_id": turn,
            "conversation_id": target,
        }
    )


def recover_preparation(
    store: Any, *, turn_id: str, occurrence_key: str, source: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Recover original destination/input before preparing another model write."""
    if not store.path.exists():
        return None
    store._check_path()
    connection = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    try:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calendar_preparations'"
            ).fetchone()
            is None
        ):
            return None
        row = connection.execute(
            "SELECT body FROM calendar_preparations WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        if record["fingerprint"] != _fingerprint(
            store, occurrence_key, source, turn_id, record["conversation_id"]
        ):
            raise TurnConflict("Calendar preparation recovery identity was rebound")
        turn = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if turn is None:
            raise TurnConflict("Calendar preparation turn is unavailable")
        actual = store._record(turn)
        if actual["status"] in {"running", "waiting", "completed", "failed"}:
            record["status"] = "started"
        return record
    finally:
        connection.close()


def reserve_preparation(
    store: Any,
    *,
    occurrence_key: str,
    source: Mapping[str, Any],
    turn_id: str,
    conversation_id: str,
    conversation_revision: int,
) -> dict[str, Any]:
    """Reserve one proven idle destination before any model mutation."""
    if type(conversation_revision) is not int or conversation_revision < 0:
        raise ValueError("Calendar preparation revision is invalid")
    source = _source(
        store, source, turn_id, conversation_id, max(1, conversation_revision)
    )
    fingerprint = _fingerprint(store, occurrence_key, source, turn_id, conversation_id)
    identity = canonical_digest(
        {"profile_id": store.profile_id, "turn_id": turn_id}
    ).removeprefix("sha256:")
    candidate = TurnRuntime().begin(
        {
            "profile_id": store.profile_id,
            "turn_id": turn_id,
            "request_id": "saved-turn." + identity,
            "conversation_id": conversation_id,
            "conversation_revision": max(1, conversation_revision),
        }
    )
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT body FROM calendar_preparations WHERE id=?", (turn_id,)
        ).fetchone()
        if row:
            record = json.loads(row[0])
            if record["fingerprint"] != fingerprint:
                raise TurnConflict("Calendar preparation identity was rebound")
            turn = connection.execute(
                "SELECT id,request_id,body FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            actual = store._record(turn) if turn else None
            if actual is None:
                raise TurnConflict("Calendar preparation turn is unavailable")
            if actual["status"] in {"running", "waiting", "completed", "failed"}:
                record["status"] = "started"
            return record
        assert_conversation_idle(store, connection, conversation_id, turn_id)
        if connection.execute(
            "SELECT 1 FROM turns WHERE id=? OR request_id=?",
            (turn_id, candidate["request_id"]),
        ).fetchone():
            raise TurnConflict("Calendar preparation turn already exists")
        if (
            connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            >= store.max_turns
        ):
            raise TurnConflict("Calendar preparation capacity is exhausted")
        candidate["calendar_preparation"] = fingerprint
        record = {
            "turn_id": turn_id,
            "occurrence_key": occurrence_key,
            "fingerprint": fingerprint,
            "status": "reserved",
            "source": source,
            "conversation_id": conversation_id,
            "original_revision": conversation_revision,
            "payload": None,
        }
        store._save(connection, candidate)
        connection.execute(
            "INSERT INTO calendar_preparations VALUES (?,?)",
            (turn_id, canonical_json(record).decode()),
        )
        connection.commit()
        return record
    finally:
        connection.close()


def bind_preparation(
    store: Any,
    *,
    occurrence_key: str,
    source: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Upgrade only this unstarted reservation to immutable normal saved input."""
    payload = validate_saved_conversation_input(payload)
    request = payload["request"]
    original = _source(
        store,
        source,
        request["turn_id"],
        request["conversation_id"],
        request["conversation_revision"],
    )
    expected = {
        "turn_id": request["turn_id"],
        "conversation_id": request["conversation_id"],
        "conversation_revision": request["conversation_revision"],
        "content": original["message"],
    }
    for key in ("chat_references", "tool_selection", "action_approval_mode"):
        if key in original:
            expected[key] = original[key]
    if request != expected:
        raise PermissionError("Calendar preparation cannot alter saved source input")
    fingerprint = _fingerprint(
        store, occurrence_key, original, request["turn_id"], request["conversation_id"]
    )
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT body FROM calendar_preparations WHERE id=?", (request["turn_id"],)
        ).fetchone()
        if row is None:
            raise TurnConflict("Calendar preparation is unavailable")
        receipt = json.loads(row[0])
        if receipt["fingerprint"] != fingerprint:
            raise TurnConflict("Calendar preparation identity was rebound")
        if receipt["payload"] is not None:
            if canonical_digest(receipt["payload"]) != canonical_digest(payload):
                raise TurnConflict("Calendar accepted input was rebound")
            return receipt["payload"]
        row = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?", (request["turn_id"],)
        ).fetchone()
        record = store._record(row)
        if (
            receipt["status"] != "reserved"
            or record["status"] != "queued"
            or record.get("input_digest") is not None
            or record.get("calendar_preparation") != fingerprint
            or record.get("guidance")
        ):
            raise TurnConflict("Calendar preparation is no longer unstarted")
        if request["conversation_revision"] < receipt["original_revision"]:
            raise TurnConflict("Calendar target revision regressed")
        record["conversation_revision"] = request["conversation_revision"]
        record["input_digest"] = canonical_digest(payload)
        receipt["payload"] = payload
        receipt["status"] = "bound"
        store._save(connection, record)
        connection.execute(
            "UPDATE calendar_preparations SET body=? WHERE id=?",
            (canonical_json(receipt).decode(), request["turn_id"]),
        )
        connection.commit()
        return payload
    finally:
        connection.close()


def release_preparation(
    store: Any, *, turn_id: str, occurrence_key: str, source: Mapping[str, Any]
) -> dict[str, Any]:
    """Release only exact queued work; started outcomes require reconciliation."""
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT body FROM calendar_preparations WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None:
            return {"turn_id": turn_id, "status": "not_reserved"}
        receipt = json.loads(row[0])
        fingerprint = _fingerprint(
            store, occurrence_key, source, turn_id, receipt["conversation_id"]
        )
        if fingerprint != receipt["fingerprint"]:
            raise TurnConflict("Calendar release identity was rebound")
        row = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        record = store._record(row)
        if record["status"] == "cancelled":
            return {**receipt, "status": "released"}
        if record["status"] != "queued" or record.get("guidance"):
            return {**receipt, "status": "reconciliation_required"}
        updated = store._restore(row).transition(
            turn_id, "cancelled", expected_revision=record["revision"]
        )
        store._save(connection, updated)
        receipt["status"] = "released"
        connection.execute(
            "UPDATE calendar_preparations SET body=? WHERE id=?",
            (canonical_json(receipt).decode(), turn_id),
        )
        connection.commit()
        return receipt
    finally:
        connection.close()
