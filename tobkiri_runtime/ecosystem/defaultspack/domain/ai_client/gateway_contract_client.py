"""Finite legacy projection over the active provider-neutral AI gateway."""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
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
    tool_intents = [
        dict(item)
        for item in value.get("tool_intents") or []
        if isinstance(item, Mapping)
    ]
    return {
        **value,
        "content": _legacy_content(value.get("output"), tool_intents),
        "tool_calls": tool_intents,
    }


def _legacy_content(
    output: Any,
    tool_intents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project gateway output and intents to the chat engine's block schema."""
    blocks: list[dict[str, Any]] = []
    if isinstance(output, list):
        blocks.extend(dict(item) for item in output if isinstance(item, Mapping))
    elif output not in {None, ""}:
        blocks.append({"type": "text", "text": str(output)})
    for intent in tool_intents:
        blocks.append(
            {
                "type": "tool_use",
                "id": str(intent.get("intent_id") or ""),
                "name": str(intent.get("operation") or ""),
                "input": dict(intent.get("arguments") or {}),
            }
        )
    return blocks


def stream(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Invoke the stream contract and project typed events to chat chunks."""
    value = _invoke(_STREAM_CONTRACT, "stream", payload)
    events = value.get("events") if isinstance(value, dict) else None
    if not isinstance(events, list):
        raise RuntimeError("AI gateway returned an invalid stream")
    projected: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    for item in events:
        if not isinstance(item, Mapping):
            continue
        event_type = str(item.get("type") or "")
        if event_type == "text_delta":
            text = str(item.get("delta") or "")
            if text:
                projected.append(
                    {
                        "type": "content_delta",
                        "delta": {"type": "text", "text": text},
                    }
                )
        elif event_type == "thinking_delta":
            text = str(item.get("delta") or "")
            if text:
                projected.append(
                    {
                        "type": "reasoning_delta",
                        "delta": {"type": "text", "text": text},
                    }
                )
        elif event_type == "tool_intent_delta":
            intent = item.get("tool_intent")
            if isinstance(intent, Mapping):
                projected.append(
                    {
                        "type": "tool_use",
                        "id": str(intent.get("intent_id") or ""),
                        "name": str(intent.get("operation") or ""),
                        "input": dict(intent.get("arguments") or {}),
                    }
                )
        elif event_type == "usage":
            if isinstance(item.get("usage"), Mapping):
                usage = dict(item["usage"])
                if isinstance(item.get("usage_cost"), Mapping):
                    usage["usage_cost"] = dict(item["usage_cost"])
        elif event_type == "finish":
            projected.append(
                {
                    "type": "stream_end",
                    "finish_reason": str(item.get("finish_reason") or "stop"),
                    "usage": usage,
                }
            )
        elif event_type == "error":
            projected.append(
                {
                    "type": "stream_end",
                    "finish_reason": "error",
                    "usage": usage,
                }
            )
    return projected


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
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
        model = str(request.get("model") or request.get("model_reference") or "")
        parameters = request.get("params")
        if not isinstance(parameters, Mapping):
            parameters = request.get("parameters")
        parameters = dict(parameters or {}) if isinstance(parameters, Mapping) else {}
        try:
            return generate(
                {
                    "request_id": request.get("request_id"),
                    "messages": list(request.get("messages") or []),
                    "tools": list(request.get("tools") or []),
                    "parameters": parameters,
                    "model_reference": model,
                    "conversation_id": request.get("conversation_id"),
                    "profile_id": request.get("profile_id"),
                    "idempotency_key": request.get("idempotency_key"),
                    "authority_context": dict(request.get("authority_context") or {})
                    if isinstance(request.get("authority_context"), Mapping)
                    else {},
                    "requirements": {
                        "preferred_model_id": model,
                        "tool_calling": bool(request.get("tools")),
                        "request_surface": "legacy.chat",
                    },
                }
            )
        except (GlobalContractInvocationError, GlobalContractUnavailable):
            raise

    def stream(self, request: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        """Project one legacy stream request through the selected owner."""
        model = str(request.get("model") or request.get("model_reference") or "")
        parameters = request.get("params")
        if not isinstance(parameters, Mapping):
            parameters = request.get("parameters")
        parameters = dict(parameters or {}) if isinstance(parameters, Mapping) else {}
        try:
            return iter(
                stream(
                    {
                        "request_id": request.get("request_id"),
                        "messages": list(request.get("messages") or []),
                        "tools": list(request.get("tools") or []),
                        "parameters": parameters,
                        "model_reference": model,
                        "conversation_id": request.get("conversation_id"),
                        "profile_id": request.get("profile_id"),
                        "idempotency_key": request.get("idempotency_key"),
                        "authority_context": dict(request.get("authority_context") or {})
                        if isinstance(request.get("authority_context"), Mapping)
                        else {},
                        "requirements": {
                            "preferred_model_id": model,
                            "tool_calling": bool(request.get("tools")),
                            "request_surface": "legacy.chat_stream",
                        },
                    }
                )
            )
        except (GlobalContractInvocationError, GlobalContractUnavailable):
            raise
