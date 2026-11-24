"""Finite scheduled task projection into an owner-captured saved turn."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.scheduled_task_v1 import normalize_scheduled_saved_task
from ecosystem.rumi_turn_runtime_pack.runtime.input_context import (
    execute_with_input_context,
)
from ecosystem.rumi_turn_runtime_pack.runtime.saved import execute_saved_turn

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OP = "rumi_conversation_store_pack.conversation-resource"


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
