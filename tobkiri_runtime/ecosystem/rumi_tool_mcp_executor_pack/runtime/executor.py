"""Forward a namespace-bound operation to one selected MCP gateway."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

MCP_CALL = "tobkiri.service.mcp.tool.call.v1"
MCP_OPERATION = "rumi_mcp_gateway_pack.mcp-tool-call"
_CONTRACT = "tobkiri.service.tool.execute.v1"
_FUNCTION = "rumi_tool_mcp_executor_pack.tool-executor.mcp"
_OPERATION_ID = "rumi_tool_mcp_executor_pack.tool-mcp-execute"
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
        connection_id = execution.get("connection_id")
        if (
            not provider_instance_id
            or not _NAMESPACE.fullmatch(namespace)
            or not _OPERATION.fullmatch(remote_operation)
            or not isinstance(connection_id, str)
            or not _OPERATION.fullmatch(connection_id)
        ):
            raise ValueError("MCP execution descriptor is invalid")
        return client.invoke(
            MCP_CALL,
            MCP_OPERATION,
            {
                "connection_id": connection_id,
                "tool": remote_operation,
                "arguments": dict(payload.get("arguments") or {}),
            },
            provider_instance_id=provider_instance_id,
        )

    return operation


class McpExecutorHostFactoryV4:
    """Forward one admitted tool call to the selected sandbox Gateway."""

    function_id = _FUNCTION

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Retain exact executable identity and its one declared dependency."""
        if len(context.provider_bindings) != 1:
            raise PermissionError("MCP executor binding is unavailable")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != _FUNCTION
            or binding.operation.contract_id != _CONTRACT
            or binding.operation.operation_id != _OPERATION_ID
        ):
            raise PermissionError("MCP executor binding is invalid")
        domain = context.domain_ids[(_CONTRACT, _OPERATION_ID, binding.principal_ref.value)]

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                operation_id != _OPERATION_ID
                or invocation.envelope.target_principal != binding.principal_ref
                or invocation.envelope.contract_id != _CONTRACT
                or invocation.envelope.operation_id != _OPERATION_ID
                or dict(payload) != dict(invocation.envelope.payload)
            ):
                raise PermissionError("MCP executor operation binding changed")
            if set(payload) != {"connection_id", "tool", "arguments"} or any(
                not isinstance(payload[field], str) or not _OPERATION.fullmatch(payload[field])
                for field in ("connection_id", "tool")
            ) or not isinstance(payload["arguments"], dict):
                raise ValueError("MCP executor call is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({MCP_CALL}),
                consumer_pack_id="rumi_tool_mcp_executor_pack",
                include_credentials=False,
            )
            return client.invoke(MCP_CALL, MCP_OPERATION, dict(payload))

        return CapturedHostProviderV4(
            (HostProviderContributionV4(
                contract_id=_CONTRACT,
                contract_version=binding.operation.contract_version,
                operation_id=_OPERATION_ID,
                principal_id=binding.principal_ref.value,
                artifact_digest=binding.artifact.digest,
                implementation_digest=binding.function.implementation_digest,
                domain_id=domain,
                invoke=invoke,
            ),),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = McpExecutorHostFactoryV4()
