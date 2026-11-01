"""Pure scheduled-task data validation; execution remains with captured owners.

The v1 module names this shared Python surface without changing the scheduling
wire schema. These projections validate preferences, never execution authority.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from tobkiri_protocol.saved_conversation import (
    saved_guidance_context, validate_saved_conversation_input,
    is_saved_user_content,
)


def normalize_scheduled_intent(
    task: Mapping[str, Any], *, profile_id: str
) -> dict[str, Any]:
    """Validate a finite creation intent before any owner reads or writes."""
    optional = {"conversation_id", "model", "chat_references", "tool_selection",
                "action_approval_mode", "workspace_id"}
    if not isinstance(task, Mapping) or set(task) - optional != {"message", "profile_id"}:
        raise ValueError("scheduled intent fields are invalid")
    if task["profile_id"] != profile_id:
        raise PermissionError("scheduled intent Profile differs from capture")
    if not isinstance(task["message"], str) or not is_saved_user_content(task["message"]):
        raise ValueError("scheduled intent message is invalid")
    destination = task.get("conversation_id")
    if destination is not None and (not isinstance(destination, str) or
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", destination) is None):
        raise ValueError("scheduled intent conversation identity is invalid")
    result = {"message": task["message"], "profile_id": profile_id,
              "conversation_id": destination,
              **saved_guidance_context(task, profile_id=profile_id)}
    if "workspace_id" in task:
        workspace_id = task["workspace_id"]
        if not isinstance(workspace_id, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", workspace_id
        ) is None:
            raise ValueError("scheduled intent workspace identity is invalid")
        result["workspace_id"] = workspace_id
    if "model" in task:
        model = task["model"]
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(
            ord(char) < 32 or ord(char) == 127 for char in model
        ):
            raise ValueError("scheduled intent model reference is invalid")
        result["model"] = model
    return result


def normalize_scheduled_saved_task(
    task: Mapping[str, Any], *, profile_id: str
) -> dict[str, Any]:
    """Keep only explicit finite scheduling preferences for the captured Profile."""
    if not isinstance(task, Mapping) or set(task) - {
        "chat_references", "tool_selection", "action_approval_mode", "workspace_id"
    } != {"message", "profile_id", "conversation_id"}:
        raise ValueError("scheduled saved task fields are invalid")
    if task["profile_id"] != profile_id:
        raise PermissionError("scheduled saved task Profile differs from capture")
    if "workspace_id" in task:
        # The selected adapter must have freshly verified owned association.
        # This helper checks syntax only; the preference remains in its
        # immutable job envelope and never broadens normal saved request ABI.
        normalize_scheduled_intent(task, profile_id=profile_id)
    # Reuse the saved ABI's exact opaque identity/content bounds without
    # materializing any execution state or inferring references from text.
    validate_saved_conversation_input({"request": {
        "turn_id": "scheduled-validation", "conversation_id": task["conversation_id"],
        "conversation_revision": 1, "content": task["message"],
    }})
    if not isinstance(task["message"], str):
        raise ValueError("scheduled saved task message must be text")
    return {
        "message": task["message"], "profile_id": profile_id,
        "conversation_id": task["conversation_id"],
        **saved_guidance_context(task, profile_id=profile_id),
    }
