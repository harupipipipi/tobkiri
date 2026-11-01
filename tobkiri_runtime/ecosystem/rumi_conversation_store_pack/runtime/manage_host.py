"""Profile-captured conversation mutations for the existing action contract.

This hook is not an HTTP endpoint or an approval path. Production registration
must bind it through the normal artifact, Authority and Broker transaction.
"""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

FUNCTION_ID = "rumi_conversation_store_pack.conversation-store.manage"
CONTRACT_ID = "tobkiri.action.conversation.manage.v1"
OPERATION_ID = "rumi_conversation_store_pack.conversation-manage"


class ConversationManageHostFactoryV4:
    """Keep action calls in one captured Profile and exact revision scope."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture bindings without creating or modifying durable state."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("conversation action capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("conversation action binding is invalid")
        domain_id = context.domain_ids.get((CONTRACT_ID, OPERATION_ID, binding.principal_ref.value))
        if domain_id is None:
            raise PermissionError("conversation action domain is unavailable")
        store = ConversationStore(context.profile_id, user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation  # Authority and Broker authorize before invoking this hook.
            if operation_id != OPERATION_ID or payload.get("profile_id") != context.profile_id:
                raise PermissionError("conversation action request is invalid")
            action = payload.get("operation")
            fields = {
                "create": {"conversation", "expected_revision"},
                "update": {"conversation_id", "patch", "expected_conversation_revision"},
                "delete": {"conversation_id", "expected_conversation_revision"},
            }
            if not isinstance(action, str) or action not in fields:
                raise PermissionError("conversation action is not permitted")
            required = {"profile_id", "operation"} | fields[action]
            if not required <= set(payload) or set(payload) - required - {"_session_id"}:
                raise PermissionError("conversation action fields are invalid")
            revision_key = (
                "expected_revision" if action == "create" else "expected_conversation_revision"
            )
            revision = payload[revision_key]
            if type(revision) is not int or revision < (0 if action == "create" else 1):
                raise ValueError("conversation revision must be an exact nonnegative integer")
            if action == "create":
                record = payload["conversation"]
                if not isinstance(record, Mapping):
                    raise ValueError("conversation must be an object")
                return store.create(record, expected_revision=revision)
            conversation_id = payload["conversation_id"]
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ValueError("conversation id is required")
            if action == "update":
                patch = payload["patch"]
                if not isinstance(patch, Mapping):
                    raise ValueError("conversation patch must be an object")
                return store.update(
                    conversation_id,
                    patch,
                    expected_conversation_revision=revision,
                )
            return store.delete(conversation_id, expected_conversation_revision=revision)

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


HOST_PROVIDER_FACTORY = {FUNCTION_ID: ConversationManageHostFactoryV4()}
