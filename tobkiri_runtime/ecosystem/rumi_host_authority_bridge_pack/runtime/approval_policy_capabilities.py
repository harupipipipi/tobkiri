"""Safe presentation projection of captured Host approval policy availability."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)

FUNCTION = "rumi_host_authority_bridge_pack.host-authority.approval-policy-capabilities"
CONTRACT = "tobkiri.resource.host.approval-policy-capabilities.v1"
OPERATION = "host.action_approval_policy.capabilities"


def _bind(context: Any) -> HostFunction:
    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        invocation.assert_current()
        if set(payload) != {"conversation_id", "workspace_id"}:
            raise ValueError("approval policy applicability is invalid")
        if context.action_approval_policy_capabilities_port is None:
            raise PermissionError("approval policy capabilities are unavailable")
        return context.action_approval_policy_capabilities_port(invocation, payload)

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION,
    contract_id=CONTRACT,
    operation_id=OPERATION,
    bind=_bind,
)
