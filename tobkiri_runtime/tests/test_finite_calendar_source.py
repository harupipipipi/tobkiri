"""Actual registry/Future scheduling units; no production-capture claim."""

from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import replace
import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_calendar_track_witness_regression import setup, enter, saved_binding
from tests.test_operation_cancellation import _child_envelope
from tobkiri_host.operation_cancellation import nested_cancellation_proof_for
from tobkiri_host import finite_chat_dispatch as helper
from tobkiri_protocol.canonical import canonical_digest


@contextmanager
def calendar_source():
    registry, job, adapter, job_scope, selected = setup()
    live = [True]

    def witness(scope, envelope):
        selected(scope, envelope)
        if not live[0]:
            raise PermissionError("selected provider revoked")

    request = {
        "conversation_id": "calendar:"
        + canonical_digest([adapter.context.profile_id, adapter.idempotency_key])[7:],
        "turn_id": "calendar:"
        + canonical_digest(
            [adapter.context.profile_id, adapter.payload["schedule_id"], adapter.idempotency_key]
        )[7:],
    }
    guest = replace(
        _child_envelope(adapter),
        contract_id=helper.GUEST[0],
        operation_id=helper.GUEST[1],
        payload={"request": request},
    )
    adapter_scope = CapturedInvocationScopeV4(adapter, lambda: None, job_scope)
    scope = CapturedInvocationScopeV4(guest, lambda: None, adapter_scope)
    owner = ("host-owner", "host-session")
    payload = {
        "tool_id": "chat_send_message",
        "tool_call_id": "call",
        "arguments": {
            "target_kind": "chat",
            "target_id": "target",
            "content": "hello",
            "delivery": "steer",
        },
    }
    with enter(registry, job_scope, adapter, witness) as parent:
        slot = parent.reserve_independent_calendar_child(adapter)
        outer = Future()
        outer.set_running_or_notify_cancel()
        parent.bind_child(slot, outer)
        with parent.independent_calendar_execution_scope(adapter):
            with saved_binding(registry, adapter).track("actual-calendar-source"):
                proof = nested_cancellation_proof_for(adapter, *owner)
                child = proof.reserve_child(guest)
                future = Future()
                future.set_running_or_notify_cancel()
                proof.bind_child(child, future)
                try:
                    yield scope, proof, payload, owner, live, future
                finally:
                    live[0] = True
                    with proof._registry._lock:
                        proof._children[child].future = future
                    future.set_result(None)
                    proof.record_resource_drain(future)
        outer.set_result(None)
        parent.record_resource_drain(outer)


def test_actual_selected_adapter_direct_guest_mints_finite_source():
    with calendar_source() as (scope, proof, payload, owner, _, _future):
        parent, selected = helper._assert_saved_source_ancestry(scope, proof, owner)
        assert selected and parent is scope.parent
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            root = replace(
                _child_envelope(scope.envelope),
                contract_id=helper.BROKER[0],
                operation_id=helper.BROKER[1],
                payload=payload,
            )
            assert helper.select_inline_chat_dispatch(root, proof, scope)


@pytest.mark.parametrize(
    "failure",
    [
        "revoked",
        "expired",
        "foreign_owner",
        "clone",
        "untracked",
        "fake_payload",
        "missing_witness",
        "cancelled",
    ],
)
def test_selected_calendar_source_rejects_unavailable_or_forged_identity(failure):
    with calendar_source() as (scope, proof, payload, owner, live, future):
        if failure == "revoked":
            live[0] = False
        elif failure == "expired":
            scope = CapturedInvocationScopeV4(
                scope.envelope,
                lambda: (_ for _ in ()).throw(TimeoutError()),
                scope.parent,
            )
        elif failure == "foreign_owner":
            owner = ("foreign", owner[1])
        elif failure == "clone":
            scope = CapturedInvocationScopeV4(replace(scope.envelope), lambda: None, scope.parent)
        elif failure == "untracked":
            with proof._registry._lock:
                next(iter(proof._children.values())).future = None
        elif failure == "fake_payload":
            scope.envelope.payload["request"]["turn_id"] = "forged-calendar-turn"
        elif failure == "missing_witness":
            proof._calendar_execution_witness = None
        elif failure == "cancelled":
            proof.request()
        with pytest.raises((PermissionError, TimeoutError)):
            with helper.finite_saved_chat_source(scope, proof, payload, owner):
                pytest.fail("unavailable source minted scheduling token")


def test_selected_calendar_source_keeps_existing_finite_ancestry_bound():
    with calendar_source() as (scope, proof, payload, owner, _, _future):
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            root = replace(
                _child_envelope(scope.envelope),
                contract_id=helper.BROKER[0],
                operation_id=helper.BROKER[1],
                payload=payload,
            )
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            parent = CapturedInvocationScopeV4(root, lambda: None, scope)
            slot = proof.reserve_child(root)
            running = Future()
            running.set_running_or_notify_cancel()
            proof.bind_child(slot, running)
            try:
                for _ in range(16):
                    parent = CapturedInvocationScopeV4(root, lambda: None, parent)
                with pytest.raises(PermissionError, match="ancestry"):
                    helper.select_inline_chat_dispatch(root, proof, parent)
            finally:
                running.set_result(None)
                proof.record_resource_drain(running)


def test_direct_calendar_suffix_normalizes_only_actual_token_scopes():
    from ecosystem.rumi_tool_broker_pack.runtime.chat_delivery import (
        captured_delivery_ancestors,
        _delivery_owner,
    )

    with calendar_source() as (scope, proof, payload, owner, _, _future):
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            root = replace(
                _child_envelope(scope.envelope),
                contract_id=helper.BROKER[0],
                operation_id=helper.BROKER[1],
                payload=payload,
            )
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            broker_scope = CapturedInvocationScopeV4(root, lambda: None, scope)
            scopes = captured_delivery_ancestors(broker_scope, limit=10)
            assert scopes == [broker_scope, scope, scope.parent]
            assert _delivery_owner(scopes) == owner
            clone = CapturedInvocationScopeV4(
                replace(scope.parent.envelope), lambda: None, scope.parent.parent
            )
            forged = CapturedInvocationScopeV4(scope.envelope, lambda: None, clone)
            forged_broker = CapturedInvocationScopeV4(root, lambda: None, forged)
            assert _delivery_owner(captured_delivery_ancestors(forged_broker, limit=10)) != owner


@pytest.mark.parametrize("replacement", ["noop_adapter", "reparent_adapter", "clone_parent_chain"])
def test_original_envelopes_cannot_replace_retained_calendar_scope(replacement):
    """Retaining genuine envelopes cannot manufacture the physical scope join."""
    from ecosystem.rumi_tool_broker_pack.runtime.chat_delivery import (
        captured_delivery_ancestors,
        _delivery_owner,
    )

    with calendar_source() as (scope, proof, payload, owner, _, _future):
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            root = replace(
                _child_envelope(scope.envelope),
                contract_id=helper.BROKER[0],
                operation_id=helper.BROKER[1],
                payload=payload,
            )
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            original_adapter = scope.parent
            assert original_adapter is not None
            parent = original_adapter.parent
            if replacement == "reparent_adapter":
                parent = None
            elif replacement == "clone_parent_chain":
                assert parent is not None
                parent = CapturedInvocationScopeV4(
                    parent.envelope,
                    lambda: None,
                    parent.parent,
                )
            substituted = CapturedInvocationScopeV4(
                original_adapter.envelope,
                lambda: None,
                parent,
            )
            guest = CapturedInvocationScopeV4(scope.envelope, lambda: None, substituted)
            supplied = CapturedInvocationScopeV4(root, lambda: None, guest)
            with pytest.raises(PermissionError):
                captured_delivery_ancestors(supplied, limit=10)
            with pytest.raises(PermissionError):
                _delivery_owner([supplied, guest, substituted])


def test_recaptured_guest_keeps_exact_retained_parent_and_original_guard():
    """Production recaptures Guest wrappers, retaining the actual parent object."""
    from ecosystem.rumi_tool_broker_pack.runtime.chat_delivery import (
        captured_delivery_ancestors,
        _delivery_owner,
    )

    with calendar_source() as (scope, proof, payload, owner, live, _future):
        with helper.finite_saved_chat_source(scope, proof, payload, owner):
            root = replace(
                _child_envelope(scope.envelope),
                contract_id=helper.BROKER[0],
                operation_id=helper.BROKER[1],
                payload=payload,
            )
            assert helper.select_inline_chat_dispatch(root, proof, scope)
            recaptured = CapturedInvocationScopeV4(scope.envelope, lambda: None, scope.parent)
            supplied = CapturedInvocationScopeV4(root, lambda: None, recaptured)
            scopes = captured_delivery_ancestors(supplied, limit=10)
            assert scopes[-1] is scope.parent
            assert _delivery_owner(scopes) == owner
            live[0] = False
            with pytest.raises(PermissionError, match="revoked"):
                captured_delivery_ancestors(supplied, limit=10)
