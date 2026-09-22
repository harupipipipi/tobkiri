"""Finite defaultspack compatibility adapter for Wave 8 media contracts."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from core_runtime.resolved_profile_scope import active_resolved_profile


CLIPBOARD_READ = "rumi.resource.clipboard.v1"
CLIPBOARD_WRITE = "rumi.action.clipboard.v1"
MEDIA_CAPTURE = "rumi.action.media.capture.v1"
MEDIA_INSPECT = "rumi.service.media.inspect.v1"


def invoke_media_contract(
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
    *,
    source_function_id: str,
) -> dict[str, Any]:
    """Invoke one selected owner without direct host or file access."""

    registry = get_container().get_or_none("v4_dispatch_session")
    plan = active_resolved_profile()
    if registry is None or plan is None:
        raise RuntimeError("global media owner is unavailable")
    request = {
        "profile_id": plan.profile_id,
        **dict(payload),
        "_contract_consumer_pack_id": "defaultspack",
        "_contract_consumer_function_id": source_function_id,
    }
    result = invoke_global_contract(registry, contract_id, operation, request)
    if not isinstance(result, dict):
        raise RuntimeError("media owner returned an invalid result")
    return result


def execute_ui_host_contract(
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
    *,
    source_function_id: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a UI-origin HostIntent through core Authority and Viewer broker."""

    from core_runtime.host_intent import maybe_handle_host_intent_output

    intent = invoke_media_contract(
        contract_id,
        operation,
        payload,
        source_function_id=source_function_id,
    )
    request_context = dict(context or {})
    handled = maybe_handle_host_intent_output(
        intent,
        principal_id=str(request_context.get("principal_id") or "defaultspack-ui"),
        caller_pack_id="defaultspack",
        caller_function_id=source_function_id,
        request_context=request_context,
    )
    if not isinstance(handled, dict):
        raise RuntimeError("media host owner did not return a HostIntent")
    return handled
