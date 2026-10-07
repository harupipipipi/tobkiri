"""Finite scheduled task projection into an owner-captured saved turn."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable, Mapping
import re

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import (
    saved_guidance_context, validate_saved_conversation_input,
    is_saved_user_content,
)
from ecosystem.rumi_turn_runtime_pack.runtime.input_context import (
    execute_with_input_context,
)
from ecosystem.rumi_turn_runtime_pack.runtime.saved import execute_saved_turn

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OP = "rumi_conversation_store_pack.conversation-resource"


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


def execute_scheduled_saved_task(
    task: Mapping[str, Any], schedule_id: str, execution_id: str, *,
    client: Any, store: Any, guard: Callable[[], None],
    recover_source: Callable[[str], Mapping[str, Any] | None],
    bind_source: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    track_execution: Callable[[str], AbstractContextManager[None]] = lambda _: nullcontext(),
) -> dict[str, Any]:
    """Resolve and bind once through fresh captured ports, then use normal saved execution.

    Source persistence callbacks are authenticated owner ports. Their immutable
    record must retain original conversation revision across occurrence replay;
    no session, authority handle, or request-derived callback is persisted.
    """
    profile_id = client.session.profile_id
    if store.profile_id != profile_id:
        raise PermissionError("scheduled saved store Profile differs from capture")
    normalized = normalize_scheduled_saved_task(task, profile_id=profile_id)
    if any(not isinstance(value, str) or not value or len(value) > 256
           for value in (schedule_id, execution_id)):
        raise ValueError("scheduled occurrence identity is invalid")
    turn_id = "calendar:" + canonical_digest(
        [profile_id, schedule_id, execution_id]
    ).removeprefix("sha256:")
    guard()
    recovered = recover_source(turn_id)
    if recovered is not None:
        source = validate_saved_conversation_input(recovered)
        expected = {
            "turn_id": turn_id, "conversation_id": normalized["conversation_id"],
            "content": normalized["message"],
            **{key: normalized[key] for key in ("chat_references", "tool_selection", "action_approval_mode")
               if key in normalized},
        }
        request = dict(source["request"])
        request.pop("conversation_revision", None)
        if request != expected:
            raise PermissionError("scheduled occurrence source was rebound")
    else:
        guard()
        result = client.invoke(CONVERSATION, CONVERSATION_OP, {
            "operation": "get", "profile_id": profile_id,
            "conversation_id": normalized["conversation_id"],
        })
        conversation = result.get("conversation") if isinstance(result, Mapping) else None
        if not isinstance(conversation, Mapping) or conversation.get("id") != normalized["conversation_id"]:
            raise ValueError("scheduled saved destination is unavailable")
        source = validate_saved_conversation_input({"request": {
            "turn_id": turn_id, "conversation_id": normalized["conversation_id"],
            "conversation_revision": conversation.get("conversation_revision"),
            "content": normalized["message"],
            **{key: normalized[key] for key in ("chat_references", "tool_selection", "action_approval_mode")
               if key in normalized},
        }})

    def bind(lean: Mapping[str, Any], accepted: Mapping[str, Any]) -> Mapping[str, Any]:
        guard()
        winner = validate_saved_conversation_input(bind_source(lean))
        if winner != lean:
            # A concurrent occurrence capture won. Use only its immutable
            # accepted owner input, never the losing current projection.
            captured = store.saved_input(winner)
            if captured is None:
                raise RuntimeError("scheduled winner capture is not available")
            return captured
        return store.bind_saved_input(lean, accepted)

    return execute_with_input_context(
        source, client=client, guard=guard,
        recover_input=store.saved_input, bind_input=bind,
        execute=lambda accepted: execute_saved_turn(
            store, accepted, client=client, guard=guard,
            track_execution=track_execution,
        ),
    )
