"""Forward a namespace-bound operation to one selected MCP gateway."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient

MCP_CALL = "rumi.service.mcp.tool.call.v1"
_NAMESPACE = re.compile(r"^mcp\.[a-z0-9][a-z0-9._-]{0,127}$")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EXPECTED_CONSUMER = "rumi_tool_broker_pack"


def create_execute_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create an MCP executor with explicit namespace isolation."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "execute":
            raise ValueError(f"unknown MCP executor operation: {name}")
        if payload.get("_contract_consumer_pack_id") != _EXPECTED_CONSUMER:
            raise PermissionError("MCP executor consumer is not authorized")
        definition = payload.get("definition")
        definition = definition if isinstance(definition, Mapping) else {}
        execution = definition.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        if str(execution.get("contract_id") or "") != MCP_CALL:
            raise ValueError("MCP tool contract is invalid")
        provider_instance_id = str(
            execution.get("provider_instance_id") or ""
        ).strip()
        namespace = str(execution.get("namespace") or "").strip()
        remote_operation = str(execution.get("operation") or "").strip()
        if (
            not provider_instance_id
            or not _NAMESPACE.fullmatch(namespace)
            or not _OPERATION.fullmatch(remote_operation)
        ):
            raise ValueError("MCP execution descriptor is invalid")
        return client.invoke(
            MCP_CALL,
            "call",
            {
                "namespace": namespace,
                "operation": remote_operation,
                "arguments": dict(payload.get("arguments") or {}),
                "tool_call_id": payload.get("tool_call_id"),
                "caller_id": payload.get("caller_id"),
                "profile_id": payload.get("profile_id"),
                "deadline": payload.get("deadline"),
            },
            provider_instance_id=provider_instance_id,
        )

    return operation

