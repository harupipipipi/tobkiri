"""Bind browser execution to the exact, authorized Host Provider envelope."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)

from ecosystem.rumi_browser_host_service_pack.runtime.runner import BrowserHostRunner
from ecosystem.rumi_browser_host_service_pack.runtime.browser_tool import (
    HOST_PROVIDER_FACTORY as BROWSER_TOOL_FACTORY,
)


PACK_ID = "rumi_browser_host_service_pack"
_FORBIDDEN_ARGUMENTS = frozenset({
    "approved", "approval_token", "authority_token", "viewer_host_approved",
    "yolo_mode", "_host_context", "_contract_consumer_pack_id",
    "_contract_consumer_function_id", "_source_function_id",
    "_rumi_contract_operation", "artifact_root", "_session_id",
})


def _bind_browser(
    context: HostProviderCaptureContextV4,
    *,
    access: str,
) -> HostFunction:
    """Capture owner state and a finite action set under the selected artifact."""
    from ecosystem.rumi_browser_host_service_pack.runtime import service

    operations = (
        service.OBSERVE_OPERATIONS if access == "observe" else service.CONTROL_OPERATIONS
    )
    host_functions = dict(service._HOST_FUNCTIONS)
    if context.user_data_root is None:
        raise PermissionError("browser owner storage is unavailable")
    runner = BrowserHostRunner(user_data_root=context.user_data_root)
    profile_namespace = hashlib.sha256(context.profile_id.encode("utf-8")).hexdigest()
    artifact_root = context.state_root / PACK_ID / "artifacts" / profile_namespace

    def invoke(
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        invocation.assert_current()
        if (
            set(payload) != {"operation", "arguments"}
            or not isinstance(payload["operation"], str)
            or not isinstance(payload["arguments"], Mapping)
        ):
            raise ValueError("browser invocation payload is invalid")
        operation = payload["operation"]
        arguments = dict(payload["arguments"])
        if operation not in operations:
            raise PermissionError("browser operation is outside the authorized contract")
        if _FORBIDDEN_ARGUMENTS.intersection(arguments):
            raise PermissionError("client browser authority material is forbidden")
        action = host_functions.get(operation)
        if not isinstance(action, str) or not action.startswith("browser."):
            raise PermissionError("browser operation has no owned execution route")
        if operation == "browser.network.capture":
            duration_ms = arguments.get("duration_ms", 2000)
            if (
                type(duration_ms) is not int
                or duration_ms < 0
                or duration_ms / 1000 >= (
                    invocation.envelope.deadline_monotonic - time.monotonic()
                )
            ):
                raise ValueError("network capture exceeds the authorized request budget")
        # SingleOperationHostFactoryV4 checks the captured principal, domain,
        # activation, Plan, epoch and exact payload before this handler runs.
        # Authority and durable audit are checked by the Broker, never by a
        # flag supplied in browser arguments.
        result = runner.run(
            action,
            {**arguments, "_rumi_contract_operation": operation},
            viewer_host_approved=True,
            artifact_root=artifact_root,
        )
        if not isinstance(result, Mapping) or result.get("type") == "host_intent":
            raise RuntimeError("browser execution returned an invalid result")
        return dict(result)

    return invoke


def _bind_observe(context: HostProviderCaptureContextV4) -> HostFunction:
    return _bind_browser(context, access="observe")


def _bind_control(context: HostProviderCaptureContextV4) -> HostFunction:
    return _bind_browser(context, access="control")


BROWSER_HOST_PROVIDER_FACTORIES = {
    f"{PACK_ID}.browser-host.observe": SingleOperationHostFactoryV4(
        function_id=f"{PACK_ID}.browser-host.observe",
        contract_id="tobkiri.resource.browser.host.v1",
        operation_id=f"{PACK_ID}.browser-host-observe",
        bind=_bind_observe,
    ),
    f"{PACK_ID}.browser-host.control": SingleOperationHostFactoryV4(
        function_id=f"{PACK_ID}.browser-host.control",
        contract_id="tobkiri.action.browser.host.v1",
        operation_id=f"{PACK_ID}.browser-host-control",
        bind=_bind_control,
    ),
    BROWSER_TOOL_FACTORY.function_id: BROWSER_TOOL_FACTORY,
}
