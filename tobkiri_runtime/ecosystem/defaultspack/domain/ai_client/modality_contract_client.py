"""Finite legacy projection over selected AI modality gateways."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract


def invoke_modality(
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke one active modality owner and require an object result."""
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("interface registry is unavailable")
    request = dict(payload)
    model_id = str(request.get("model_id") or "")
    if "provider_id" not in request and "/" in model_id:
        request["provider_id"] = model_id.split("/", 1)[0]
    value = invoke_global_contract(
        registry,
        contract_id,
        operation,
        request,
    )
    if not isinstance(value, dict):
        raise RuntimeError("modality gateway returned an invalid result")
    return value
