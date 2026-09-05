"""Return auditable handoff intents without self-approving human control."""

from __future__ import annotations

from typing import Any, Callable, Mapping


def create_generate_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the non-streaming human handoff provider."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"generate", "invoke"}:
            raise ValueError(f"unknown human provider operation: {name}")
        return {
            "status": "ok",
            "output": "",
            "tool_intents": [_intent(payload)],
            "finish_reason": "human_handoff_required",
            "usage": {},
            "usage_provenance": "not_applicable",
        }

    return operation


def create_stream_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the streaming human handoff provider."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"stream", "invoke"}:
            raise ValueError(f"unknown human stream operation: {name}")
        return {
            "events": [
                {"type": "tool_intent_delta", "tool_intent": _intent(payload)},
                {"type": "finish", "finish_reason": "human_handoff_required"},
            ]
        }

    return operation


def _intent(payload: Mapping[str, Any]) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("human handoff request_id is required")
    return {
        "name": "rumi.human.handoff.request",
        "arguments": {
            "request_id": request_id,
            "conversation_id": payload.get("conversation_id"),
            "model_id": payload.get("model_id"),
        },
        "approval_required": True,
        "authority_granted": False,
    }

