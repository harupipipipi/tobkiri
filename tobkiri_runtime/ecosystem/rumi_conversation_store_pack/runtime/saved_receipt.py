"""Content-free saved append evidence committed by the conversation owner."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.saved_conversation import (
    is_saved_text_content, validate_saved_conversation_input,
)
from tobkiri_protocol.saved_tools import saved_tool_messages, saved_tool_logs


def append_receipt(
    previous: Mapping[str, Any] | None,
    initial: Mapping[str, Any],
    conversation_id: str,
    expected_revision: int,
    message: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one owner-normalized append, without storing another transcript.

    The captured Host adapter must authorize saved callers before this helper.
    Its return value must be written in the same commit as the message.
    """
    initial = validate_saved_conversation_input(initial)
    request = initial["request"]
    user_id, assistant_id = (
        "message:" + canonical_digest([
            conversation_id, request["turn_id"], role,
        ]).removeprefix("sha256:")
        for role in ("user", "assistant")
    )
    identity = {
        "turn_id": request["turn_id"],
        "conversation_id": conversation_id,
        "input_digest": canonical_digest(initial),
        "initial_revision": request["conversation_revision"],
        "user_message_id": user_id,
        "assistant_message_id": assistant_id,
    }
    role = message.get("role")
    metadata = {"turn_id": request["turn_id"]}
    trace = saved_tool_messages((message.get("metadata") or {}).get("saved_tool_messages", []))
    if trace:
        if role != "assistant" or request.get("tool_selection", {}).get("mode", "none") == "none":
            raise ValueError("saved append tool transcript is out of scope")
        metadata["saved_tool_messages"] = trace
    if canonical_json(message.get("tool_logs") or []) != canonical_json(saved_tool_logs(trace)):
        raise ValueError("saved append tool logs differ from transcript")
    if (
        conversation_id != request["conversation_id"]
        or role not in {"user", "assistant"}
        or message.get("id") != (user_id if role == "user" else assistant_id)
        or message.get("status") != "complete"
        or canonical_json(message.get("metadata")) != canonical_json(metadata)
    ):
        raise ValueError("saved append identity is invalid")
    if role == "user":
        if (
            previous is not None
            or expected_revision != request["conversation_revision"]
            or message.get("content") != request["content"]
        ):
            raise ValueError("saved user append cannot be replayed or rebound")
        return {**identity, "user_revision": expected_revision + 1}
    if (
        previous is None
        or set(previous) != set(identity) | {"user_revision"}
        or any(previous[key] != value for key, value in identity.items())
        or type(previous["user_revision"]) is not int
        or previous["user_revision"] != expected_revision
        or message.get("parent_id") != user_id
        or not is_saved_text_content(message.get("content"))
    ):
        raise ValueError("saved assistant append has no matching user receipt")
    revision = expected_revision + 1
    outcome = {
        "status": "ok",
        "turn_id": request["turn_id"],
        "conversation_id": conversation_id,
        "conversation_revision": revision,
        "user_message_id": user_id,
        "message": dict(message),
    }
    return {
        **dict(previous),
        "result_reference": {
            "conversation_id": conversation_id,
            "conversation_revision": revision,
            "user_message_id": user_id,
            "assistant_message_id": assistant_id,
            "outcome_digest": canonical_digest(outcome),
        },
    }
