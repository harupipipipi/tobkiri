"""Recognize one live, source-bound synchronous approved delivery child."""

from __future__ import annotations

from typing import Any

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tobkiri_protocol.canonical import canonical_digest

BROKER = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
DELIVERY = (
    "tobkiri.action.chat.message.delivery.v1",
    "rumi_turn_runtime_pack.chat-message-deliver",
)
EXECUTE = (
    "tobkiri.service.chat.message.send.v1",
    "rumi_default_tools_pack.chat-message-send",
)
SAVED = ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
EFFECT = ("tobkiri.service.interactive-effect.v1", "interactive_effect.manage")
LOCAL = (
    "tobkiri.service.tool.local.operation.v1",
    "rumi_default_tools_pack.chat-message-operation",
)
EXECUTOR = (
    "tobkiri.service.tool.execute.v1",
    "rumi_tool_local_executor_pack.tool-local-execute",
)
MAX_ANCESTORS = 10
CAPTURE_FIELDS = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
)


def captured_delivery_ancestors(
    scope: CapturedInvocationScopeV4 | None, *, limit: int,
    capture: tuple[Any, ...] | None = None,
) -> list[CapturedInvocationScopeV4]:
    """Validate a finite delivery prefix and an authenticated Calendar suffix.

    The Calendar suffix is recognized only through the exact current finite
    source token and its registered selected occurrence proof. It is never
    recognized from a payload origin or a caller-supplied owner.
    """
    from tobkiri_host.finite_chat_dispatch import (
        _current, _assert_saved_source_ancestry,
    )
    scopes: list[CapturedInvocationScopeV4] = []
    while scope is not None:
        if (not isinstance(scope, CapturedInvocationScopeV4)
                or len(scopes) >= limit or any(scope is prior for prior in scopes)):
            raise PermissionError("delivery ancestry is invalid")
        scope.assert_current()
        if capture is not None and tuple(
            getattr(scope.envelope.context, key) for key in CAPTURE_FIELDS
        ) != capture:
            raise PermissionError("delivery capture changed")
        scopes.append(scope)
        token = _current.get()
        if (token is not None and token.active and token.root is not None
                and token.scope.parent is not None
                and token.scope.parent.parent is not None
                and scope.envelope is token.scope.parent.envelope):
            parent, calendar = _assert_saved_source_ancestry(
                token.scope, token.source_proof, token.owner,
            )
            if (not calendar or parent is not scope
                    or not any(item.envelope is token.root for item in scopes)
                    or len(scopes) < 2
                    or scopes[-2].envelope is not token.scope.envelope
                    or scopes[-2].parent is not parent):
                raise PermissionError("Calendar delivery source changed")
            break
        scope = scope.parent
    return scopes


def _delivery_owner(scopes: list[CapturedInvocationScopeV4]) -> tuple[str, str]:
    """Use the selected source proof for Calendar; ordinary roots stay exact."""
    root = scopes[-1]
    from tobkiri_host.finite_chat_dispatch import _current

    token = _current.get()
    retained_calendar_parent = (
        token is not None and token.active and token.scope.parent is not None
        and token.scope.parent.parent is not None
        and root.envelope is token.scope.parent.envelope
    )
    if root.parent is not None or retained_calendar_parent:
        from tobkiri_host.finite_chat_dispatch import (
            _current, _assert_saved_source_ancestry,
        )
        token = _current.get()
        if token is None or not token.active:
            raise PermissionError("Calendar delivery owner is unavailable")
        parent, calendar = _assert_saved_source_ancestry(
            token.scope, token.source_proof, token.owner,
        )
        if not calendar or parent is not root:
            raise PermissionError("Calendar delivery owner changed")
        return token.owner
    context = root.envelope.context
    return context.caller_principal.value, context.caller_session_id


def assert_delivery_child(invocation: Any, held: Any) -> None:
    """Require trusted ancestry to the exact Broker invocation holding the gate.

    This recognizes ancestry only; normal Host dispatch still performs every
    contract, authority, cancellation and admission check.
    """
    from core_runtime.owned_chat_message_approval_v4 import (
        assert_chat_message_request_live,
    )
    from tobkiri_protocol.chat_message_v1 import validate_execute_payload

    invocation.assert_current()
    held.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != BROKER:
        raise PermissionError("delivery child Broker changed")
    capture = tuple(getattr(envelope.context, key) for key in CAPTURE_FIELDS)
    scopes = captured_delivery_ancestors(
        invocation.parent_invocation, limit=MAX_ANCESTORS, capture=capture,
    )
    holder = next(
        (i for i, item in enumerate(scopes) if item.envelope is held.envelope), None
    )
    if holder is None:
        raise PermissionError("delivery child has no active source Broker")
    operations = [
        (item.envelope.contract_id, item.envelope.operation_id) for item in scopes
    ]
    # Only one incoming turn may descend from the source. No further Broker or
    # delivery recursion is admitted, even if clients copy identical payloads.
    if operations.count(BROKER) != 1 or operations.count(DELIVERY) != 1:
        raise PermissionError("delivery child recursion is unavailable")
    saved = next(
        (i for i, operation in enumerate(operations) if operation == SAVED), None
    )
    if (
        saved is None
        or operations[saved + 1 : saved + 3] != [DELIVERY, EXECUTE]
        or saved + 3 > holder
    ):
        raise PermissionError("delivery child direct saved ancestry changed")
    if operations[saved + 3 : holder] != [EFFECT, LOCAL, EXECUTOR]:
        raise PermissionError("delivery child source execution ancestry changed")
    delivery, execute = scopes[saved + 1 : saved + 3]
    bound = validate_execute_payload(**execute.public_payload())
    if delivery.public_payload() != bound:
        raise PermissionError("delivery child approved snapshot changed")
    assert_chat_message_request_live(bound["request"], execute.envelope.context)
    request, plan = bound["request"], bound["plan"]
    initial = scopes[saved].public_payload().get("request", {})
    target = initial.get("conversation_id")
    target_turn = (
        "delivery:"
        + canonical_digest(
            [
                request["profile_id"],
                "message-delivery:"
                + canonical_digest(
                    [
                        request["profile_id"],
                        request["source_turn_id"],
                        request["tool_call_id"],
                        target,
                    ]
                )[7:],
                target,
            ]
        )[7:]
    )
    if (
        target not in plan["recipient_ids"]
        or target == request["source_conversation_id"]
        or initial.get("content") != request["content"]
        or initial.get("turn_id") != target_turn
        or request["profile_id"] != envelope.context.profile_id
        or held.envelope.payload.get("tool_id") != "chat_send_message"
        or held.envelope.payload.get("tool_call_id") != request["tool_call_id"]
    ):
        raise PermissionError("delivery child source or recipient changed")
    source_saved = next(
        (
            item
            for item in scopes[holder + 1 :]
            if (item.envelope.contract_id, item.envelope.operation_id)
            == ("conversation.saved-turn.v1", "saved_complete")
        ),
        None,
    )
    source = source_saved.public_payload().get("request", {}) if source_saved else {}
    if (
        source.get("conversation_id") != request["source_conversation_id"]
        or source.get("turn_id") != request["source_turn_id"]
    ):
        raise PermissionError("delivery child saved source changed")
    source_owner = _delivery_owner(scopes)
    for owner in (invocation, held):
        if (
            owner.presentation_owner_principal_id != source_owner[0]
            or owner.presentation_owner_session_id != source_owner[1]
        ):
            raise PermissionError("delivery child owner changed")
    invocation.assert_current()
    held.assert_current()


def assert_delivery_effect_child(invocation: Any, held_scope: CapturedInvocationScopeV4) -> None:
    """Admit only one file effect beneath the exact live source chat resume."""
    invocation.assert_current()
    held_scope.assert_current()
    scopes = captured_delivery_ancestors(invocation.parent_invocation, limit=13)
    file_local = ("tobkiri.service.tool.local.operation.v1", "rumi_default_tools_pack.file-create-operation")
    operations = [(scope.envelope.contract_id, scope.envelope.operation_id) for scope in scopes]
    if operations[:3] != [file_local, EXECUTOR, BROKER]:
        raise PermissionError("delivery effect child is unavailable")
    current = next((scope for scope in scopes if scope.envelope is held_scope.envelope), None)
    if current is None or operations.count(EFFECT) != 1:
        raise PermissionError("delivery effect source resume changed")
    source_broker = next((scope for scope in scopes[3:] if (
        scope.envelope.contract_id, scope.envelope.operation_id) == BROKER), None)
    if source_broker is None:
        raise PermissionError("delivery effect source broker is unavailable")

    class ScopeView:
        def __init__(self, scope: CapturedInvocationScopeV4) -> None:
            self.envelope = scope.envelope
            self.parent_invocation = scope.parent
            self.assert_current = scope.assert_current
            self.presentation_owner_principal_id = invocation.presentation_owner_principal_id
            self.presentation_owner_session_id = invocation.presentation_owner_session_id

    assert_delivery_child(ScopeView(scopes[2]), ScopeView(source_broker))


def _delivery_saved_source(
    scopes: list[CapturedInvocationScopeV4],
    operations: list[tuple[str, str]],
) -> CapturedInvocationScopeV4:
    """Select the existing Saved owner or the exact live Calendar source join."""
    if operations.count(SAVED) == 1:
        return scopes[operations.index(SAVED)]
    else:
        from tobkiri_host.finite_chat_dispatch import (
            _current,
            _assert_saved_source_ancestry,
        )

        token = _current.get()
        if token is None or not token.active or operations.count(SAVED) != 0:
            raise PermissionError("independent saved branch source is unavailable")
        parent, calendar = _assert_saved_source_ancestry(
            token.scope,
            token.source_proof,
            token.owner,
        )
        if (
            not calendar
            or len(scopes) != 7
            or scopes[4].envelope is not token.root
            or scopes[5].envelope is not token.scope.envelope
            or scopes[5].parent is not parent
            or scopes[6] is not parent
        ):
            raise PermissionError("independent Calendar saved source changed")
        from concurrent.futures import Future

        proof = token.source_proof
        if not proof.matches_invocation(token.root, *token.owner):
            raise PermissionError("independent Calendar source broker unavailable")
        with proof._registry._lock:
            if not any(
                child.envelope is token.root
                and isinstance(child.future, Future)
                and child.future.running()
                and not child.completed
                for child in proof._children.values()
            ):
                raise PermissionError(
                    "independent Calendar source broker Future unavailable"
                )
        return token.scope


def assert_delivery_saved_branch(invocation: Any, payload: Any) -> None:
    """Authenticate a direct approved delivery before minting its saved branch."""
    from core_runtime.owned_chat_message_approval_v4 import (
        assert_chat_message_request_live,
    )
    from tobkiri_protocol.chat_message_v1 import validate_execute_payload

    invocation.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != DELIVERY:
        raise PermissionError("independent saved branch has no delivery owner")
    capture = tuple(getattr(envelope.context, key) for key in CAPTURE_FIELDS)
    scopes = captured_delivery_ancestors(
        invocation.parent_invocation, limit=MAX_ANCESTORS, capture=capture,
    )
    operations = [(item.envelope.contract_id, item.envelope.operation_id) for item in scopes]
    if (
        operations[:5] != [EXECUTE, EFFECT, LOCAL, EXECUTOR, BROKER]
        or operations.count(BROKER) != 1
        or DELIVERY in operations
    ):
        raise PermissionError("independent saved branch execution ancestry changed")
    execute = scopes[0]
    bound = validate_execute_payload(**execute.public_payload())
    if CapturedInvocationScopeV4(envelope, invocation.assert_current, invocation.parent_invocation).public_payload() != bound:
        raise PermissionError("independent saved branch approved plan changed")
    request, plan = bound["request"], bound["plan"]
    assert_chat_message_request_live(request, execute.envelope.context)
    initial = payload.get("request", {})
    target = initial.get("conversation_id")
    delivery_id = "message-delivery:" + canonical_digest([
        request["profile_id"], request["source_turn_id"], request["tool_call_id"], target,
    ])[7:]
    target_turn = "delivery:" + canonical_digest([
        request["profile_id"], delivery_id, target,
    ])[7:]
    source_scope = _delivery_saved_source(scopes, operations)
    source = source_scope.public_payload().get("request", {})
    holder = scopes[4].envelope
    source_owner = _delivery_owner(scopes)
    if (
        target not in plan["recipient_ids"]
        or target == request["source_conversation_id"]
        or initial.get("content") != request["content"]
        or initial.get("turn_id") != target_turn
        or request["profile_id"] != envelope.context.profile_id
        or source.get("conversation_id") != request["source_conversation_id"]
        or source.get("turn_id") != request["source_turn_id"]
        or holder.payload.get("tool_id") != "chat_send_message"
        or holder.payload.get("tool_call_id") != request["tool_call_id"]
        or invocation.presentation_owner_principal_id != source_owner[0]
        or invocation.presentation_owner_session_id != source_owner[1]
    ):
        raise PermissionError("independent saved branch source or target changed")
    invocation.assert_current()
