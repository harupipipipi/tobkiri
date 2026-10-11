"""Scheduling-unit proof uses actual registry membership, not production capture."""

from dataclasses import replace
from concurrent.futures import Future

import pytest

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _envelope, _child_envelope, _binding
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)

from tobkiri_host import finite_chat_dispatch as helper


def setup():
    registry = OwnedCancellationHandles()
    saved = replace(
        _envelope(), contract_id=helper.SAVED[0], operation_id=helper.SAVED[1],
        payload={"request": {"conversation_id": "source", "turn_id": "turn"}},
    )
    owner = (saved.context.caller_principal.value, saved.context.caller_session_id)
    guest = replace(
        _child_envelope(saved),
        contract_id=helper.GUEST[0],
        operation_id=helper.GUEST[1],
        payload=saved.payload,
    )
    saved_scope = CapturedInvocationScopeV4(saved, lambda: None)
    guest_scope = CapturedInvocationScopeV4(guest, lambda: None, saved_scope)
    payload = dict(
        tool_id="chat_send_message",
        tool_call_id="call",
        arguments=dict(
            target_kind="chat",
            target_id="target",
            content="hello",
            delivery="interrupt",
        ),
    )
    root = replace(
        _child_envelope(guest),
        contract_id=helper.BROKER[0],
        operation_id=helper.BROKER[1],
        payload=payload,
    )
    return registry, saved, owner, guest, guest_scope, payload, root


def test_actual_parent_membership_and_root_scope_are_required():
    registry, saved, owner, guest, scope, payload, root = setup()
    with _binding(
        registry, saved, "execute", owner_principal=owner[0], owner_session=owner[1]
    ).track("source"):
        proof = nested_cancellation_proof_for(saved, *owner)
        guest_id = proof.reserve_child(guest)
        guest_future = Future()
        proof.bind_child(guest_id, guest_future)
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            root_id = proof.reserve_child(root)
            root_future = Future()
            proof.bind_child(root_id, root_future)
            root_scope = CapturedInvocationScopeV4(root, lambda: None, scope)
            leaf = replace(
                _child_envelope(root),
                contract_id="tobkiri.service.tool.arguments.validate.v1",
            )
            assert helper.select_inline_chat_dispatch(leaf, proof, root_scope)
            with pytest.raises(PermissionError):
                helper.select_inline_chat_dispatch(leaf, proof, scope)
            assert not helper.select_inline_chat_dispatch(leaf, None, root_scope)
        assert not helper.select_inline_chat_dispatch(leaf, proof, root_scope)
        root_future.set_result(None)
        guest_future.set_result(None)


@pytest.mark.parametrize(
    "change", ["no_proof", "wrong_owner", "wire_scope", "foreign_capture", "cycle"]
)
def test_source_and_child_forgery_fails_closed(change):
    registry, saved, owner, guest, scope, payload, root = setup()
    with _binding(
        registry, saved, "execute", owner_principal=owner[0], owner_session=owner[1]
    ).track("source"):
        proof = nested_cancellation_proof_for(saved, *owner)
        proof.reserve_child(guest)
        if change == "no_proof":
            proof = None
        elif change == "wrong_owner":
            owner = ("other", owner[1])
        elif change == "wire_scope":
            scope = {"envelope": guest}
        elif change == "foreign_capture":
            scope = CapturedInvocationScopeV4(
                replace(guest, context=replace(guest.context, profile_id="other")),
                lambda: None,
                scope.parent,
            )
        else:
            scope = CapturedInvocationScopeV4(guest, lambda: None, scope)
        with pytest.raises((PermissionError, AttributeError)):
            with helper.finite_saved_chat_source(scope, proof, payload, owner):
                helper.select_inline_chat_dispatch(root, proof, scope)


@pytest.mark.parametrize("change", ["cancel", "expired", "budget", "foreign_parent", "loop"])
def test_inline_selection_rechecks_live_parent_and_finite_budget(change):
    registry, saved, owner, guest, scope, payload, root = setup()
    with _binding(registry, saved, "execute", owner_principal=owner[0], owner_session=owner[1]).track("source"):
        proof = nested_cancellation_proof_for(saved, *owner)
        proof.reserve_child(guest)
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            proof.reserve_child(root)
            root_scope = CapturedInvocationScopeV4(root, lambda: None, scope)
            leaf = replace(_child_envelope(root), contract_id="tobkiri.service.tool.arguments.validate.v1")
            if change == "cancel":
                saved.cancellation_requested.set()
            elif change == "expired":
                import time
                proof._envelope = replace(saved, deadline_monotonic=time.monotonic() - 1)
            elif change == "budget":
                helper._current.get().steps = 16384
            elif change == "foreign_parent":
                root_scope = CapturedInvocationScopeV4(replace(root), lambda: None, scope)
            else:
                object.__setattr__(root_scope, "parent", root_scope)
            with pytest.raises(PermissionError):
                helper.select_inline_chat_dispatch(leaf, proof, root_scope)


def test_candidate_witness_is_exact_and_cleanup_fences_copied_context():
    import contextvars
    from types import SimpleNamespace
    registry, saved, owner, guest, scope, payload, root = setup()
    closed = []
    with _binding(registry, saved, "execute", owner_principal=owner[0], owner_session=owner[1]).track("source"):
        proof = nested_cancellation_proof_for(saved, *owner)
        proof.reserve_child(guest)
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            token = helper._current.get()
            with pytest.raises(PermissionError):
                token.bind_candidate(SimpleNamespace(source_broker_envelope=replace(root)))
            witness = SimpleNamespace(source_broker_envelope=root)
            token.bind_candidate(witness)
            with pytest.raises(PermissionError):
                token.bind_candidate(witness)
            token.cleanup.append(lambda: closed.append(helper._current.get()))
            copied = contextvars.copy_context()
        assert closed == [None]
        assert not token.active
        with pytest.raises(PermissionError):
            copied.run(helper.select_inline_chat_dispatch, root, proof, scope)
