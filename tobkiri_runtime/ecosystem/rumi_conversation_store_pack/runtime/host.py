"""Captured, read-only Host entrypoint for the conversation owner store."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore


FUNCTION_ID = "rumi_conversation_store_pack.conversation-store.resource"
CONTRACT_ID = "tobkiri.resource.conversation.v1"
OPERATION_ID = "rumi_conversation_store_pack.conversation-resource"


class ConversationReadHostFactoryV4:
    """Bind conversation reads to one Host-selected root and Profile."""

    function_id = FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Reject unrecognized bindings before constructing a local reader."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("conversation read capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
        ):
            raise PermissionError("conversation read binding is invalid")
        key = (CONTRACT_ID, OPERATION_ID, binding.principal_ref.value)
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("conversation read domain is unavailable")
        store = ConversationStore(
            context.profile_id,
            user_data_root=context.user_data_root,
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            # Execution/admission remain with the exact Host backend and Broker.
            # This hook neither accepts an ambient path nor chooses a Profile.
            del invocation
            if (
                operation_id != OPERATION_ID
                or payload.get("profile_id") != context.profile_id
                or set(payload)
                - {
                    "profile_id",
                    "operation",
                    "conversation_id",
                    "_session_id",
                }
            ):
                raise PermissionError("conversation read request is invalid")
            action = payload.get("operation")
            if action == "list" and "conversation_id" not in payload:
                return store.snapshot()
            if action == "get":
                conversation_id = payload.get("conversation_id")
                if not isinstance(conversation_id, str) or not conversation_id:
                    raise ValueError("conversation id is required")
                value = store.get(conversation_id)
                if value is None:
                    raise KeyError("conversation is unavailable")
                return {"conversation": value}
            raise PermissionError("conversation read operation is not permitted")

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=operation.contract_version,
                    operation_id=OPERATION_ID,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {FUNCTION_ID: ConversationReadHostFactoryV4()}
