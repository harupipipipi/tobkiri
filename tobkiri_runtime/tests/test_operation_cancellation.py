"""Live cancellation is owner-scoped and never a durable completion proof."""

from dataclasses import replace
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
import contextvars
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


def test_nested_host_futures_require_grandchild_exit_and_resource_drain() -> None:
    """An exited gateway cannot confirm Stop while its adapter still owns work."""

    registry, execution, execute, cancel = _tracked_execution_and_stop()
    gateway = _child_envelope(execution)
    adapter = _child_envelope(gateway)
    gateway_ready, adapter_entered, release_adapter = (
        threading.Event() for _ in range(3)
    )
    adapter_work: list[tuple[int, Future[object]]] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            with execute.track("turn"):
                proof = nested_cancellation_proof_for(execution, "owner", "session")
                assert proof is not None

                def invoke_adapter() -> None:
                    assert nested_cancellation_proof_for(
                        adapter, "owner", "session"
                    ) is proof
                    adapter_entered.set()
                    assert release_adapter.wait(2), "adapter was not released"

                def invoke_gateway() -> None:
                    nested = nested_cancellation_proof_for(
                        gateway, "owner", "session"
                    )
                    assert nested is proof
                    nested.validate_parent(gateway.cancellation_requested)
                    adapter_id = nested.reserve_child(adapter)
                    adapter_context = contextvars.copy_context()
                    adapter_future = pool.submit(adapter_context.run, invoke_adapter)
                    nested.bind_child(adapter_id, adapter_future)
                    adapter_work.append((adapter_id, adapter_future))
                    gateway_ready.set()
                    assert gateway.cancellation_requested.wait(2)

                gateway_id = proof.reserve_child(gateway)
                gateway_context = contextvars.copy_context()
                gateway_future = pool.submit(gateway_context.run, invoke_gateway)
                proof.bind_child(gateway_id, gateway_future)
                assert gateway_ready.wait(2), "gateway did not submit its adapter"
                assert adapter_entered.wait(2), "adapter did not enter"
                adapter_id, adapter_future = adapter_work[0]
                observation = cancel.request("turn")
                proof.record_backend_cancellation(gateway_id, gateway_future)
                proof.record_backend_cancellation(adapter_id, adapter_future)
                gateway_future.result(timeout=2)
                proof.record_resource_drain(gateway_future)
                assert not adapter_future.done()
            assert observation.completed.is_set()
            assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
            release_adapter.set()
            adapter_future.result(timeout=2)
            assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
            proof.record_resource_drain(adapter_future)
            assert observation.wait_for_verified_drain(time.monotonic() + 0.1)
            assert not registry._records
        finally:
            execution.cancellation_requested.set()
            release_adapter.set()


def test_exact_child_can_propagate_after_root_exit_until_all_resources_drain() -> None:
    """A copied child Host context stays enrolled after its root returns normally."""

    registry, execution, execute, _cancel = _tracked_execution_and_stop()
    child = _child_envelope(execution)
    child_future = Future()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(child)
        proof.bind_child(child_id, child_future)
        child_context = contextvars.copy_context()
    assert not registry._active
    assert child_context.run(
        nested_cancellation_proof_for, execution, "owner", "session"
    ) is None
    nested = child_context.run(
        nested_cancellation_proof_for, child, "owner", "session"
    )
    assert nested is proof
    nested.validate_parent(child.cancellation_requested)
    grandchild = _child_envelope(child)
    grandchild_id = nested.reserve_child(grandchild)
    grandchild_future = Future()
    nested.bind_child(grandchild_id, grandchild_future)
    child_future.set_result(None)
    nested.record_resource_drain(child_future)
    assert child_context.run(
        nested_cancellation_proof_for, child, "owner", "session"
    ) is None
    assert child_context.run(
        nested_cancellation_proof_for, grandchild, "owner", "session"
    ) is proof
    with pytest.raises(PermissionError):
        with execute.track("turn"):
            pytest.fail("a live child proof must not be replaced by a new root")
    grandchild_future.set_result(None)
    nested.record_resource_drain(grandchild_future)
    assert not registry._records
    assert child_context.run(
        nested_cancellation_proof_for, grandchild, "owner", "session"
    ) is None
    with pytest.raises(PermissionError):
        nested.validate_parent(child.cancellation_requested)
    with execute.track("turn"):
        assert nested_cancellation_proof_for(execution, "owner", "session") is not proof


def test_child_lookup_requires_exact_enrollment_and_rejects_abandonment() -> None:
    """Matching fields or a shared Event cannot impersonate a reserved child."""

    _registry, execution, execute, _cancel = _tracked_execution_and_stop()
    child = _child_envelope(execution)
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        assert nested_cancellation_proof_for(child, "owner", "session") is None
        child_id = proof.reserve_child(child)
        # A worker may start before bind_child publishes its exact Future.
        assert nested_cancellation_proof_for(child, "owner", "session") is proof
        fabricated = replace(child)
        assert fabricated == child and fabricated is not child
        assert nested_cancellation_proof_for(fabricated, "owner", "session") is None
        with pytest.raises(PermissionError):
            proof.reserve_child(child)
        proof.abandon_child(child_id)
        assert nested_cancellation_proof_for(child, "owner", "session") is None


@pytest.mark.parametrize("owner,session", [("foreign", "session"), ("owner", "other")])
def test_exact_child_proof_rejects_foreign_presentation_owner(
    owner: str, session: str,
) -> None:
    """An exact registered child still cannot cross presentation ownership."""

    _registry, execution, execute, _cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child = _child_envelope(execution)
        proof.reserve_child(child)
        assert nested_cancellation_proof_for(execution, owner, session) is None
        assert nested_cancellation_proof_for(child, owner, session) is None


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other"), ("profile_revision", digest("revision")),
    ("activation_id", "other"), ("activation_digest", digest("activation")),
    ("plan_digest", digest("plan")), ("profile_authority_digest", digest("authority")),
    ("security_epoch", 10), ("fencing_token", 2),
])
def test_child_reservation_rejects_foreign_capture(field: str, value: object) -> None:
    """A shared root signal cannot authorize another activation's child."""

    _registry, execution, execute, _cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child = replace(
            _child_envelope(execution),
            context=replace(execution.context, **{field: value}),
        )
        with pytest.raises(PermissionError):
            proof.reserve_child(child)
        assert nested_cancellation_proof_for(child, "owner", "session") is None


@pytest.mark.parametrize("deadline", [
    0.0, float("nan"), float("inf"), float("-inf"), True, None, "invalid",
])
def test_child_reservation_rejects_expired_or_invalid_deadline(
    deadline: object,
) -> None:
    """Every enrolled child has a live finite deadline inside its root budget."""

    _registry, execution, execute, _cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child = replace(_child_envelope(execution), deadline_monotonic=deadline)
        with pytest.raises(PermissionError):
            proof.reserve_child(child)


def test_child_reservation_rejects_foreign_signal_and_extended_deadline() -> None:
    """A child cannot exchange the private signal or extend the root's budget."""

    _registry, execution, execute, _cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        for child in (
            replace(
                _child_envelope(execution),
                cancellation_requested=threading.Event(),
            ),
            replace(
                _child_envelope(execution),
                deadline_monotonic=execution.deadline_monotonic + 1,
            ),
        ):
            with pytest.raises(PermissionError):
                proof.reserve_child(child)


def test_child_proof_rejects_completed_or_cancelled_identity() -> None:
    """Exited and cancelled children cannot authorize another nested invocation."""

    _registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child = _child_envelope(execution)
        child_id = proof.reserve_child(child)
        child_future = Future()
        proof.bind_child(child_id, child_future)
        assert nested_cancellation_proof_for(child, "owner", "session") is proof
        child_future.set_result(None)
        assert nested_cancellation_proof_for(child, "owner", "session") is None
        proof.record_resource_drain(child_future)
        assert nested_cancellation_proof_for(child, "owner", "session") is None
        live_child = _child_envelope(execution)
        proof.reserve_child(live_child)
        cancel.request("turn")
        assert nested_cancellation_proof_for(live_child, "owner", "session") is None
        with pytest.raises(PermissionError):
            proof.validate_parent(live_child.cancellation_requested)


def test_registry_close_signals_a_child_retained_after_root_exit() -> None:
    """Shutdown revokes late child propagation even after the root handle exits."""

    registry, execution, execute, _cancel = _tracked_execution_and_stop()
    child = _child_envelope(execution)
    child_future = Future()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(child)
        proof.bind_child(child_id, child_future)
        child_context = contextvars.copy_context()
    registry.close()
    assert execution.cancellation_requested.is_set()
    assert child_context.run(
        nested_cancellation_proof_for, child, "owner", "session"
    ) is None
    with pytest.raises(PermissionError):
        proof.reserve_child(_child_envelope(child))
    child_future.set_result(None)
    proof.record_resource_drain(child_future)
    assert not registry._records


def test_duplicate_or_foreign_future_cannot_supply_child_proof() -> None:
    """One exact Future can bind only once and foreign acknowledgements fail."""

    _registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        first_id = proof.reserve_child(_child_envelope(execution))
        second_id = proof.reserve_child(_child_envelope(execution))
        child, foreign = Future(), Future()
        proof.bind_child(first_id, child)
        with pytest.raises(PermissionError):
            proof.bind_child(first_id, child)
        with pytest.raises(PermissionError):
            proof.bind_child(second_id, child)
        proof.abandon_child(second_id)
        observation = cancel.request("turn")
        with pytest.raises(PermissionError):
            proof.record_backend_cancellation(first_id, foreign)
        with pytest.raises(PermissionError):
            proof.record_queued_cancellation(first_id, foreign)
        with pytest.raises(PermissionError):
            proof.record_resource_drain(foreign)
        child.set_result(None)
        proof.record_resource_drain(child)
    assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)


def test_exact_child_cannot_borrow_another_current_invocation_proof() -> None:
    """An envelope enrolled in one tree never qualifies for another tree."""

    registry, execution, execute, _cancel = _tracked_execution_and_stop()
    with execute.track("first"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child = _child_envelope(execution)
        child_id = proof.reserve_child(child)
        child_future = Future()
        proof.bind_child(child_id, child_future)
        first_context = contextvars.copy_context()
        second_root = replace(execution)
        with _binding(registry, second_root, "execute").track("second"):
            assert nested_cancellation_proof_for(child, "owner", "session") is None
            assert nested_cancellation_proof_for(execution, "owner", "session") is None
            assert first_context.run(
                nested_cancellation_proof_for, child, "owner", "session"
            ) is proof
        child_future.set_result(None)
        proof.record_resource_drain(child_future)
    assert not registry._records


def test_expired_exact_child_cannot_propagate_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deadline expiry revokes proof lookup without waiting for a Future to exit."""

    _registry, execution, execute, _cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child = replace(
            _child_envelope(execution),
            deadline_monotonic=execution.deadline_monotonic - 1,
        )
        child_id = proof.reserve_child(child)
        child_future = Future()
        proof.bind_child(child_id, child_future)
        with monkeypatch.context() as patcher:
            patcher.setattr(
                "tobkiri_host.operation_cancellation.time.monotonic",
                lambda: child.deadline_monotonic + 0.5,
            )
            assert nested_cancellation_proof_for(execution, "owner", "session") is proof
            assert nested_cancellation_proof_for(child, "owner", "session") is None
        child_future.set_result(None)
        proof.record_resource_drain(child_future)


@pytest.mark.parametrize("completed_before_stop", [True, False])
def test_stop_ack_distinguishes_precompleted_child_from_prompt_cancel_exit(
    completed_before_stop: bool,
) -> None:
    """A post-Stop backend ACK cannot claim work that had already completed."""

    _registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        if completed_before_stop:
            child.set_result(None)
        observation = cancel.request("turn")
        if not completed_before_stop:
            # Authenticated cancellation may finish the child before its ACK.
            child.set_result(None)
        assert child.done()
        proof.record_backend_cancellation(child_id, child)
        proof.record_resource_drain(child)
    expected = not completed_before_stop
    assert observation.wait_for_verified_drain(time.monotonic() + 0.01) is expected


@pytest.mark.parametrize("completed_before_binding", [True, False])
def test_late_future_binding_requires_observable_work_after_stop_intent(
    completed_before_binding: bool,
) -> None:
    """An already-done, late-bound Future cannot prove when it completed."""

    _registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        observation = cancel.request("turn")
        child = Future()
        if completed_before_binding:
            child.set_result(None)
        proof.bind_child(child_id, child)
        if not completed_before_binding:
            child.set_result(None)
        proof.record_backend_cancellation(child_id, child)
        proof.record_resource_drain(child)
    expected = not completed_before_binding
    assert observation.wait_for_verified_drain(time.monotonic() + 0.01) is expected


def test_repeated_stop_preserves_first_intent_for_prompt_child_completion() -> None:
    """A repeated Stop cannot reclassify a child that finished after intent."""

    _registry, execution, execute, cancel = _tracked_execution_and_stop()
    with execute.track("turn"):
        proof = nested_cancellation_proof_for(execution, "owner", "session")
        assert proof is not None
        child_id = proof.reserve_child(_child_envelope(execution))
        child = Future()
        proof.bind_child(child_id, child)
        observation = cancel.request("turn")
        child.set_result(None)
        assert cancel.request("turn").completed is observation.completed
        proof.record_backend_cancellation(child_id, child)
        proof.record_resource_drain(child)
    assert observation.wait_for_verified_drain(time.monotonic() + 0.1)
