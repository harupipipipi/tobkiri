"""Captured read-only chat references through existing signed owner contracts."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.chat_reference import (
    project_references,
    validate_request,
)

FUNCTION_ID = "rumi_conversation_store_pack.chat-reference.resource"
CONTRACT_ID = "tobkiri.resource.chat.reference.v1"
OPERATION_ID = "rumi_conversation_store_pack.chat-reference-read"
CONVERSATION_CONTRACT = "tobkiri.resource.conversation.v1"
CONVERSATION_OPERATION = "rumi_conversation_store_pack.conversation-resource"
PROJECT_CONTRACT = "tobkiri.resource.project.state.v1"
PROJECT_OPERATION = "tobkiri_ui_settings_pack.projects-read"


class ChatReferenceHostFactoryV4:
    """Read existing identities in one captured Profile/caller context."""

    function_id = FUNCTION_ID

    def __init__(self, *, clock: Callable[[], int] | None = None) -> None:
        """Inject a trusted Host clock for deterministic membership snapshots."""
        self._clock = clock or (lambda: int(time.time() * 1000))

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture exact identity without opening either owner store."""
        if not context.profile_id or len(context.provider_bindings) != 1:
            raise PermissionError("chat reference capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("chat reference binding is invalid")
        domain_id = context.domain_ids.get(
            (CONTRACT_ID, OPERATION_ID, binding.principal_ref.value)
        )
        if domain_id is None:
            raise PermissionError("chat reference domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                not invocation.presentation_owner_principal_id
                or not invocation.presentation_owner_session_id
            ):
                raise PermissionError("chat reference caller identity is unavailable")
            if operation_id != OPERATION_ID:
                raise PermissionError("chat reference operation is invalid")
            validate_request(payload, context.profile_id)
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(
                    {CONVERSATION_CONTRACT, PROJECT_CONTRACT}
                ),
                consumer_pack_id="rumi_conversation_store_pack",
                include_credentials=False,
            )
            # contract_client preserves Host-authenticated presentation ancestry;
            # no caller principal, session or filesystem root comes from payload.
            conversations = client.invoke(
                CONVERSATION_CONTRACT,
                CONVERSATION_OPERATION,
                {
                    "profile_id": context.profile_id,
                    "operation": "list",
                },
            )
            invocation.assert_current()
            projects = client.invoke(
                PROJECT_CONTRACT,
                PROJECT_OPERATION,
                {
                    "profile_id": context.profile_id,
                },
            )
            invocation.assert_current()
            if not isinstance(conversations, Mapping) or not isinstance(
                projects, Mapping
            ):
                raise ValueError("chat reference owner response is invalid")
            result = project_references(
                payload,
                conversations,
                projects,
                profile_id=context.profile_id,
                now_ms=self._clock(),
            )
            invocation.assert_current()
            return result

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


HOST_PROVIDER_FACTORY = {FUNCTION_ID: ChatReferenceHostFactoryV4()}
