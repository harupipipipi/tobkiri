"""The Calculator's finite Host operation; no file, network or ambient state."""

from __future__ import annotations

import re
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.host_provider_function_v4 import HostFunction, SingleOperationHostFactoryV4
from ecosystem.rumi_default_tools_pack.domain.tool.calculator import calculate

PACK_ID = "rumi_default_tools_pack"
FUNCTION = f"{PACK_ID}.calculator"
CONTRACT = "tobkiri.service.tool.local.operation.v1"
OPERATION = f"{PACK_ID}.calculator-evaluate"


def _bind(_context: HostProviderCaptureContextV4) -> HostFunction:
    def invoke(
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        if (
            set(payload) != {"tool_id", "tool_call_id", "arguments"}
            or payload["tool_id"] != "calculator"
            or not isinstance(payload["tool_call_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", payload["tool_call_id"]) is None
            or not isinstance(payload["arguments"], dict)
            or set(payload["arguments"]) != {"expression"}
        ):
            raise ValueError("Calculator invocation payload is invalid")
        invocation.assert_current()
        expression = payload["arguments"]["expression"]
        try:
            result = calculate(expression)
        except ValueError as error:
            return {"result": str(error), "is_error": True, "widget": None}
        return {
            "result": f"Calculated: {expression} = {result}",
            "is_error": False,
            "widget": None,
        }

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION,
    contract_id=CONTRACT,
    operation_id=OPERATION,
    bind=_bind,
)
