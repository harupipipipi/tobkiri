"""Dispatch an owned local tool definition through its exact captured operation."""

from __future__ import annotations

import re
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)
from tobkiri_protocol.canonical import canonical_json

PACK_ID = "rumi_tool_local_executor_pack"
CONTRACT = "tobkiri.service.tool.execute.v1"
OPERATION = f"{PACK_ID}.tool-local-execute"
LOCAL_OPERATION = "tobkiri.service.tool.local.operation.v1"
DEFINITION = "tobkiri.resource.tool.definition.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


def _bind(_context: HostProviderCaptureContextV4) -> HostFunction:
    def invoke(
        payload: Mapping[str, Any], invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        if (
            set(payload) != {"tool_id", "tool_call_id", "arguments", "definition"}
            or any(
                not isinstance(payload[key], str)
                or not _IDENTIFIER.fullmatch(payload[key])
                for key in ("tool_id", "tool_call_id")
            )
            or not isinstance(payload["arguments"], dict)
            or not isinstance(payload["definition"], Mapping)
        ):
            raise ValueError("local tool invocation payload is invalid")
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({DEFINITION, LOCAL_OPERATION}),
            consumer_pack_id=PACK_ID,
            include_credentials=False,
        )
        # A definition received in a payload is data, including when it came
        # from the Broker. Confirm the owner still returns the exact definition
        # that was validated; never execute a replacement after a Registry edit.
        resolved = client.invoke(
            DEFINITION, "rumi_tool_registry_pack.tool-definition-resource",
            {"operation": "resolve", "tool_id": payload["tool_id"]},
        )
        if (
            not isinstance(resolved, Mapping)
            or resolved.get("found") is not True
            or resolved.get("resolved_tool_id") != payload["tool_id"]
            or not isinstance(resolved.get("definition"), Mapping)
            or canonical_json(dict(resolved["definition"]))
            != canonical_json(dict(payload["definition"]))
        ):
            raise PermissionError("local tool definition changed or is unavailable")
        execution = resolved["definition"].get("execution")
        if (
            not isinstance(execution, Mapping)
            or execution.get("kind") != "local"
            or execution.get("contract_id") != LOCAL_OPERATION
            or any(
                not isinstance(execution.get(key), str)
                or not _IDENTIFIER.fullmatch(execution[key])
                for key in ("provider_instance_id", "operation")
            )
        ):
            raise ValueError("local tool execution descriptor is invalid")
        candidates = [
            item for item in client.providers(LOCAL_OPERATION)
            if execution["provider_instance_id"] in (
                item.get("provider_instance_id"), item.get("function_id"),
            )
            and execution["operation"] == item.get("operation_id")
            and item.get("backend_id")
            and not item.get("backend_unavailable_reason")
        ]
        if len(candidates) != 1:
            raise PermissionError("selected local tool operation is unavailable")
        invocation.assert_current()
        # The nested Host route supplies the caller, Profile, original deadline,
        # cancellation and authority. Neither the tool's labels nor this adapter
        # can authorize the target or replay it through a legacy executor.
        return client.invoke(
            LOCAL_OPERATION, execution["operation"],
            {key: payload[key] for key in ("tool_id", "tool_call_id", "arguments")},
            provider_instance_id=candidates[0]["provider_instance_id"],
        )

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=f"{PACK_ID}.tool-executor.local",
    contract_id=CONTRACT,
    operation_id=OPERATION,
    bind=_bind,
)
