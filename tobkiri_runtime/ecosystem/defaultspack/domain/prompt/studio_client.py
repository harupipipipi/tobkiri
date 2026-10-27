"""Read-only consumer of the optional global Prompt Studio resource."""

from __future__ import annotations

from typing import Any

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    invoke_global_contract,
)

_RESOURCE_CONTRACT = "rumi.resource.prompt.studio.v1"
_AUTHOR_CONTRACT = "rumi.action.prompt.author.v1"


def prompt_owner_available() -> bool:
    """Return whether the active plan selected the authored prompt resource."""
    try:
        from core_runtime.resolved_profile_scope import active_resolved_profile

        plan = active_resolved_profile()
    except Exception:
        plan = None
    return bool(
        plan is not None
        and any(
            item.contract_id == _RESOURCE_CONTRACT
            for item in plan.providers
        )
    )


def invoke_prompt_contract(
    contract_id: str,
    operation: str,
    payload: dict[str, Any],
) -> Any:
    """Invoke the active prompt owner through the generic registry."""
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise GlobalContractUnavailable("interface registry is unavailable")
    return invoke_global_contract(registry, contract_id, operation, payload)


def authored_prompts(profile_id: str) -> list[dict[str, Any]]:
    """Project authored prompts from the optional owner as read-only records."""
    try:
        result = invoke_prompt_contract(
            _RESOURCE_CONTRACT,
            "list",
            {"profile_id": profile_id},
        )
    except (
        GlobalContractInvocationError,
        GlobalContractUnavailable,
        KeyError,
        ValueError,
    ):
        return []
    prompts = result.get("prompts") if isinstance(result, dict) else None
    if not isinstance(prompts, list):
        return []
    return [dict(item) for item in prompts if isinstance(item, dict)]


def authored_edge_states(profile_id: str) -> dict[str, bool]:
    """Return composition edge state owned by the optional prompt pack."""
    try:
        result = invoke_prompt_contract(
            _RESOURCE_CONTRACT,
            "edge_states",
            {"profile_id": profile_id},
        )
    except (
        GlobalContractInvocationError,
        GlobalContractUnavailable,
        KeyError,
        ValueError,
    ):
        return {}
    values = result.get("edge_states") if isinstance(result, dict) else None
    if not isinstance(values, dict):
        return {}
    return {str(key): bool(value) for key, value in values.items()}


def write_authored_edge_state(
    profile_id: str,
    edge_id: str,
    enabled: bool,
) -> dict[str, Any]:
    """Persist one composition edge through the active prompt owner."""
    return write_authored_prompt(
        profile_id,
        "edge.toggle",
        {"edge_id": edge_id, "enabled": enabled},
    )


def write_authored_prompt(
    profile_id: str,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Forward a finite legacy mutation to the one authoritative owner."""
    result = invoke_prompt_contract(
        _AUTHOR_CONTRACT,
        operation,
        {"profile_id": profile_id, **payload},
    )
    if not isinstance(result, dict):
        raise RuntimeError("prompt owner returned an invalid result")
    return result


def compact_prompt_via_owner(body: str) -> dict[str, Any]:
    """Request optional authoring compaction without owning its algorithm."""
    try:
        from core_runtime.profile_paths import active_profile_id

        profile_id = str(active_profile_id() or "").strip()
        if not profile_id:
            return {}
        result = invoke_prompt_contract(
            _AUTHOR_CONTRACT,
            "compact",
            {"profile_id": profile_id, "body": str(body)},
        )
        return dict(result) if isinstance(result, dict) else {}
    except (
        GlobalContractInvocationError,
        GlobalContractUnavailable,
        KeyError,
        ValueError,
    ):
        return {}


def authored_prompt(
    profile_id: str,
    prompt_ids: list[str],
) -> dict[str, Any] | None:
    """Return the first authored prompt from the active provider, if installed."""
    for prompt_id in prompt_ids:
        try:
            result = invoke_prompt_contract(
                _RESOURCE_CONTRACT,
                "get",
                {"profile_id": profile_id, "prompt_id": prompt_id},
            )
        except (
            GlobalContractInvocationError,
            GlobalContractUnavailable,
            KeyError,
            ValueError,
        ):
            continue
        if not isinstance(result, dict):
            continue
        prompt = result.get("prompt")
        if isinstance(prompt, dict) and prompt.get("enabled", True):
            return dict(prompt)
    return None
