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


def tobkiri_packvm_invoke(operation_id: str, payload: dict) -> dict:
    """Invoke the exact pure reducer using the documented PackVM Python ABI."""
    if operation_id == "temporal.reduce":
        return transition(payload)
    if operation_id == "timing.project.owner":
        return project_owner_timing(payload)
    raise ValueError("unknown operation")


def project_owner_timing(payload: dict[str, Any]) -> dict[str, Any]:
    """Project supplied owner shape; captured execution establishes its provenance.

    This stateless implementation mirrors the public active_task_gap_context
    projection using only stdlib. It does not authenticate a caller's snapshot,
    confirm a turn, persist a baseline, or grant execution authority.
    """
    if not isinstance(payload, dict) or set(payload) != {"profile_id", "owner_snapshot"}:
        raise ValueError("unexpected timing input fields")
    profile = payload["profile_id"]
    owner = payload["owner_snapshot"]
    if not isinstance(profile, str) or not profile or len(profile) > 256:
        raise ValueError("invalid profile identity")
    if not isinstance(owner, dict):
        raise ValueError("owner snapshot must be an object")
    conversation = owner.get("id")
    revision = owner.get("conversation_revision")
    lifecycle = owner.get("lifecycle")
    if (not isinstance(conversation, str) or not conversation
            or type(revision) is not int or revision < 1
            or not isinstance(lifecycle, dict)):
        raise ValueError("invalid owner identity or revision")
    active = lifecycle.get("active_user_message_id")
    if not isinstance(active, str) or not active:
        raise ValueError("missing active owner receipt")
    context = None
    completed = lifecycle.get("resumed_completed_at_ms")
    received = lifecycle.get("active_user_received_at_ms")
    messages = owner.get("messages")
    if (lifecycle.get("version") == "tobkiri.conversation-lifecycle.v1"
            and lifecycle.get("state") == "running"
            and type(completed) is int and type(received) is int
            and completed >= 0 and received - completed >= 3_600_000
            and isinstance(messages, list)
            and any(isinstance(message, dict) and message.get("id") == active
                    and message.get("role") == "user" for message in messages)):
        context = {
            "version": "tobkiri.conversation-lifecycle.v1",
            "completion_message_id": lifecycle.get("completion_message_id"),
            "previous_task_completed_at": datetime.fromtimestamp(
                completed / 1000, timezone.utc).isoformat(),
            "current_user_message_at": datetime.fromtimestamp(
                received / 1000, timezone.utc).isoformat(),
            "elapsed_seconds": (received - completed) // 1000,
        }
    return {"internal_context_api_version": "io.tobkiri.saved-internal-context.v1",
            "profile_id": profile, "conversation_id": conversation,
            "conversation_revision": revision, "active_user_message_id": active,
            "context": context}
