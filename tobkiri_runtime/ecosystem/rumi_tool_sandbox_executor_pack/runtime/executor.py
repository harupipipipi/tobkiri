"""Execute only registry entries declared with the Docker calling convention."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core_runtime.capability_executor import get_capability_executor
from core_runtime.di_container import get_container

_EXPECTED_CONSUMER = "rumi_tool_broker_pack"


def create_execute_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a sandbox executor that cannot downgrade to host execution."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "execute":
            raise ValueError(f"unknown sandbox executor operation: {name}")
        if payload.get("_contract_consumer_pack_id") != _EXPECTED_CONSUMER:
            raise PermissionError("sandbox executor consumer is not authorized")
        definition = payload.get("definition")
        definition = definition if isinstance(definition, Mapping) else {}
        execution = definition.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        permission_id = str(execution.get("contract_id") or "").strip()
        caller_id = str(payload.get("caller_id") or "").strip()
        arguments = payload.get("arguments")
        if not permission_id or not caller_id or not isinstance(arguments, Mapping):
            raise ValueError("sandbox execution descriptor is incomplete")
        registry = get_container().get_or_none("function_registry")
        entry = (
            registry.get_by_permission_id(permission_id)
            if registry is not None
            else None
        )
        if entry is None or entry.calling_convention != "python_docker":
            raise PermissionError("sandbox tool is not bound to a Docker handler")
        response = get_capability_executor().execute(
            caller_id,
            {
                "permission_id": permission_id,
                "args": dict(arguments),
                "request_id": str(payload.get("tool_call_id") or ""),
                "timeout_seconds": _timeout(payload),
            },
        ).to_dict()
        return {
            "result": response.get("output"),
            "is_error": not bool(response.get("success")),
            "error": {
                "code": str(response.get("error_type") or "sandbox_failed"),
                "message": str(response.get("error") or ""),
            }
            if not response.get("success")
            else None,
            "widget": None,
            "latency_ms": response.get("latency_ms"),
        }

    return operation


def _timeout(payload: Mapping[str, Any]) -> float:
    try:
        return max(0.1, min(300.0, float(payload.get("deadline")) - time.time()))
    except (TypeError, ValueError):
        return 60.0

