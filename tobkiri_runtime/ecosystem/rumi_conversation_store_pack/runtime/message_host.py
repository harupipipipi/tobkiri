"""Captured message actions for the conversation owner's existing contract."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

FUNCTION_ID = "rumi_conversation_store_pack.conversation-store.message-manage"
CONTRACT_ID = "tobkiri.action.message.manage.v1"
OPERATION_ID = "rumi_conversation_store_pack.message-manage"
_FIELDS = {
    "append": {"message"},
    "update": {"message_id", "patch"},
    "delete": {"message_id"},
    "replace": {"messages"},
}


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("an explicit message or conversation identity is required")
    return value


def _message(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("message must be an object")
    _identifier(value.get("id"))
    return value


class MessageManageHostFactoryV4:
    """Bind message writes to one captured Profile, root and owner revision."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture the exact action binding without accessing durable state."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("message action capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("message action binding is invalid")
        domain_id = context.domain_ids.get((CONTRACT_ID, OPERATION_ID, binding.principal_ref.value))
        if domain_id is None:
            raise PermissionError("message action domain is unavailable")
        store = ConversationStore(context.profile_id, user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation  # The Host Authority/Broker authorize before dispatch.
            if operation_id != OPERATION_ID or payload.get("profile_id") != context.profile_id:
                raise PermissionError("message action request is invalid")
            action = payload.get("operation")
            if not isinstance(action, str) or action not in _FIELDS:
                raise PermissionError("message action is not permitted")
            required = {
                "profile_id",
                "operation",
                "conversation_id",
                "expected_conversation_revision",
            } | _FIELDS[action]
            if not required <= set(payload) or set(payload) - required - {"_session_id"}:
                raise PermissionError("message action fields are invalid")
            revision = payload["expected_conversation_revision"]
            if type(revision) is not int or revision < 1:
                raise ValueError("message action requires an exact positive revision")
            conversation_id = _identifier(payload["conversation_id"])
            if action == "append":
                return store.append_message(
                    conversation_id,
                    _message(payload["message"]),
                    expected_conversation_revision=revision,
                )
            if action == "replace":
                messages = payload["messages"]
                if not isinstance(messages, list):
                    raise ValueError("messages must be an ordered list")
                return store.replace_messages(
                    conversation_id,
                    [_message(item) for item in messages],
                    expected_conversation_revision=revision,
                )
            message_id = _identifier(payload["message_id"])
            patch = payload.get("patch")
            if action == "update" and (
                not isinstance(patch, Mapping)
                or not patch
                or set(patch)
                - {
                    "content",
                    "parts",
                    "metadata",
                    "status",
                    "sequence_number",
                    "raw_text",
                    "finish_reason",
                    "usage",
                    "widget",
                    "events",
                    "tool_logs",
                    "parent_id",
                }
            ):
                raise ValueError("message patch fields are invalid")
            return store.mutate_message(
                conversation_id,
                message_id,
                expected_conversation_revision=revision,
                patch=patch,
                delete=action == "delete",
            )

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


HOST_PROVIDER_FACTORY = {FUNCTION_ID: MessageManageHostFactoryV4()}
