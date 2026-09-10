"""Resolve owned tool definitions and dispatch through captured Host authority."""

from __future__ import annotations

import re
import threading
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)

PACK_ID = "rumi_tool_broker_pack"
CONTRACT = "tobkiri.service.tool.invoke.v1"
OPERATION = f"{PACK_ID}.tool-invoke"
DEFINITION = "tobkiri.resource.tool.definition.v1"
VALIDATE = "tobkiri.service.tool.arguments.validate.v1"
EXECUTE = "tobkiri.service.tool.execute.v1"
NORMALIZE = "tobkiri.service.tool.result.normalize.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
# These are protocol adapters, not tool-name branches. Their nested effects
# still require separate signed edges and Authority checks in the same Broker.
_EXECUTORS = {
    "local": ("rumi_tool_local_executor_pack", "tool-executor.local", "tool-local-execute"),
    "mcp": ("rumi_tool_mcp_executor_pack", "tool-executor.mcp", "tool-mcp-execute"),
}


def _bind(context: HostProviderCaptureContextV4) -> HostFunction:
    # An outer synchronous Broker invocation must not fill every worker with
    # calls waiting on their own nested execution. Reject contention, don't queue.
    single_flight = threading.BoundedSemaphore(1)

    def invoke(
        payload: Mapping[str, Any], invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        if (
            set(payload) - {"expected_definition_hash"} != {"tool_id", "tool_call_id", "arguments"}
            or any(
                not isinstance(payload[key], str)
                or not _IDENTIFIER.fullmatch(payload[key])
                for key in ("tool_id", "tool_call_id")
            )
            or not isinstance(payload["arguments"], dict)
            or ("expected_definition_hash" in payload and (
                not isinstance(payload["expected_definition_hash"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["expected_definition_hash"])
            ))
        ):
            raise ValueError("tool invocation payload is invalid")
        if not single_flight.acquire(blocking=False):
            raise PermissionError("tool broker is busy")
        try:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({DEFINITION, VALIDATE, EXECUTE, NORMALIZE}),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            resolved = client.invoke(
                DEFINITION, "rumi_tool_registry_pack.tool-definition-resource",
                {"operation": "resolve", "tool_id": payload["tool_id"]},
            )
            if (
                not isinstance(resolved, Mapping)
                or resolved.get("found") is not True
                or not isinstance(resolved.get("definition"), Mapping)
                or not isinstance(resolved.get("resolved_tool_id"), str)
            ):
                raise ValueError("tool is not registered")
            definition = resolved["definition"]
            if ("expected_definition_hash" in payload
                    and payload["expected_definition_hash"] != definition.get("definition_hash")):
                raise PermissionError("selected tool definition changed")
            validation = client.invoke(
                VALIDATE, "rumi_tool_validation_pack.tool-arguments-validate",
                {"schema": definition.get("input_schema"), "arguments": payload["arguments"]},
            )
            if (
                not isinstance(validation, Mapping)
                or validation.get("valid") is not True
                or validation.get("coerced") is not False
                or validation.get("arguments") != payload["arguments"]
            ):
                raise ValueError("tool arguments are invalid")
            execution = definition.get("execution")
            if not isinstance(execution, Mapping) or execution.get("kind") not in _EXECUTORS:
                raise PermissionError("tool execution kind is unavailable")
            pack, function, operation = _EXECUTORS[execution["kind"]]
            operation_id = f"{pack}.{operation}"
            candidates = [
                item for item in client.providers(EXECUTE)
                if item.get("function_id") == f"{pack}.{function}"
                and item.get("operation_id") == operation_id
                and item.get("backend_id")
                and not item.get("backend_unavailable_reason")
            ]
            if len(candidates) != 1:
                raise PermissionError("selected tool executor is unavailable")
            selected = candidates[0]
            request = {
                "tool_call_id": payload["tool_call_id"],
                "tool_id": resolved["resolved_tool_id"],
                "arguments": validation["arguments"],
                "definition": dict(definition),
            }
            if execution["kind"] == "mcp":
                request = _mcp_request(context, execution, validation["arguments"])
            # No grant, approval flag, identity or legacy token is forwarded.
            # Denials and pending effects propagate; they are never retried here.
            invocation.assert_current()
            raw = client.invoke(
                EXECUTE, operation_id, request,
                provider_instance_id=selected["provider_instance_id"],
            )
            return client.invoke(
                NORMALIZE, "rumi_tool_result_pack.tool-result-normalize",
                {
                    "tool_call_id": payload["tool_call_id"],
                    "tool_id": resolved["resolved_tool_id"],
                    "executor_provider_instance_id": selected["provider_instance_id"],
                    "executor_content_hash": selected["implementation_digest"],
                    "value": raw,
                },
            )
        finally:
            single_flight.release()

    return invoke


def _mcp_request(
    context: HostProviderCaptureContextV4,
    execution: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    contract = "tobkiri.service.mcp.tool.call.v1"
    bindings = [
        item for item in context.catalog_bindings
        if item.operation.contract_id == contract
        and item.operation.operation_id == "rumi_mcp_gateway_pack.mcp-tool-call"
    ]
    if len(bindings) != 1:
        raise PermissionError("selected MCP gateway is unavailable")
    binding = bindings[0]
    provider = binding.function.function_id.removeprefix(f"{binding.artifact.pack_id}.")
    namespace = execution.get("namespace")
    if (
        execution.get("contract_id") != contract
        or execution.get("provider_instance_id") != provider
        or not isinstance(namespace, str)
        or not re.fullmatch(r"mcp\.[a-z0-9][a-z0-9._-]{0,127}", namespace)
        or any(
            not isinstance(execution.get(key), str)
            or not _IDENTIFIER.fullmatch(execution[key])
            for key in ("connection_id", "operation")
        )
    ):
        raise ValueError("MCP tool execution descriptor is invalid")
    return {
        "connection_id": execution["connection_id"],
        "tool": execution["operation"],
        "arguments": dict(arguments),
    }


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=f"{PACK_ID}.tool-broker.invoke",
    contract_id=CONTRACT,
    operation_id=OPERATION,
    bind=_bind,
)
