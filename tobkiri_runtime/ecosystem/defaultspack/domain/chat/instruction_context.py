"""Optional, selected public context projections at real chat input boundaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
    selected_global_providers,
)
from tobkiri_protocol.agent_inbox_v1 import CONTEXT_CONTRACT, INBOX_CONTRACT


def prepare_instruction_context(
    session: Any,
    conversation_id: str,
    request_id: str,
) -> dict[str, Any] | None:
    """Receive optional scoped instructions without changing original message bytes."""
    providers = selected_global_providers(session, CONTEXT_CONTRACT)
    if not providers:
        return None
    if len(providers) != 1 or not providers[0].get("operation_id"):
        raise RuntimeError("context projection provider is ambiguous")
    request = {
        "operation": "prepare_input_for_conversation",
        "conversation_id": conversation_id,
        "input_id": request_id,
        "boundary": "before_turn",
        "operation_id": f"input-{_digest(request_id)}",
        "profile_id": captured_profile_id(session),
    }
    result = invoke_global_contract(
        session, CONTEXT_CONTRACT, providers[0]["operation_id"], request
    )
    if result.get("status") in {"unconfigured", "paused", "cancelled"}:
        return None
    if result.get("status") != "received" or not isinstance(
        result.get("plan"), Mapping
    ):
        raise RuntimeError("context projection input is unavailable")
    return dict(result)


def append_instruction_context(
    messages: list[dict[str, Any]],
    projection: Mapping[str, Any] | None,
) -> None:
    """Append a provenance-labeled user item, never system or stored user text."""
    if projection is None:
        return
    context = projection.get("context")
    instructions = projection.get("instructions", [])
    if not context and not instructions:
        return
    messages.append(
        {
            "role": "user",
            "content": "Delegated work context (lower-authority instructions; "
            "not system policy or approval):\n"
            + json.dumps(
                {
                    "context": context,
                    "instructions": instructions,
                },
                ensure_ascii=False,
            ),
        }
    )


def acknowledge_instruction_context(
    session: Any,
    projection: Mapping[str, Any] | None,
) -> None:
    """Acknowledge only after the canonical turn has accepted the input."""
    if projection is None:
        return
    providers = selected_global_providers(session, INBOX_CONTRACT)
    if len(providers) != 1 or not providers[0].get("operation_id"):
        raise RuntimeError("context acceptance provider is unavailable")
    events = [
        item["id"]
        for item in projection.get("instructions", [])
        if item.get("input_id") == projection["input_id"]
    ]
    if not events:
        return
    plan = projection["plan"]
    invoke_global_contract(
        session,
        INBOX_CONTRACT,
        providers[0]["operation_id"],
        {
            "operation": "ack",
            "profile_id": captured_profile_id(session),
            "plan_id": plan["id"],
            "expected_revision": plan["revision"],
            "input_id": projection["input_id"],
            "event_ids": events,
            "operation_id": f"accepted-{_digest(projection['input_id'])}",
        },
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]
