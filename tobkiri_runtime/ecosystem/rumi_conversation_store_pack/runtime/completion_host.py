"""Captured confirmation of saved append candidates after durable settlement."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_conversation_store_pack.runtime.calendar_completion_source import (
    CALENDAR_FUNCTION,
    assert_calendar_completion_source,
)

FUNCTION_ID = "rumi_conversation_store_pack.conversation-store.completion"
CONTRACT_ID = "tobkiri.action.conversation.completion.v1"
OPERATION_ID = "rumi_conversation_store_pack.conversation-completion"
TURN_CONTRACT = "tobkiri.resource.turn.v1"
TURN_OPERATION = "rumi_turn_runtime_pack.turn-resource"
_CALLERS = frozenset(
    {
        "rumi_turn_runtime_pack.turn-runtime.saved",
        "rumi_turn_runtime_pack.turn-runtime.reconcile",
    }
)


class ConversationCompletionHostFactoryV4:
    """Read terminal evidence through an exact resource-only captured client."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture finite owner and caller identities without accessing storage."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("completion capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("completion binding is invalid")
        domain_id = context.domain_ids.get(
            (CONTRACT_ID, OPERATION_ID, binding.principal_ref.value)
        )
        if not domain_id:
            raise PermissionError("completion domain is unavailable")
        callers = frozenset(
            item.principal_ref.value
            for item in context.catalog_bindings
            if item.function.function_id in _CALLERS
        )
        calendar_callers = frozenset(
            item.principal_ref.value
            for item in context.catalog_bindings
            if item.function.function_id == CALENDAR_FUNCTION
            and item.artifact.pack_id == "rumi_turn_runtime_pack"
            and (
                item.operation.contract_id,
                item.operation.operation_id,
                item.operation.contract_version,
            )
            == ("tobkiri.action.job.adapter.v2", CALENDAR_FUNCTION, "2.0.0")
        )
        store = ConversationStore(
            context.profile_id, user_data_root=context.user_data_root
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            values = {
                key: value for key, value in payload.items() if key != "_session_id"
            }
            if (
                operation_id != OPERATION_ID
                or set(values) != {"profile_id", "operation", "turn_id"}
                or values["profile_id"] != context.profile_id
                or values["operation"] != "confirm"
                or invocation.envelope.context.caller_principal.value
                not in callers | calendar_callers
            ):
                raise PermissionError("completion requires a captured saved owner")
            turn_id = values["turn_id"]
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
                raise ValueError("completion requires an exact turn identity")
            invocation.assert_current()
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({TURN_CONTRACT}),
                consumer_pack_id="rumi_conversation_store_pack",
                include_credentials=False,
            )
            turn = client.invoke(
                TURN_CONTRACT,
                TURN_OPERATION,
                {
                    "profile_id": context.profile_id,
                    "operation": "get",
                    "turn_id": turn_id,
                },
            )
            invocation.assert_current()
            if not isinstance(turn, Mapping) or turn.get("id") != turn_id:
                raise ValueError("completion turn resource is invalid")
            if invocation.envelope.context.caller_principal.value in calendar_callers:
                assert_calendar_completion_source(invocation, turn, context.profile_id)
            return store.confirm_saved_completion(turn)

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


HOST_PROVIDER_FACTORY = {FUNCTION_ID: ConversationCompletionHostFactoryV4()}
