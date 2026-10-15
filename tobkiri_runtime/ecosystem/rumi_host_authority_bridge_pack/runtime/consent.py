"""Finite Host consent operation: no file, terminal or credential capability."""

from __future__ import annotations

import re
from typing import Any, Mapping
from core_runtime.host_provider_function_v4 import SingleOperationHostFactoryV4

FUNCTION = "rumi_host_authority_bridge_pack.host-authority.host-consent"
CONTRACT = "tobkiri.action.host.consent.v1"
OPERATION = "host_consent.admit"


def _bind(_context: Any) -> Any:
    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        if set(payload) != {
            "retained_operation_digest",
            "operation_digest",
            "capture_digest",
            "turn_id",
            "tool_call_id",
        }:
            raise ValueError("Host consent payload is invalid")
        for key in ("retained_operation_digest", "operation_digest", "capture_digest"):
            if (
                not isinstance(payload[key], str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", payload[key]) is None
            ):
                raise ValueError("Host consent digest is invalid")
        for key in ("turn_id", "tool_call_id"):
            if not isinstance(payload[key], str) or not payload[key] or len(payload[key]) > 256:
                raise ValueError("Host consent identity is invalid")
        invocation.assert_current()
        # The Broker consumed the exact native one-use consent Grant before
        # entering this operation. This echo alone confers no file authority.
        result = {"admitted_operation_digest": payload["retained_operation_digest"]}
        invocation.assert_current()
        return result

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION, contract_id=CONTRACT, operation_id=OPERATION, bind=_bind
)
