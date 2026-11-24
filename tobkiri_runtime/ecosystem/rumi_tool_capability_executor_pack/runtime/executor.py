"""Execute declared capability permissions through the trusted core executor."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core_runtime.capability_executor import get_capability_executor

_EXPECTED_CONSUMER = "rumi_tool_broker_pack"


def create_execute_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create an adapter that retains core trust and grant enforcement."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "execute":
            raise ValueError(f"unknown capability executor operation: {name}")
        if payload.get("_contract_consumer_pack_id") != _EXPECTED_CONSUMER:
            raise PermissionError("capability executor consumer is not authorized")
        definition = payload.get("definition")
        definition = definition if isinstance(definition, Mapping) else {}
        execution = definition.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        permission_id = str(execution.get("contract_id") or "").strip()
        principal_id = str(payload.get("caller_id") or "").strip()
        arguments = payload.get("arguments")
        if not permission_id or not principal_id or not isinstance(arguments, Mapping):
            raise ValueError("capability tool execution descriptor is incomplete")
        response = get_capability_executor().execute(
            principal_id,
            {
                "permission_id": permission_id,
                "args": dict(arguments),
                "request_id": str(payload.get("tool_call_id") or ""),
                "timeout_seconds": _timeout(payload),
            },
        )
        value = response.to_dict()
        return {
            "result": value.get("output"),
            "is_error": not bool(value.get("success")),
            "error": {
                "code": str(value.get("error_type") or "capability_failed"),
                "message": str(value.get("error") or ""),
            }
            if not value.get("success")
            else None,
            "widget": None,
            "latency_ms": value.get("latency_ms"),
        }

    return operation


def _timeout(payload: Mapping[str, Any]) -> float:
    deadline = payload.get("deadline")
    try:
        return max(0.1, min(300.0, float(deadline) - time.time()))
    except (TypeError, ValueError):
        return 60.0

