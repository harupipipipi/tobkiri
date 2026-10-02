"""Exact captured side conversation providers using a credential-free client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from .side_chat import (
    CONVERSATION,
    MANAGE,
    TURN,
    EVENTS,
    SAVED,
    STOP,
    RECONCILE,
    SideChat,
)

PACK_ID = "tobkiri_side_chat_pack"
BINDINGS = {
    "resource": ("tobkiri.resource.side-chat.v1", "side-chat-resource"),
    "manage": ("tobkiri.action.side-chat.v1", "side-chat-manage"),
    "turn": ("tobkiri.service.side-chat.turn.v1", "side-chat-turn"),
}
_FIELDS = {
    "resource": {
        "get": {"parent_conversation_id"},
        "events": {"conversation_id", "turn_id"},
    },
    "manage": {"ensure": {"parent_conversation_id", "expected_parent_revision"}},
    "turn": {
        "send": {
            "conversation_id",
            "turn_id",
            "expected_parent_revision",
            "expected_child_revision",
            "content",
        },
        "stop": {"conversation_id", "turn_id"},
        "reconcile": {"conversation_id", "turn_id"},
    },
}


class SideChatHostFactoryV4:
    """Bind exact Function, Profile and execution domain before dispatch."""

    def __init__(self, kind: str) -> None:
        """Choose a statically declared public provider role."""
        self.kind = kind
        self.contract_id, operation = BINDINGS[kind]
        self.operation_id = f"{PACK_ID}.{operation}"
        self.function_id = f"{PACK_ID}.{kind}"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture without filesystem IO or ambient provider lookups."""
        if not context.profile_id or len(context.provider_bindings) != 1:
            raise PermissionError("side conversation capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != self.function_id
            or operation.contract_id != self.contract_id
            or operation.operation_id != self.operation_id
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("side conversation binding is invalid")
        domain_id = context.domain_ids.get(
            (self.contract_id, self.operation_id, binding.principal_ref.value)
        )
        if not domain_id:
            raise PermissionError("side conversation domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                operation_id != self.operation_id
                or payload.get("profile_id") != context.profile_id
            ):
                raise PermissionError("side conversation Profile differs")
            action = payload.get("operation")
            if not isinstance(action, str) or action not in _FIELDS[self.kind]:
                raise PermissionError("side conversation operation is invalid")
            fields = {"operation", "profile_id"} | _FIELDS[self.kind][action]
            if not fields <= set(payload) or set(payload) - fields - {"_session_id"}:
                raise PermissionError("side conversation request fields are invalid")
            for key in fields - {"operation", "profile_id", "content"}:
                value = payload[key]
                if key.startswith("expected_"):
                    if type(value) is not int or value < 1:
                        raise ValueError("side conversation requires an exact revision")
                elif not isinstance(value, str) or not value or len(value) > 256:
                    raise ValueError("side conversation identity is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(
                    target[0]
                    for target in {
                        "resource": (CONVERSATION, TURN, EVENTS),
                        "manage": (CONVERSATION, MANAGE, TURN),
                        "turn": (CONVERSATION, TURN, SAVED, STOP, RECONCILE),
                    }[self.kind]
                ),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            service = SideChat(client, context.profile_id)
            if action == "get":
                return service.get(payload["parent_conversation_id"])
            if action == "ensure":
                return service.ensure(
                    payload["parent_conversation_id"],
                    payload["expected_parent_revision"],
                )
            if action == "send":
                return service.send(payload)
            return service.turn(action, payload["conversation_id"], payload["turn_id"])

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=self.contract_id,
                    contract_version="1.0.0",
                    operation_id=self.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    f"{PACK_ID}.{kind}": SideChatHostFactoryV4(kind) for kind in BINDINGS
}
