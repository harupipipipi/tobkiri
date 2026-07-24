"""Finite legacy projection over the active provider-neutral AI gateway."""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractUnavailable,
    invoke_global_contract,
)
from core_runtime.profile_paths import active_profile_id

_GENERATE_CONTRACT = "rumi.service.ai.generate.v1"
_STREAM_CONTRACT = "rumi.service.ai.stream.v1"


def generate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Invoke the selected gateway and project its result to legacy fields."""
    value = _invoke(_GENERATE_CONTRACT, "generate", payload)
    if not isinstance(value, dict):
        raise RuntimeError("AI gateway returned an invalid result")
    return {
        **value,
        "content": value.get("output"),
        "tool_calls": list(value.get("tool_intents") or []),
    }


def stream(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Invoke the distinct stream contract and return normalized events."""
    value = _invoke(_STREAM_CONTRACT, "stream", payload)
    events = value.get("events") if isinstance(value, dict) else None
    if not isinstance(events, list):
        raise RuntimeError("AI gateway returned an invalid stream")
    return [dict(item) for item in events if isinstance(item, dict)]


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("interface_registry")
    if registry is None:
        raise GlobalContractUnavailable("interface registry is unavailable")
    request = dict(payload)
    profile_id = str(request.get("profile_id") or active_profile_id() or "").strip()
    if profile_id:
        request["profile_id"] = profile_id
    return invoke_global_contract(
        registry,
        contract_id,
        operation,
        request,
    )


class ContractLLMGateway:
    """Compatibility object for orchestration that expects gateway methods."""

    def supports_stream(self, model: str) -> bool:
        """Report the stream capability exposed by the global stream contract.

        Model-specific normalization and rejection remain owned by the AI
        gateway pack.  This compatibility adapter only declares that its
        ``stream`` method is a real contract-backed implementation.
        """
        return bool(str(model or "").strip())

    def complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Project one legacy gateway request through the selected owner."""
        model = str(request.get("model") or "")
        return generate(
            {
                "messages": list(request.get("messages") or []),
                "tools": list(request.get("tools") or []),
                "parameters": dict(request.get("params") or {}),
                "model_reference": model,
                "conversation_id": request.get("conversation_id"),
                "profile_id": request.get("profile_id"),
                "idempotency_key": request.get("idempotency_key"),
                "requirements": {
                    "preferred_model_id": model,
                    "tool_calling": bool(request.get("tools")),
                    "request_surface": "legacy.chat",
                },
            }
        )

    def stream(self, request: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        """Project one legacy stream request through the selected owner."""
        model = str(request.get("model") or "")
        return iter(
            stream(
                {
                    "messages": list(request.get("messages") or []),
                    "tools": list(request.get("tools") or []),
                    "parameters": dict(request.get("params") or {}),
                    "model_reference": model,
                    "conversation_id": request.get("conversation_id"),
                    "profile_id": request.get("profile_id"),
                    "idempotency_key": request.get("idempotency_key"),
                    "requirements": {
                        "preferred_model_id": model,
                        "tool_calling": bool(request.get("tools")),
                        "request_surface": "legacy.chat_stream",
                    },
                }
            )
        )
