"""Authority approval bridge for HostIntent."""

from __future__ import annotations

from typing import Any

from core_runtime.host_permissions import get_host_permission_definition

from .models import HostIntent


def approval_reason(intent: HostIntent) -> str:
    definition = get_host_permission_definition(intent.operation)
    label = definition.label if definition is not None else intent.operation
    caller = " / ".join(item for item in (intent.caller_pack_id, intent.caller_function_id) if item)
    stream = " stream" if intent.is_stream else ""
    return f"{caller or 'pack'} requests {label}{stream}"


def check_host_intent_authority(
    service: Any,
    intent: HostIntent,
    *,
    principal_id: str,
    request_id: str | None = None,
    approval_token: str | None = None,
    consume_approval_token: bool = True,
) -> dict[str, Any]:
    definition = get_host_permission_definition(intent.operation)
    decision = service.check(
        principal_id=principal_id,
        permission_id=intent.operation,
        resource=intent.resource(),
        reason=approval_reason(intent),
        conversation_id=intent.conversation_id or None,
        request_id=request_id,
        approval_token=approval_token,
        consume_approval_token=consume_approval_token,
    )
    event = decision.to_dict()
    if decision.approval_required:
        resource = intent.resource()
        event = decision.to_approval_event()
        event.update(
            {
                "approval_kind": "host_intent",
                "host_intent": True,
                "operation": intent.operation,
                "args_hash": intent.args_hash,
                "typed_confirmation_required": bool(
                    definition.typed_confirmation_required if definition is not None else False
                ),
                "confirmation_phrase": resource.get("confirmation_phrase"),
            }
        )
    return event
