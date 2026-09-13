"""Live cancellation is owner-scoped and never a durable completion proof."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import threading
import time

import pytest

from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.operation_cancellation import OwnedCancellationHandles
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
    ("security_epoch", 10),
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
