"""Independent saved branches keep parent fanout and exact drain ownership."""

from concurrent.futures import Future
from dataclasses import replace
import threading
import time

import pytest

from tests.test_operation_cancellation import _envelope, _binding, _child_envelope
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles, nested_cancellation_proof_for,
)
from tobkiri_host.approved_interrupt import ApprovedInterruptBinding
from tobkiri_protocol.canonical import canonical_digest


def independent(parent):
    return replace(
        _child_envelope(parent), cancellation_requested=threading.Event(),
        contract_id="tobkiri.action.turn.saved.v1",
        operation_id="rumi_turn_runtime_pack.turn-saved",
    )


def test_normal_child_cannot_supply_an_independent_signal():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        with pytest.raises(PermissionError):
            proof.reserve_child(independent(parent))


def test_only_finite_saved_operation_can_reserve_independent_branch():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        with pytest.raises(PermissionError):
            proof.reserve_independent_saved_child(replace(
                independent(parent), operation_id="arbitrary-operation",
            ))


def test_independent_child_has_exact_authenticated_parent_membership():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        child = independent(parent)
        proof.reserve_independent_saved_child(child)
        assert nested_cancellation_proof_for(child, "owner", "session") is proof
        assert nested_cancellation_proof_for(replace(child), "owner", "session") is None
        assert nested_cancellation_proof_for(child, "owner", "foreign-session") is None
        child.cancellation_requested.set()
        assert nested_cancellation_proof_for(child, "owner", "session") is None


@pytest.mark.parametrize("change", [
    {"profile_id": "foreign"}, {"security_epoch": 999},
    {"fencing_token": 999}, {"plan_digest": "sha256:" + "f" * 64},
])
def test_independent_branch_keeps_full_capture_binding(change):
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        child = independent(parent)
        with pytest.raises(PermissionError):
            proof.reserve_independent_saved_child(replace(
                child, context=replace(child.context, **change),
            ))


def test_independent_branch_cannot_extend_authenticated_parent_deadline():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        with pytest.raises(PermissionError):
            proof.reserve_independent_saved_child(replace(
                independent(parent), deadline_monotonic=parent.deadline_monotonic + 1,
            ))


def test_parent_cancel_fans_out_reserved_and_bound_independent_branches():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        first, second = independent(parent), independent(parent)
        first_id = proof.reserve_independent_saved_child(first)
        second_id = proof.reserve_independent_saved_child(second)
        future = Future()
        proof.bind_child(first_id, future)
        observation = _binding(registry, _envelope(), "stop").request("parent")
        assert parent.cancellation_requested.is_set()
        assert first.cancellation_requested.is_set()
        assert second.cancellation_requested.is_set()
        # Source stop before the second Future bind must not miss this child.
        late_future = Future()
        proof.bind_child(second_id, late_future)
        for child_id, pending in ((first_id, future), (second_id, late_future)):
            assert pending.cancel()
            proof.record_queued_cancellation(child_id, pending)
            proof.record_resource_drain(pending)
    assert observation.wait_for_verified_drain(time.monotonic() + 0.1)


def test_target_stop_leaves_parent_and_independent_sibling_running():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        parent_proof = nested_cancellation_proof_for(parent, "owner", "session")
        target, sibling = independent(parent), independent(parent)
        parent_proof.reserve_independent_saved_child(target)
        parent_proof.reserve_independent_saved_child(sibling)
        with _binding(registry, target, "execute").track("target"):
            target_proof = nested_cancellation_proof_for(target, "owner", "session")
            child_id = target_proof.reserve_child(_child_envelope(target))
            child = Future()
            target_proof.bind_child(child_id, child)
            observation = _binding(registry, _envelope(), "stop").request("target")
            assert target.cancellation_requested.is_set()
            assert not parent.cancellation_requested.is_set()
            assert not sibling.cancellation_requested.is_set()
            assert child.cancel()
            target_proof.record_queued_cancellation(child_id, child)
            target_proof.record_resource_drain(child)
        assert observation.wait_for_verified_drain(time.monotonic() + 0.1)


def test_independent_signal_cannot_be_reused_for_a_second_branch():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        first = independent(parent)
        proof.reserve_independent_saved_child(first)
        with pytest.raises(PermissionError):
            proof.reserve_independent_saved_child(replace(first))


def test_independent_branch_cannot_borrow_an_unrelated_tracked_signal():
    registry, parent, unrelated = (
        OwnedCancellationHandles(), _envelope(), _envelope(),
    )
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        with _binding(registry, unrelated, "execute").track("unrelated"):
            with pytest.raises(PermissionError):
                proof.reserve_independent_saved_child(replace(
                    independent(parent),
                    cancellation_requested=unrelated.cancellation_requested,
                ))


def test_parent_timeout_signal_fans_out_when_scope_closes():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        child = independent(parent)
        proof.reserve_independent_saved_child(child)
        parent.cancellation_requested.set()
    assert child.cancellation_requested.is_set()


def test_shutdown_fans_out_before_independent_child_has_its_own_tracking_scope():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        child = independent(parent)
        proof.reserve_independent_saved_child(child)
        registry.close()
        assert parent.cancellation_requested.is_set()
        assert child.cancellation_requested.is_set()


def test_done_future_without_backend_cancellation_never_proves_drain():
    registry, parent = OwnedCancellationHandles(), _envelope()
    with _binding(registry, parent, "execute").track("parent"):
        proof = nested_cancellation_proof_for(parent, "owner", "session")
        child = independent(parent)
        identity = proof.reserve_independent_saved_child(child)
        future = Future()
        proof.bind_child(identity, future)
        observation = _binding(registry, _envelope(), "stop").request("parent")
        future.set_result(None)
        proof.record_resource_drain(future)
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.02)


def test_approved_interrupt_handles_independent_incoming_target_without_source_stop():
    registry, parent = OwnedCancellationHandles(), _envelope()
    group = ("rumi_turn_runtime_pack", "saved-turn")
    owner_binding = registry.bind(
        group=group, role="execute", envelope=parent,
        owner_principal="owner", owner_session="source-session", guard=lambda: None,
    )
    delivery = replace(
        _envelope(), cancellation_requested=parent.cancellation_requested,
        contract_id="tobkiri.action.chat.message.delivery.v1",
        operation_id="rumi_turn_runtime_pack.chat-message-deliver",
    )
    target = replace(
        independent(parent),
        payload={"request": {"turn_id": "target", "conversation_id": "target-chat"}},
    )
    target_binding = registry.bind(
        group=group, role="execute", envelope=target,
        owner_principal="owner", owner_session="target-session", guard=lambda: None,
    )
    state = {
        "conversation_id": "target-chat", "conversation_revision": 3,
        "turn_id": "target", "revision": 2, "status": "running",
        "request_id": "saved-turn." + canonical_digest({
            "profile_id": parent.context.profile_id, "turn_id": "target",
        }).removeprefix("sha256:"),
    }
    port = ApprovedInterruptBinding(
        registry=registry, envelope=delivery,
        presentation_owner_principal_id="owner", authorize=lambda state: None,
        guard=lambda: None,
    )
    with owner_binding.track("source"):
        parent_proof = nested_cancellation_proof_for(parent, "owner", "source-session")
        target_id = parent_proof.reserve_independent_saved_child(target)
        target_future = Future()
        parent_proof.bind_child(target_id, target_future)
        with target_binding.track("target"):
            proof = nested_cancellation_proof_for(target, "owner", "target-session")
            child_id = proof.reserve_child(_child_envelope(target))
            future = Future()
            proof.bind_child(child_id, future)
            observation = port.request(state, before_signal=lambda: None)
            assert target.cancellation_requested.is_set()
            assert not parent.cancellation_requested.is_set()
            assert future.cancel()
            proof.record_queued_cancellation(child_id, future)
            proof.record_resource_drain(future)
        assert observation.wait_for_verified_drain(time.monotonic() + 0.1)
        target_future.set_result(None)
        parent_proof.record_resource_drain(target_future)
        assert not parent.cancellation_requested.is_set()
