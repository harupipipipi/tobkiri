"""Exact captured lifecycle operations using the ordinary restricted Broker."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.tobkiri_conversation_lifecycle_pack.runtime.lifecycle import (
    ARCHIVE_ACTION_ID,
    CONVERSATION,
    CONVERSATION_ACTION,
    ConversationLifecycle,
    SCHEDULE,
    SCHEDULE_ACTION,
)

PACK_ID = "tobkiri_conversation_lifecycle_pack"
STATUS_CONTRACT = "tobkiri.resource.conversation.lifecycle.v1"
MANAGE_CONTRACT = "tobkiri.action.conversation.lifecycle.v1"
JOB_CONTRACT = "tobkiri.action.job.adapter.v2"
OPERATIONS = {
    f"{PACK_ID}.status": (STATUS_CONTRACT, f"{PACK_ID}.lifecycle-status"),
    f"{PACK_ID}.manage": (MANAGE_CONTRACT, f"{PACK_ID}.lifecycle-manage"),
    f"{PACK_ID}.archive-job": (JOB_CONTRACT, f"{PACK_ID}.archive-job-adapter"),
}


class LifecycleHostFactoryV4:
    """Bind one exact optional Pack Function to a captured Profile."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture immutable identity without reading or scheduling user data."""
        if not context.profile_id or len(context.provider_bindings) != 1:
            raise PermissionError("lifecycle capture is incomplete")
        binding = context.provider_bindings[0]
        contract_id, operation_id = OPERATIONS[self.function_id]
        contract_version = "2.0.0" if contract_id == JOB_CONTRACT else "1.0.0"
        operation = binding.operation
        if (
            binding.function.function_id != self.function_id
            or operation.contract_id != contract_id
            or operation.operation_id != operation_id
            or operation.contract_version != contract_version
        ):
            raise PermissionError("lifecycle capture binding is invalid")
        domain_id = context.domain_ids.get(
            (
                contract_id,
                operation_id,
                binding.principal_ref.value,
            )
        )
        if not domain_id:
            raise PermissionError("lifecycle capture domain is unavailable")

        def invoke(
            requested: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if (
                requested != operation_id
                or payload.get("profile_id") != context.profile_id
            ):
                raise PermissionError("lifecycle invocation binding is invalid")
            invocation.assert_current()
            payload_fields = set(payload) - {"_session_id"}
            if contract_id == JOB_CONTRACT and payload.get("operation") == "describe":
                if payload_fields != {"profile_id", "operation"}:
                    raise ValueError("lifecycle describe fields are invalid")
                return {"action_ids": [ARCHIVE_ACTION_ID]}
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(
                    {
                        CONVERSATION,
                        CONVERSATION_ACTION,
                        SCHEDULE,
                        SCHEDULE_ACTION,
                    }
                ),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            runtime = ConversationLifecycle(client, context.profile_id)
            action = payload.get("operation")
            if contract_id == STATUS_CONTRACT:
                if (
                    payload_fields != {"profile_id", "operation", "conversation_id"}
                    or action != "get"
                ):
                    raise ValueError("lifecycle status fields are invalid")
                return runtime.status(_identity(payload))
            if contract_id == MANAGE_CONTRACT:
                if (
                    payload_fields
                    != {"profile_id", "operation", "conversation_id", "mode"}
                    or action != "configure"
                ):
                    raise ValueError("lifecycle mode fields are invalid")
                return runtime.configure(_identity(payload), str(payload["mode"]))
            allowed = {
                "profile_id",
                "operation",
                "action_id",
                "payload",
                "idempotency_key",
                "schedule_id",
                "lease_id",
            }
            if (
                payload_fields != allowed
                or payload.get("action_id") != ARCHIVE_ACTION_ID
            ):
                raise PermissionError("lifecycle job fields are invalid")
            if action == "status":
                return {"status": "unknown"}
            if action == "cancel":
                # There is no background action left alive after this synchronous call.
                return {"status": "cancelled"}
            if action != "dispatch" or payload.get("payload") != {}:
                raise ValueError("lifecycle job operation is invalid")
            for key in ("idempotency_key", "schedule_id", "lease_id"):
                if not isinstance(payload[key], str) or not payload[key]:
                    raise ValueError("lifecycle job identity is required")
            return runtime.tick()

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=contract_id,
                    contract_version=contract_version,
                    operation_id=operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


def _identity(payload: Mapping[str, Any]) -> str:
    value = payload.get("conversation_id")
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("conversation identity is required")
    return value


HOST_PROVIDER_FACTORY = {
    function_id: LifecycleHostFactoryV4(function_id) for function_id in OPERATIONS
}
