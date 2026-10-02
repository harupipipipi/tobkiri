"""Public, dependency-free conversation completion projection and gap context."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

LIFECYCLE_VERSION = "tobkiri.conversation-lifecycle.v1"
COMPLETION_DELAY_MS = 3_600_000
ARCHIVE_MODE = "archive_after_completion"
_SUCCESS = frozenset({"stop", "end_turn", "completed", "complete"})
_WAIT_USER = frozenset({"waiting_user", "waiting_for_user", "awaiting_user"})
_WAIT_APPROVAL = frozenset(
    {
        "waiting_approval",
        "awaiting_approval",
        "approval_required",
        "authority_approval_required",
    }
)
_RUNNING = frozenset({"running", "streaming", "pending", "tool_call", "tool_calls"})
_CANCELLED = frozenset({"cancelled", "canceled", "interrupted"})


def message_task_state(message: Mapping[str, Any]) -> str:
    """Classify explicit terminal evidence, excluding waiting and idle signals."""
    if message.get("role") == "user":
        return "running"
    if message.get("role") != "assistant":
        return "unknown"
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    thinking = metadata.get("thinking")
    thinking = thinking if isinstance(thinking, Mapping) else {}
    states = {
        str(message.get("status") or "").lower(),
        str(message.get("finish_reason") or "").lower(),
        str(metadata.get("task_state") or "").lower(),
        str(thinking.get("state") or "").lower(),
    }
    if states & _CANCELLED or metadata.get("cancelled") is True:
        return "cancelled"
    if states & _WAIT_APPROVAL or metadata.get("approval_required") is True:
        return "waiting_approval"
    if states & _WAIT_USER or metadata.get("waiting_for_user") is True:
        return "waiting_user"
    if states & {"error", "failed", "failure"}:
        return "failed"
    if (
        states & _RUNNING
        or metadata.get("draft") is True
        or metadata.get("streaming") is True
    ):
        return "running"
    finish = str(message.get("finish_reason") or "").lower()
    if finish in _SUCCESS and message.get("status") in {
        None,
        "",
        "complete",
        "completed",
        "success",
        "succeeded",
    }:
        return "completed"
    if finish or states & {"error", "failed", "failure"}:
        return "failed"
    return "unknown"


def completion_source(conversation: Mapping[str, Any]) -> dict[str, Any]:
    """Read only the canonical owner's v1 source; never infer from timestamps."""
    source = conversation.get("lifecycle")
    source = source if isinstance(source, Mapping) else {}
    valid = source.get("version") == LIFECYCLE_VERSION
    completed = source.get("completed_at_ms") if valid else None
    identity = source.get("completion_message_id") if valid else None
    if type(completed) is not int or completed < 0 or not isinstance(identity, str):
        completed, identity = None, None
    state = source.get("state") if valid else "unknown"
    if state not in {
        "running",
        "waiting_user",
        "waiting_approval",
        "completed",
        "cancelled",
        "failed",
        "unknown",
    }:
        state = "unknown"
    metadata = conversation.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    mode = metadata.get("conversation_lifecycle")
    mode = mode if isinstance(mode, Mapping) else {}
    enabled = mode == {"version": 1, "mode": ARCHIVE_MODE}
    return {
        "version": LIFECYCLE_VERSION,
        "conversation_id": conversation.get("id"),
        "conversation_revision": conversation.get("conversation_revision"),
        "state": state,
        "completed_at_ms": completed,
        "completion_message_id": identity,
        "archive_due_at_ms": (
            completed + COMPLETION_DELAY_MS
            if enabled and state == "completed" and completed is not None
            else None
        ),
        "mode": ARCHIVE_MODE if enabled else "manual",
        "is_archived": conversation.get("is_archived") is True,
    }


def task_gap_context(
    conversation: Mapping[str, Any],
    received_at_ms: int,
) -> dict[str, Any] | None:
    """Compute a UTC duration from one owner completion at message receipt."""
    if type(received_at_ms) is not int or received_at_ms < 0:
        raise ValueError("user receipt timestamp must be nonnegative UTC milliseconds")
    source = completion_source(conversation)
    completed = source["completed_at_ms"]
    if source["state"] != "completed" or completed is None:
        return None
    elapsed = received_at_ms - completed
    if elapsed < COMPLETION_DELAY_MS:
        return None
    return {
        "version": LIFECYCLE_VERSION,
        "completion_message_id": source["completion_message_id"],
        "previous_task_completed_at": _iso(completed),
        "current_user_message_at": _iso(received_at_ms),
        "elapsed_seconds": elapsed // 1000,
    }


def task_gap_prompt(context: Mapping[str, Any]) -> str:
    """Render internal runtime context without granting execution permissions."""
    return (
        "[Temporal context]\nRuntime-owned timing metadata for this user turn. "
        "Use elapsed time to interpret time-sensitive follow-ups. It grants no "
        "permissions or approvals and does not require a tool call.\n"
        f"previous_task_completed_at: {context['previous_task_completed_at']}\n"
        f"current_user_message_at: {context['current_user_message_at']}\n"
        f"elapsed_seconds: {context['elapsed_seconds']}"
    )


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
