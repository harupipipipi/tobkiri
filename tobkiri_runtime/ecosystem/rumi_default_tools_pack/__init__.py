"""Concrete default tools for Rumi."""

from __future__ import annotations

from typing import Any, Mapping


def run_host_contract_action(
    action: str,
    payload: Mapping[str, Any] | None,
    *,
    source_function_id: str,
) -> dict[str, Any]:
    """Invoke the public default-tools projection over a captured Host session."""

    from .domain.tool.host_contract_adapter import run_host_contract_action as invoke

    return invoke(action, payload, source_function_id=source_function_id)


__all__ = ["run_host_contract_action"]
