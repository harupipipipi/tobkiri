"""Finite defaultspack projection over the selected global tool broker."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractUnavailable,
    invoke_global_contract,
)

INVOKE_CONTRACT = "rumi.service.tool.invoke.v1"


def invoke(
    tool_id: str,
    arguments: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke the selected broker with host-bound caller/profile context."""
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise GlobalContractUnavailable("interface registry is unavailable")
    caller_id = str(
        context.get("owner_pack")
        or context.get("pack_id")
        or context.get("_source_pack_id")
        or "defaultspack"
    ).strip()
    profile_id = str(
        context.get("active_startup_profile_id")
        or context.get("profile_id")
        or "default"
    ).strip()
    value = invoke_global_contract(
        registry,
        INVOKE_CONTRACT,
        "invoke",
        {
            "tool_id": tool_id,
            "tool_call_id": context.get("tool_call_id"),
            "arguments": dict(arguments),
            "caller_id": caller_id,
            "profile_id": profile_id,
            "cancelled": bool(context.get("cancelled", False)),
            "deadline": context.get("deadline"),
            "approval_token": _approval_token(context),
            "approval_request_id": context.get("approval_request_id"),
        },
    )
    if not isinstance(value, Mapping):
        raise RuntimeError("global tool broker returned an invalid result")
    return dict(value)


def _approval_token(context: Mapping[str, Any]) -> Any:
    for key in (
        "authority_one_shot_token",
        "approval_token",
        "_tool_authority_token",
    ):
        value = context.get(key)
        if value:
            return value
    return None
