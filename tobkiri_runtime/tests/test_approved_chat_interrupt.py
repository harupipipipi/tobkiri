"""Narrow approved target signalling preserves ordinary owner/session stop."""

from dataclasses import replace
from concurrent.futures import Future
import importlib.util
from pathlib import Path
import time

import pytest

from tests.test_operation_cancellation import _envelope, _child_envelope
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles, nested_cancellation_proof_for,
)
from tobkiri_protocol.canonical import canonical_digest


_PATH = Path(__file__).parents[1] / "tobkiri_host" / "approved_interrupt.py"
_SPEC = importlib.util.spec_from_file_location("approved_interrupt_candidate", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
ApprovedInterruptBinding = _MODULE.ApprovedInterruptBinding


def setup_target(*, owner="owner", session="target-session", field=None):
    registry = OwnedCancellationHandles()
    target = replace(
        _envelope(),
        contract_id="tobkiri.action.turn.saved.v1",
        operation_id="rumi_turn_runtime_pack.turn-saved",
        payload={"request": {"turn_id": "target-turn", "conversation_id": "target"}},
    )
    source = replace(
        _envelope(),
        contract_id="tobkiri.action.chat.message.delivery.v1",
        operation_id="rumi_turn_runtime_pack.chat-message-deliver",
    )
    if field is not None:
        target = replace(target, context=replace(target.context, **field))
    execute = registry.bind(
        group=("rumi_turn_runtime_pack", "saved-turn"), role="execute",
        envelope=target, owner_principal=owner, owner_session=session,
        guard=lambda: None,
    )
    state = {
        "conversation_id": "target", "conversation_revision": 3,
        "turn_id": "target-turn", "revision": 4, "status": "running",
        "request_id": "saved-turn." + canonical_digest({
            "profile_id": source.context.profile_id, "turn_id": "target-turn",
        }).removeprefix("sha256:"),
    }
    return registry, target, source, execute, state


def binding(registry, source, *, authorize=lambda state: None, guard=lambda: None):
    return ApprovedInterruptBinding(
        registry=registry, envelope=source,
        presentation_owner_principal_id="owner", authorize=authorize, guard=guard,
    )


def test_approved_cross_session_signals_exact_target_after_owner_cas():
    registry, target, source, execute, state = setup_target()
    events = []
    port = binding(registry, source, authorize=lambda state: events.append("authority"))
    ordinary = registry.bind(
        group=("rumi_turn_runtime_pack", "saved-turn"), role="stop",
        envelope=source, owner_principal="owner", owner_session="source-session",
        guard=lambda: None,
    )
    with execute.track("target-turn"):
        proof = nested_cancellation_proof_for(target, "owner", "target-session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(target))
        child = Future()
        proof.bind_child(child_id, child)
        assert not ordinary.can_request("target-turn")
        with pytest.raises(PermissionError):
            ordinary.request("target-turn")
        observation = port.request(state, before_signal=lambda: events.append("cas"))
        assert events == ["authority", "cas", "authority"]
        assert target.cancellation_requested.is_set()
        assert not source.cancellation_requested.is_set()
        assert not observation.completed.is_set()
        assert child.cancel()
        proof.record_queued_cancellation(child_id, child)
        proof.record_resource_drain(child)
    assert observation.wait_for_verified_drain(time.monotonic() + 1)


@pytest.mark.parametrize("field", [
    {"profile_id": "foreign"}, {"security_epoch": 999},
    {"activation_id": "foreign"}, {"profile_revision": "sha256:" + "f" * 64},
])
def test_foreign_capture_never_reaches_owner_cas(field):
    registry, target, source, execute, state = setup_target(field=field)
    with execute.track("target-turn"):
        with pytest.raises(PermissionError):
            binding(registry, source).request(
                state, before_signal=lambda: pytest.fail("foreign CAS"),
            )
        assert not target.cancellation_requested.is_set()


def test_foreign_presenter_denied_without_copying_target_session():
    registry, target, source, execute, state = setup_target(owner="foreign")
    with execute.track("target-turn"):
        with pytest.raises(PermissionError):
            binding(registry, source).request(
                state, before_signal=lambda: pytest.fail("foreign owner CAS"),
            )
        assert not target.cancellation_requested.is_set()


@pytest.mark.parametrize("boundary", ["guard", "authority", "cas"])
def test_invalid_authority_cancellation_or_stale_cas_never_signals(boundary):
    registry, target, source, execute, state = setup_target()

    def deny(*args):
        raise PermissionError("denied")

    port = binding(
        registry, source, authorize=deny if boundary == "authority" else lambda s: None,
        guard=deny if boundary == "guard" else lambda: None,
    )
    with execute.track("target-turn"):
        with pytest.raises(PermissionError):
            port.request(state, before_signal=deny if boundary == "cas" else lambda: None)
        assert not target.cancellation_requested.is_set()


def test_shared_saved_scope_signal_cannot_cancel_an_unrelated_source():
    registry, target, source, execute, state = setup_target()
    unrelated = replace(_envelope(), cancellation_requested=target.cancellation_requested)
    other = registry.bind(
        group=("rumi_turn_runtime_pack", "saved-turn"), role="execute",
        envelope=unrelated, owner_principal="owner", owner_session="other-session",
        guard=lambda: None,
    )
    with execute.track("target-turn"), other.track("unrelated-turn"):
        with pytest.raises(PermissionError):
            binding(registry, source).request(
                state, before_signal=lambda: pytest.fail("shared signal CAS"),
            )
        assert not target.cancellation_requested.is_set()


def test_copied_turn_identifier_cannot_interrupt_other_conversation():
    registry, target, source, execute, state = setup_target()
    with execute.track("target-turn"):
        with pytest.raises(PermissionError):
            binding(registry, source).request(
                {**state, "conversation_id": "different"},
                before_signal=lambda: pytest.fail("wrong conversation CAS"),
            )
        assert not target.cancellation_requested.is_set()


def test_forged_durable_request_identity_denied_before_authorization():
    registry, target, source, execute, state = setup_target()
    with execute.track("target-turn"):
        with pytest.raises(ValueError):
            binding(registry, source).request(
                {**state, "request_id": "forged"},
                before_signal=lambda: pytest.fail("forged request CAS"),
            )
        assert not target.cancellation_requested.is_set()
