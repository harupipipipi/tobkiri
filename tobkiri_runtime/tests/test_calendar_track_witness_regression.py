"""Calendar registry guard units; real envelopes, no native capture claim."""

from concurrent.futures import Future
from dataclasses import replace
import threading
import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _child_envelope, _envelope
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)


def setup():
    payload = dict(
        profile_id="profile-1",
        operation="dispatch",
        action_id="chat.saved",
        payload={
            "profile_id": "profile-1",
            "message": "hello",
            "conversation_id": None,
        },
        idempotency_key="key",
        schedule_id="schedule",
        lease_id="lease",
    )
    parent = replace(
        _envelope(),
        contract_id="tobkiri.action.job.v1",
        operation_id="rumi_job_action_broker_pack.job-action-broker",
        payload=payload,
    )
    target = replace(
        _child_envelope(parent),
        contract_id="tobkiri.action.job.adapter.v2",
        contract_version="2.0.0",
        operation_id="rumi_turn_runtime_pack.chat-saved-job-adapter",
        payload={**payload},
        idempotency_key="key",
        cancellation_requested=threading.Event(),
    )
    scope = CapturedInvocationScopeV4(parent, lambda: None)

    def witness(actual_scope, actual_target):
        if actual_scope is not scope or actual_target is not target:
            raise PermissionError("actual Host selection changed")

    return OwnedCancellationHandles(), parent, target, scope, witness


def enter(registry, scope, target, witness):
    return registry.private_calendar_dispatch_scope(
        scope=scope,
        target_envelope=target,
        owner_principal="host-owner",
        owner_session="host-session",
        selected_adapter_guard=witness,
    )


def saved_binding(registry, envelope, *, guard=lambda: None, session="host-session"):
    return registry.bind(
        group=("rumi_turn_runtime_pack", "saved-turn"),
        role="execute",
        envelope=envelope,
        owner_principal="host-owner",
        owner_session=session,
        guard=guard,
    )


@pytest.mark.parametrize("failure", ["selection", "invocation"])
def test_selected_witness_and_fresh_invocation_guard_survive_saved_track(failure):
    registry, parent, target, scope, witness = setup()
    selected = [True]
    fresh = [True]

    def selected_guard(actual_scope, actual_target):
        witness(actual_scope, actual_target)
        if not selected[0]:
            raise PermissionError("selected adapter revoked")

    def invocation_guard():
        if not fresh[0]:
            raise TimeoutError("current invocation expired")

    execution = saved_binding(registry, target, guard=invocation_guard)
    with enter(registry, scope, target, selected_guard) as parentproof:
        child = parentproof.reserve_independent_calendar_child(target)
        future = Future()
        parentproof.bind_child(child, future)
        with parentproof.independent_calendar_execution_scope(target):
            with execution.track("calendar-turn"):
                proof = nested_cancellation_proof_for(
                    target, "host-owner", "host-session"
                )
                assert proof._calendar_execution_witness is not None
                if failure == "selection":
                    selected[0] = False
                else:
                    fresh[0] = False
                with pytest.raises((PermissionError, TimeoutError)):
                    proof.validate_parent(target.cancellation_requested)
                with pytest.raises((PermissionError, TimeoutError)):
                    proof.reserve_child(_child_envelope(target))
                selected[0] = fresh[0] = True
        future.set_result(None)
        parentproof.record_resource_drain(future)


def test_registered_saved_descendant_retains_calendar_witness():
    registry, parent, target, scope, witness = setup()
    selected = [True]

    def selected_guard(actual_scope, actual_target):
        witness(actual_scope, actual_target)
        if not selected[0]:
            raise PermissionError("selection revoked")

    with enter(registry, scope, target, selected_guard) as parentproof:
        child = parentproof.reserve_independent_calendar_child(target)
        future = Future()
        parentproof.bind_child(child, future)
        with parentproof.independent_calendar_execution_scope(target):
            with saved_binding(registry, target).track("calendar-turn"):
                calendar = nested_cancellation_proof_for(
                    target, "host-owner", "host-session"
                )
                descendant = replace(
                    _child_envelope(target),
                    contract_id="tobkiri.action.turn.saved.v1",
                    operation_id="rumi_turn_runtime_pack.turn-saved",
                    contract_version="1.0.0",
                )
                childid = calendar.reserve_child(descendant)
                nestedfuture = Future()
                calendar.bind_child(childid, nestedfuture)
                with saved_binding(registry, descendant).track("saved-child"):
                    tracked = nested_cancellation_proof_for(
                        descendant, "host-owner", "host-session"
                    )
                    assert tracked._calendar_execution_witness is not None
                    selected[0] = False
                    with pytest.raises(PermissionError):
                        tracked.validate_parent(descendant.cancellation_requested)
                    selected[0] = True
                nestedfuture.set_result(None)
                calendar.record_resource_drain(nestedfuture)
        future.set_result(None)
        parentproof.record_resource_drain(future)


@pytest.mark.parametrize(
    "change", ["cloned_envelope", "foreign_owner", "foreign_registry"]
)
def test_private_calendar_marker_never_authorizes_unmatched_track(change):
    registry, parent, target, scope, witness = setup()
    with enter(registry, scope, target, witness) as parentproof:
        child = parentproof.reserve_independent_calendar_child(target)
        future = Future()
        parentproof.bind_child(child, future)
        with parentproof.independent_calendar_execution_scope(target):
            actual = replace(target) if change == "cloned_envelope" else target
            other = (
                OwnedCancellationHandles() if change == "foreign_registry" else registry
            )
            session = "foreign-session" if change == "foreign_owner" else "host-session"
            with pytest.raises(PermissionError):
                with saved_binding(other, actual, session=session).track("wrong-turn"):
                    pass
        future.set_result(None)
        parentproof.record_resource_drain(future)


def test_ordinary_saved_proof_does_not_inherit_unrelated_guard():
    registry = OwnedCancellationHandles()
    envelope = _envelope()
    fresh = [True]

    def guard():
        if not fresh[0]:
            raise PermissionError("guard revoked")

    with saved_binding(registry, envelope, guard=guard).track("ordinary"):
        proof = nested_cancellation_proof_for(envelope, "host-owner", "host-session")
        assert proof._calendar_execution_witness is None
        assert proof._execution_guard is None
        fresh[0] = False
        proof.validate_parent(envelope.cancellation_requested)


def test_calendar_native_deadline_expiry_is_rechecked_after_saved_track(monkeypatch):
    import tobkiri_host.operation_cancellation as cancellation

    current = [10.0]
    monkeypatch.setattr(cancellation.time, "monotonic", lambda: current[0])
    registry, parent, target, scope, witness = setup()
    with enter(registry, scope, target, witness) as parentproof:
        identifier = parentproof.reserve_independent_calendar_child(target)
        future = Future()
        parentproof.bind_child(identifier, future)
        with parentproof.independent_calendar_execution_scope(target):
            with saved_binding(registry, target).track("calendar-turn"):
                proof = nested_cancellation_proof_for(
                    target, "host-owner", "host-session"
                )
                current[0] = (
                    max(parent.deadline_monotonic, target.deadline_monotonic) + 1
                )
                with pytest.raises(PermissionError):
                    proof.validate_parent(target.cancellation_requested)
        future.set_result(None)
        parentproof.record_resource_drain(future)
