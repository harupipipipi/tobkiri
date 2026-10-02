"""Selected clock owner; recurrence and job meaning remain in scheduler state."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.captured_wake_v4 import CapturedWakeDeclarationV4
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

FUNCTION = "rumi_scheduler_runtime_pack.scheduler.clock"
CONTRACT = "tobkiri.action.scheduler.clock.v1"
OPERATION = "rumi_scheduler_runtime_pack.scheduler-clock-control"


class SchedulerClockFactoryV4:
    """Capture a finite Host wake port for a separately selected clock caller."""

    function_id = FUNCTION
    wake_declaration = CapturedWakeDeclarationV4(
        "tobkiri.action.scheduler.v1",
        "rumi_scheduler_runtime_pack.scheduler-control",
        {"operation": "tick", "limit": 20},
        60000,
    )

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Bind only this exact public clock operation and selected owner."""
        if (
            not context.profile_id
            or not context.provider_bindings
            or any(
                binding.function.function_id != FUNCTION
                or binding.operation.contract_id != CONTRACT
                or binding.operation.operation_id != OPERATION
                for binding in context.provider_bindings
            )
        ):
            raise PermissionError("clock owner binding is invalid")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != OPERATION or (
                "profile_id" in payload and payload["profile_id"] != context.profile_id
            ):
                raise PermissionError("clock invocation scope is invalid")
            name = payload.get("operation")
            allowed = {"operation", "profile_id"} | ({"duration_ms"} if name == "arm" else set())
            if set(payload) - allowed or name not in {"status", "arm", "disarm"}:
                raise PermissionError("clock request is invalid")
            invocation.assert_current()
            port = context.wake_port
            if port is None:
                if name == "status":
                    return {"armed": False, "available": False, "reason": "wake_driver_unavailable"}
                raise PermissionError("Host wake driver is unavailable")
            if name == "status":
                result = port.status()
            elif name == "disarm":
                result = port.disarm(invocation)
            else:
                duration = payload.get("duration_ms")
                if type(duration) is not int or not 60000 <= duration <= 86400000:
                    raise ValueError("finite clock registration duration is invalid")
                result = port.arm(invocation, duration)
            invocation.assert_current()
            return result

        contributions = []
        for binding in context.provider_bindings:
            key = (CONTRACT, OPERATION, binding.principal_ref.value)
            domain = context.domain_ids.get(key)
            if domain is None:
                raise PermissionError("clock owner domain is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=CONTRACT,
                    contract_version=binding.operation.contract_version,
                    operation_id=OPERATION,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {FUNCTION: SchedulerClockFactoryV4()}
