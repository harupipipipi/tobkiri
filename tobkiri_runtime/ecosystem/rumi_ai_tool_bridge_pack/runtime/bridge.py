"""Normalize provider tool calls into non-authoritative operation descriptors."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def create_tool_intent_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a pure provider-tool-call normalization operation."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"normalize", "validate"}:
            raise ValueError(f"unknown AI tool bridge operation: {name}")
        values = payload.get("intents")
        values = values if isinstance(values, list) else []
        request_id = str(payload.get("request_id") or "").strip()
        normalized = [
            _normalize(item, request_id, index)
            for index, item in enumerate(values)
            if isinstance(item, Mapping)
        ]
        if len(normalized) != len(values):
            raise _invalid("tool intent must be an object")
        return {"intents": normalized}

    return operation


def _normalize(
    value: Mapping[str, Any],
    request_id: str,
    index: int,
) -> dict[str, Any]:
    function = value.get("function")
    function = function if isinstance(function, Mapping) else value
    name = str(function.get("name") or value.get("name") or "").strip()
    if _TOOL_NAME.fullmatch(name) is None:
        raise _invalid("tool intent name is invalid")
    arguments = function.get("arguments", value.get("arguments", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise _invalid("tool intent arguments are invalid JSON") from None
    if not isinstance(arguments, Mapping):
        raise _invalid("tool intent arguments must be an object")
    intent_id = str(value.get("id") or f"{request_id}:tool:{index}")
    return {
        "intent_id": intent_id,
        "request_id": request_id,
        "operation": name,
        "arguments": dict(arguments),
        "authority_granted": False,
        "approved": False,
        "approval_status": "unrequested",
        "executes": False,
    }


def _invalid(message: str) -> GlobalContractInvocationError:
    return GlobalContractInvocationError("invalid_response", message)

