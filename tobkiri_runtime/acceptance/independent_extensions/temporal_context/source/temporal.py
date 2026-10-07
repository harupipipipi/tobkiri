"""Pure Issue #1409 reducer; host must authenticate events and persist snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def timestamp(value: Any) -> datetime:
    """Require an offset-aware ISO timestamp and normalize duration arithmetic."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include UTC offset")
    return parsed.astimezone(timezone.utc)


def identity(value: Any) -> str:
    """Validate an opaque namespace/event identifier, never a filesystem path."""
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("invalid identifier")
    return value


def transition(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce authenticated events without IO, changing only internal metadata.

    Snapshot durability and event authenticity are the composer's responsibilities.
    Sequence is a host-issued monotonic sequence in a profile/conversation namespace.
    All events consume sequence, preventing stale/duplicate event reuse.
    """
    if set(payload) != {"namespace", "state", "event"}:
        raise ValueError("unexpected input fields")
    namespace = identity(payload["namespace"])
    state, event = payload["state"], payload["event"]
    if not isinstance(state, dict) or not isinstance(event, dict):
        raise ValueError("state and event must be objects")
    if set(event) != {"id", "sequence", "kind", "at"}:
        raise ValueError("unexpected event fields")
    identity(event["id"])
    sequence = event["sequence"]
    if type(sequence) is not int or sequence < 1:
        raise ValueError("invalid sequence")
    kind = event["kind"]
    if kind not in {"assistant.completed", "assistant.error", "assistant.cancelled",
                    "tool.completed", "user.received"}:
        raise ValueError("unknown event kind")
    now = timestamp(event["at"])
    if state:
        if set(state) != {"namespace", "sequence", "event_id", "last_event_at",
                         "last_task_completed_at"}:
            raise ValueError("invalid snapshot fields")
        if state["namespace"] != namespace:
            raise ValueError("cross-namespace snapshot")
        identity(state["event_id"])
        if type(state["sequence"]) is not int or state["sequence"] < 1:
            raise ValueError("invalid snapshot sequence")
        if sequence <= state["sequence"] or event["id"] == state["event_id"]:
            raise ValueError("stale or duplicate event")
        previous_event = timestamp(state["last_event_at"])
        if now < previous_event:
            raise ValueError("event timestamp moved backwards")
        completed = state["last_task_completed_at"]
        if completed is not None and timestamp(completed) > previous_event:
            raise ValueError("snapshot completion is in future")
    else:
        completed = None
    internal = None
    if kind == "assistant.completed":
        completed = event["at"]
    elif kind == "user.received" and completed is not None:
        seconds = (now - timestamp(completed)).total_seconds()
        if seconds >= 3600:
            internal = {"previous_task_completed_at": completed,
                        "current_user_message_at": event["at"],
                        "elapsed_seconds": seconds}
    return {"state": {"namespace": namespace, "sequence": sequence,
                      "event_id": event["id"], "last_event_at": event["at"],
                      "last_task_completed_at": completed},
            "internal_temporal_context": internal}


def run(context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Return the pure reducer result using official scaffold authoring shape."""
    del context
    return {"status": "ok", "data": transition(args), "error": None}
