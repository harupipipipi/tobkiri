"""Select accepted tool preferences from one authenticated executing Saved tree."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tobkiri_protocol.canonical import canonical_digest

GUEST = ("conversation.saved-turn.v1", "saved_complete")
SAVED = ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")


def saved_tool_request_scope(invocation: Any) -> CapturedInvocationScopeV4:
    """Retain ordinary selection; authenticate an incoming recipient separately.

    This returns request data only. Presentation ownership remains validated by
    the existing owner path, independently of the delivery provider caller.
    """
    from ecosystem.rumi_tool_broker_pack.runtime import chat_delivery as delivery
    from tobkiri_host.finite_chat_dispatch import (
        _assert_saved_source_ancestry,
        _current,
    )
    from tobkiri_host.operation_cancellation import (
        _NestedCancellationProof,
        nested_cancellation_proof_for,
    )
    from core_runtime import owned_chat_message_approval_v4 as approval
    from tobkiri_protocol.chat_message_v1 import validate_execute_payload

    scopes: list[Any] = []
    scope = invocation.parent_invocation
    while scope is not None:
        if len(scopes) >= 16 or any(scope is previous for previous in scopes):
            raise PermissionError("saved tool request ancestry is unavailable")
        scopes.append(scope)
        scope = scope.parent
    guests = [
        scope
        for scope in scopes
        if (scope.envelope.contract_id, scope.envelope.operation_id) == GUEST
    ]
    if len(guests) == 1:
        if any(
            (scope.envelope.contract_id, scope.envelope.operation_id)
            == delivery.DELIVERY
            for scope in scopes
        ):
            raise PermissionError("saved tool incoming source ancestry is unavailable")
        for scope in scopes:
            scope.assert_current()
        return guests[0]
    if len(guests) != 2:
        raise PermissionError("saved tool request ancestry is unavailable")
    invocation.assert_current()
    scopes = delivery.captured_delivery_ancestors(
        invocation.parent_invocation,
        limit=16,
        capture=tuple(
            getattr(invocation.envelope.context, key) for key in delivery.CAPTURE_FIELDS
        ),
    )
    routes = [
        (scope.envelope.contract_id, scope.envelope.operation_id) for scope in scopes
    ]
    if routes[:10] != [
        delivery.BROKER,
        GUEST,
        SAVED,
        delivery.DELIVERY,
        delivery.EXECUTE,
        delivery.EFFECT,
        delivery.LOCAL,
        delivery.EXECUTOR,
        delivery.BROKER,
        GUEST,
    ]:
        raise PermissionError("saved tool recipient ancestry changed")
    owner = (
        invocation.presentation_owner_principal_id,
        invocation.presentation_owner_session_id,
    )
    proof = nested_cancellation_proof_for(invocation.envelope, *owner)
    target_broker, guest, saved, incoming, execute = scopes[:5]
    if (
        type(proof) is not _NestedCancellationProof
        or proof._envelope is not saved.envelope
        or guest.parent is not saved
        or saved.parent is not incoming
        or incoming.parent is not execute
    ):
        raise PermissionError("saved tool recipient proof changed")
    with proof._registry._lock:
        for envelope in (guest.envelope, target_broker.envelope, invocation.envelope):
            if not any(
                child.envelope is envelope
                and isinstance(child.future, Future)
                and child.future.running()
                and not child.completed
                for child in proof._children.values()
            ):
                raise PermissionError("saved tool recipient Future unavailable")
    invocation._assert_saved_tool_parent_scopes(tuple(scopes[:4]))
    token = _current.get()
    if (
        token is None
        or not token.active
        or token.root is not scopes[8].envelope
        or token.scope.envelope is not scopes[9].envelope
        or scopes[9].parent is not token.scope.parent
        or token.owner != owner
    ):
        raise PermissionError("saved tool original source changed")
    _assert_saved_source_ancestry(token.scope, token.source_proof, token.owner)
    source_proof = token.source_proof
    if not source_proof.matches_invocation(token.root, *owner):
        raise PermissionError("saved tool source proof changed")
    with source_proof._registry._lock:
        branches = [
            child
            for child in source_proof._children.values()
            if child.envelope is saved.envelope
        ]
        roots = [
            child
            for child in source_proof._children.values()
            if child.envelope is token.root
        ]
        if (
            len(branches) != 1
            or len(roots) != 1
            or not isinstance(roots[0].future, Future)
            or not roots[0].future.running()
            or roots[0].completed
        ):
            raise PermissionError("saved tool source Future unavailable")
        branch = branches[0]
        entered = branch.execution_proof
        if (
            not branch.independent_saved_branch
            or not branch.execution_entered
            or not isinstance(branch.future, Future)
            or not branch.future.running()
            or branch.completed
            or type(entered) is not _NestedCancellationProof
            or entered._envelope is not saved.envelope
            or entered._registry is not proof._registry
            or not entered.matches_invocation(saved.envelope, *owner)
        ):
            raise PermissionError("saved tool entered recipient branch unavailable")
    if (proof is not entered
            and proof._saved_execution_parent is not entered):
        raise PermissionError("saved tool recipient track linkage changed")
    if proof is token.source_proof:
        raise PermissionError("saved tool recipient borrowed source proof")
    bound = validate_execute_payload(**execute.public_payload())
    if incoming.public_payload() != bound:
        raise PermissionError("saved tool approved delivery changed")
    request, plan = bound["request"], bound["plan"]
    approval.assert_chat_message_request_live(request, execute.envelope.context)
    with approval._live_lock:
        record = approval._live_requests.get(request["invocation_key"])
        if (
            record is None
            or record.execute_envelope is not execute.envelope
            or record.plan_digest != canonical_digest(dict(plan))
            or record.execute_guard is None
        ):
            raise PermissionError("saved tool approved execution unavailable")
        execute_guard = record.execute_guard
    execute_guard()
    accepted = guest.public_payload().get("request", {})
    original = saved.public_payload().get("request", {})
    source = token.scope.public_payload().get("request", {})
    target = original.get("conversation_id")
    delivery_id = (
        "message-delivery:"
        + canonical_digest(
            [
                request["profile_id"],
                request["source_turn_id"],
                request["tool_call_id"],
                target,
            ]
        )[7:]
    )
    turn = (
        "delivery:"
        + canonical_digest(
            [
                request["profile_id"],
                delivery_id,
                target,
            ]
        )[7:]
    )
    # The executing Guest is an exact enrolled Host dispatch of the durable
    # owner-projected input. Repeat bind_saved_input's immutable input invariant;
    # only its two owner-produced projection fields may differ.
    original_input, accepted_input = dict(original), dict(accepted)
    if "resolved_chat_references" in original_input:
        raise PermissionError("saved tool source claimed resolved references")
    original_input.pop("task_context", None)
    accepted_input.pop("task_context", None)
    accepted_input.pop("resolved_chat_references", None)
    if (
        canonical_digest(original_input) != canonical_digest(accepted_input)
        or target not in plan["recipient_ids"]
        or target == request["source_conversation_id"]
        or original.get("turn_id") != turn
        or original.get("content") != request["content"]
        or source.get("turn_id") != request["source_turn_id"]
        or source.get("conversation_id") != request["source_conversation_id"]
        or token.root.payload.get("tool_call_id") != request["tool_call_id"]
        or not proof.matches_invocation(saved.envelope, *owner)
        or not proof.matches_invocation(invocation.envelope, *owner)
    ):
        raise PermissionError("saved tool recipient accepted input changed")
    invocation.assert_current()
    return guest
