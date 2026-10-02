"""Live cancellation is owner-scoped and never a durable completion proof."""

from dataclasses import replace
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
import threading
import time

import pytest

from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)
from tobkiri_host.ports import OpaqueInvocationLease
from tests.test_tobkiri_host_execution_integration import context, digest


def _envelope() -> RequestEnvelope:
    return RequestEnvelope(
        context=context(), target_principal=OpaqueAuthorityRef("provider"),
        target_domain=OpaqueAuthorityRef("domain"), contract_id="contract",
        contract_version="1.0.0", operation_id="operation", payload={},
        request_digest=digest("request"), deadline_monotonic=time.monotonic() + 30,
        lease=OpaqueInvocationLease(b"lease"), idempotency_key=None,
    )


def _binding(registry, envelope, role, **overrides):
    values = {
        "group": ("pack", "saved"), "owner_principal": "owner",
        "owner_session": "session", "guard": lambda: None,
    }
    values.update(overrides)
    return registry.bind(envelope=envelope, role=role, **values)


def _child_envelope(parent: RequestEnvelope) -> RequestEnvelope:
    """Create an exact nested envelope sharing the Host-only parent signal."""

    return replace(
        _envelope(),
        cancellation_requested=parent.cancellation_requested,
        deadline_monotonic=parent.deadline_monotonic,
    )


def _tracked_execution_and_stop():
    registry = OwnedCancellationHandles()
    execution, stop = _envelope(), _envelope()
    execute = _binding(registry, execution, "execute")
    cancel = _binding(registry, stop, "stop")
    return registry, execution, execute, cancel


def test_request_signals_only_live_execution_without_discarding_handle() -> None:
    registry = OwnedCancellationHandles()
    execution, stop = _envelope(), _envelope()
    execute = _binding(registry, execution, "execute")
    cancel = _binding(registry, stop, "stop")
    with execute.track("turn"):
        first = cancel.request("turn")
        second = cancel.request("turn")
        assert first.completed is second.completed
        assert not first.completed.is_set()
        assert execution.cancellation_requested.is_set()
        assert not stop.cancellation_requested.is_set()
        with pytest.raises(PermissionError):
            with execute.track("turn"):
                pytest.fail("duplicate execution must not replace the handle")
    assert first.completed.is_set()
    with pytest.raises(PermissionError):
        cancel.request("turn")


@pytest.mark.parametrize("override", [
    {"owner_principal": "other"}, {"owner_session": "other"},
    {"group": ("other-pack", "saved")}, {"group": ("pack", "other-group")},
])
def test_foreign_owner_or_group_cannot_signal(override) -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    with _binding(registry, execution, "execute").track("turn"):
        with pytest.raises(PermissionError):
            _binding(registry, _envelope(), "stop", **override).request("turn")
        assert not execution.cancellation_requested.is_set()


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other"), ("profile_revision", digest("revision")),
    ("activation_id", "other"), ("activation_digest", digest("activation")),
    ("plan_digest", digest("plan")), ("profile_authority_digest", digest("authority")),
    ("security_epoch", 10), ("fencing_token", 2),
])
def test_foreign_capture_cannot_signal(field, value) -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    foreign = replace(_envelope(), context=replace(context(), **{field: value}))
    with _binding(registry, execution, "execute").track("turn"):
        with pytest.raises(PermissionError):
            _binding(registry, foreign, "stop").request("turn")
        assert not execution.cancellation_requested.is_set()


def test_roles_guards_and_close_preserve_live_ownership() -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    producer = _binding(registry, execution, "execute")
    consumer = _binding(registry, _envelope(), "stop")
    with pytest.raises(PermissionError):
        with consumer.track("turn"):
            pytest.fail("stop role cannot register work")
    with producer.track("turn"):
        with pytest.raises(PermissionError):
            producer.request("turn")
        def stale():
            raise PermissionError("stale")
        with pytest.raises(PermissionError, match="stale"):
            _binding(registry, _envelope(), "stop", guard=stale).request("turn")
        assert not execution.cancellation_requested.is_set()
        registry.close()
        assert execution.cancellation_requested.is_set()
        assert len(registry._active) == 1
    assert not registry._active
    with pytest.raises(PermissionError):
        with producer.track("new-turn"):
            pytest.fail("closed capture cannot register work")


def test_two_live_invocations_do_not_share_signals_or_exit_observations() -> None:
    registry = OwnedCancellationHandles()
    first, second, stop = _envelope(), _envelope(), _envelope()
    cancel = _binding(registry, stop, "stop")
    with _binding(registry, first, "execute").track("first"):
        with _binding(registry, second, "execute").track("second"):
            observed = cancel.request("first")
            assert first.cancellation_requested.is_set()
            assert not second.cancellation_requested.is_set()
            assert not observed.completed.is_set()
        assert not observed.completed.is_set()
    assert observed.completed.is_set()
    assert not stop.cancellation_requested.is_set()


@pytest.mark.parametrize("role", ["execute", "stop"])
def test_invocation_expiring_during_lock_wait_cannot_change_handles(role: str) -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    waiting, expired, entered = (threading.Event() for _ in range(3))
    owner_thread = threading.current_thread()

    class ContendedLock:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def __enter__(self) -> "ContendedLock":
            if threading.current_thread() is not owner_thread:
                waiting.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self.lock.release()

    registry._lock = ContendedLock()

    def guard() -> None:
        if expired.is_set():
            raise PermissionError("invocation expired during lock wait")

    binding = _binding(
        registry, execution if role == "execute" else _envelope(), role, guard=guard,
    )

    def attempt() -> None:
        if role == "stop":
            binding.request("turn")
        else:
            with binding.track("turn"):
                entered.set()

    live = (
        _binding(registry, execution, "execute").track("turn")
        if role == "stop" else nullcontext()
    )
    with live, ThreadPoolExecutor(max_workers=1) as pool:
        with registry._lock:
            future = pool.submit(attempt)
            assert waiting.wait(2), "invocation did not contend for the registry lock"
            expired.set()
        with pytest.raises(PermissionError, match="expired during lock wait"):
            future.result(timeout=2)
    assert not execution.cancellation_requested.is_set()
    assert not entered.is_set()
    assert not registry._active


def test_cancelled_envelope_cannot_register_execution() -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    execution.cancellation_requested.set()
    with pytest.raises(PermissionError):
        with _binding(registry, execution, "execute").track("turn"):
            pytest.fail("cancelled execution must not become active")
    assert not registry._active


def test_verified_drain_requires_exact_backend_cancel_future_and_scope_exit() -> None:
    """A complete exact child may confirm only after its tracked Host scope exits."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        observation = cancel.request("turn")
        proof.record_backend_cancellation(child_id, child)
        child.set_result(None)
        assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
        proof.record_resource_drain(child)
        assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
    assert observation.wait_for_verified_drain(time.monotonic() + 0.1)


def test_verified_drain_accepts_an_unstarted_exact_child_cancellation() -> None:
    """Queued work needs no backend owner, but still must complete exactly."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        observation = cancel.request("turn")
        assert child.cancel()
        proof.record_queued_cancellation(child_id, child)
        proof.record_resource_drain(child)
    assert observation.wait_for_verified_drain(time.monotonic() + 0.1)


def test_stop_before_child_rejects_late_registration_without_false_confirmation() -> None:
    """Wrapper scope exit alone never proves nested Provider termination."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        observation = cancel.request("turn")
        with pytest.raises(PermissionError):
            proof.reserve_child(_child_envelope(execution))
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.03)


def test_completed_child_before_stop_cannot_supply_a_cancellation_ack() -> None:
    """Normal completion before stop is not a nested cancellation acknowledgement."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        child.set_result(None)
        proof.record_resource_drain(child)
        observation = cancel.request("turn")
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.03)


def test_pre_request_history_does_not_hide_a_live_child_drain_requirement() -> None:
    """Only the child live at request time needs cancellation and exact exit."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        completed_id = proof.reserve_child(_child_envelope(execution))
        completed_child = Future()
        proof.bind_child(completed_id, completed_child)
        completed_child.set_result(None)
        proof.record_resource_drain(completed_child)
        live_id = proof.reserve_child(_child_envelope(execution))
        live_child = Future()
        proof.bind_child(live_id, live_child)
        observation = cancel.request("turn")
        proof.record_backend_cancellation(live_id, live_child)
        live_child.set_result(None)
        proof.record_resource_drain(live_child)
        assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
    assert observation.wait_for_verified_drain(time.monotonic() + 0.1)


@pytest.mark.parametrize("finish_child,record_backend", [(False, True), (True, False)])
def test_verified_drain_fails_closed_for_live_or_unacknowledged_child(
    finish_child: bool,
    record_backend: bool,
) -> None:
    """A missing Future exit or authenticated backend cancellation never confirms."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        observation = cancel.request("turn")
        if record_backend:
            proof.record_backend_cancellation(child_id, child)
        if finish_child:
            child.set_result(None)
            proof.record_resource_drain(child)
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.03)


def test_foreign_or_replayed_nested_proof_cannot_confirm_a_turn() -> None:
    """The active proof is bound to one outer envelope, owner, and capture."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    foreign = replace(
        _envelope(), cancellation_requested=execution.cancellation_requested
    )
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        assert nested_cancellation_proof_for(foreign, "owner", "session") is None
        assert nested_cancellation_proof_for(execution, "foreign", "session") is None
        with pytest.raises(PermissionError):
            proof.reserve_child(
                replace(
                    _child_envelope(execution),
                    context=replace(execution.context, profile_id="foreign"),
                )
            )
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        observation = cancel.request("turn")
        child.set_result(None)
    assert observation.completed.is_set()
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.03)


@pytest.mark.parametrize(
    "deadline", [float("nan"), float("inf"), float("-inf")]
)
def test_verified_drain_rejects_nonfinite_stop_deadline(deadline: float) -> None:
    """Proof wait treats malformed Host deadline values as unverified."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        observation = cancel.request("turn")
    assert not observation.wait_for_verified_drain(deadline)


def test_verified_drain_never_waits_past_stop_deadline() -> None:
    """The stop response reserves a small deadline margin for its own fence."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        observation = cancel.request("turn")
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.001)
