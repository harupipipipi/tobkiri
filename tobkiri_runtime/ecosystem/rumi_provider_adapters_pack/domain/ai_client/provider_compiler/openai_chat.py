"""Compile abstract reasoning controls for the bound OpenRouter chat route."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError


def compile_chat_parameters(
    parameters: Mapping[str, Any],
    *,
    endpoint: str,
) -> dict[str, Any]:
    """Translate the saved-turn control without changing transport permissions."""
    result = dict(parameters)
    if endpoint != "https://openrouter.ai/api/v1/chat/completions":
        return result
    if "thinking_level" not in result:
        return result
    level = result.pop("thinking_level")
    if level is None:
        return result
    efforts = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high"}
    if type(level) is not str or level not in efforts:
        raise GlobalContractInvocationError(
            "invalid_request", "OpenRouter thinking level is unsupported"
        )
    effort = efforts[level]
    if "reasoning_effort" in result and result["reasoning_effort"] != effort:
        raise GlobalContractInvocationError(
            "invalid_request", "OpenRouter reasoning controls conflict"
        )
    result["reasoning_effort"] = effort
    return result
