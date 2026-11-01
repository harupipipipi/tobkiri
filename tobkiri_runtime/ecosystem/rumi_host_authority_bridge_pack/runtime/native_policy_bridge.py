"""Finite Host native policy selection effect; no delegated tool execution."""

from __future__ import annotations

from typing import Any, Mapping, Protocol
from core_runtime.host_provider_function_v4 import SingleOperationHostFactoryV4

FUNCTION = "rumi_host_authority_bridge_pack.host-authority.approval-policy"
CONTRACT = "tobkiri.action.host.approval-policy.v1"
OPERATION = "host.action_approval_policy.select"


class NativePolicySelectionPort(Protocol):
    """Retained Host selection; durable receipt requires committed lease proof."""

    def select_prepared(self, selection_id: str, invocation: Any) -> None:
        """Verify exact retained native selection and stage its bounded effect."""


def _bind(context: Any) -> Any:
    port = getattr(context, "action_approval_policy_port", None)
    if port is None:
        raise PermissionError("native policy selection Host port is unavailable")

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {"selection_id"}:
            raise ValueError("native policy selection payload is invalid")
        identifier = payload["selection_id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or len(identifier) > 160
            or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in identifier)
        ):
            raise ValueError("native policy selection identifier is invalid")
        invocation.assert_current()
        # The finite factory and Broker authenticate exact target/lease/capture.
        # This narrow port verifies retained native request+scope+owner and stages
        # state only. It cannot manufacture future tool approvals or receipts.
        port.select_prepared(identifier, invocation)
        invocation.assert_current()
        return {"status": "selected"}

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION, contract_id=CONTRACT, operation_id=OPERATION, bind=_bind
)
