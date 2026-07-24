"""Invoke one selected local service adapter without importing its domain."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient

LOCAL_OPERATION = "rumi.service.tool.local.operation.v1"
_EXPECTED_CONSUMER = "rumi_tool_broker_pack"


def create_execute_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create a generic local adapter executor."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "execute":
            raise ValueError(f"unknown local executor operation: {name}")
        if payload.get("_contract_consumer_pack_id") != _EXPECTED_CONSUMER:
            raise PermissionError("local executor consumer is not authorized")
        definition = payload.get("definition")
        definition = definition if isinstance(definition, Mapping) else {}
        execution = definition.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        contract_id = str(execution.get("contract_id") or "").strip()
        provider_instance_id = str(
            execution.get("provider_instance_id") or ""
        ).strip()
        if contract_id != LOCAL_OPERATION or not provider_instance_id:
            raise ValueError("local tool execution descriptor is invalid")
        return client.invoke(
            LOCAL_OPERATION,
            "invoke",
            {
                "tool_id": payload.get("tool_id"),
                "tool_call_id": payload.get("tool_call_id"),
                "caller_id": payload.get("caller_id"),
                "profile_id": payload.get("profile_id"),
                "arguments": dict(payload.get("arguments") or {}),
                "deadline": payload.get("deadline"),
                "authorization": payload.get("authorization"),
            },
            provider_instance_id=provider_instance_id,
        )

    return operation

