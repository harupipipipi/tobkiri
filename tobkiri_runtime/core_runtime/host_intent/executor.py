"""HostIntent prepare/approval executor.

This module intentionally does not touch host APIs. It validates JSON intents and
routes them through Authority. Actual host mediation belongs to the Viewer/host
capability broker.
"""

from __future__ import annotations

from typing import Any

from .models import is_host_intent_payload
from .validator import validate_host_intent


class HostIntentExecutor:
    def handle(
        self,
        payload: dict[str, Any],
        *,
        principal_id: str,
        caller_pack_id: str,
        caller_function_id: str,
        request_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = request_context if isinstance(request_context, dict) else {}
        validation = validate_host_intent(
            payload,
            caller_pack_id=caller_pack_id,
            caller_function_id=caller_function_id,
            conversation_id=str(context.get("conversation_id") or ""),
        )
        if not validation.ok or validation.intent is None:
            return {
                "status": "error",
                "success": False,
                "error_type": "host_intent_invalid",
                "errors": validation.errors,
            }
        intent = validation.intent
        del principal_id
        return {
            "status": "error",
            "success": False,
            "error_type": "v4_operation_unavailable",
            "error": (
                "HostIntent legacy execution is disabled; invoke a declared "
                "Pack v4 operation through V4DispatchSession"
            ),
            "operation": intent.operation,
        }
def maybe_handle_host_intent_output(
    output: Any,
    *,
    principal_id: str,
    caller_pack_id: str,
    caller_function_id: str,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not is_host_intent_payload(output):
        return None
    return HostIntentExecutor().handle(
        output,
        principal_id=principal_id,
        caller_pack_id=caller_pack_id,
        caller_function_id=caller_function_id,
        request_context=request_context,
    )
