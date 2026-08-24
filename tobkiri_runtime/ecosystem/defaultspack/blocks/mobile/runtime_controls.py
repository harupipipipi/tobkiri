"""Scoped mobile facade for authoritative PC runtime controls."""

from __future__ import annotations

from typing import Any

from blocks._common import error, ok
from domain.frontend.command_protocol import CommandProtocolRegistry


_INVOKE_FIELDS = {
    "args",
    "catalog_revision",
    "client_sequence",
    "command_ref",
    "conversation_id",
    "expected_revision",
    "idempotency_key",
    "invocation_id",
    "mode",
}


def run(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Query or request PC-authoritative control state for a scoped device."""

    payload = input_data if isinstance(input_data, dict) else {}
    operation = str(payload.get("_mobile_control_operation") or "").strip()
    registry = CommandProtocolRegistry()
    if operation == "query_states":
        state_refs = payload.get("state_refs", [])
        if not isinstance(state_refs, list) or any(
            not isinstance(item, str) for item in state_refs
        ):
            return error("state_refs must be an array of strings", "INVALID_INPUT")
        return ok(registry.query_states(state_refs))
    if operation == "invoke":
        request = {key: payload[key] for key in _INVOKE_FIELDS if key in payload}
        if not str(request.get("command_ref") or "").strip():
            return error("command_ref is required", "INVALID_INPUT")
        return ok(registry.invoke(request, context if isinstance(context, dict) else {}))
    return error("unsupported mobile control operation", "METHOD_NOT_ALLOWED")
