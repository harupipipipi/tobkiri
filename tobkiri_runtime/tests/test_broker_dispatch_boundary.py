"""Executor queue regressions: real Authority, synthetic effect provider."""

from concurrent.futures import ThreadPoolExecutor
import contextvars
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Iterator

import pytest

from tobkiri_host.broker import RequestBroker
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import ProviderExecutionError, RequestTimedOutError
from tobkiri_host.models import InvocationFrame
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_tobkiri_host_authority_v4_adapter import _adapter, _broker, _context


@contextmanager
def _blocked_worker(broker: RequestBroker) -> Iterator[tuple[Event, Event]]:
    """Occupy the only worker; signal exactly when dispatch is queued."""
    broker._executor.shutdown(wait=True)
    executor = ThreadPoolExecutor(max_workers=1)
    broker._executor = executor
    occupied, release, submitted = Event(), Event(), Event()

    def block() -> None:
        occupied.set()
        assert release.wait(5), "test did not release worker"

    blocker = executor.submit(block)
    assert occupied.wait(2)
    submit = executor.submit

    def observed_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        submitted.set()
        return future

    executor.submit = observed_submit
    try:
        yield release, submitted
    finally:
        release.set()
        blocker.result(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)
        broker.close()


def _frame() -> InvocationFrame:
    return InvocationFrame(
        contract_id="host.http", version_range=">=1,<2", operation_id="invoke",
        payload={"message": "safe fixture"}, idempotency_key="queue-boundary",
    )


@pytest.mark.parametrize("change", ["grant", "provider", "expiry", "cancel"])
def test_queued_request_rechecks_real_authority_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    """Revocation, expiry and fencing after enqueue prohibit effects."""
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    binding = broker.prepare(_frame(), _context(harness)).binding
    backend = broker._backends.select(binding, production=True)
    invoked = Event()
    monkeypatch.setattr(backend, "invoke", lambda envelope: invoked.set())
    with _blocked_worker(broker) as (release, submitted):
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(
                broker.invoke, _frame(), _context(harness),
                effect_scope=harness.scope.to_dict(),
            )
            assert submitted.wait(2)
            if change in {"grant", "provider"}:
                harness.kernel.revoke(
                    target_kind="grant" if change == "grant" else "provider_authority",
                    target_id=(harness.grant.grant_id if change == "grant"
                               else harness.provider.record_id),
                    reason="test revoke while queued",
                )
            elif change == "expiry":
                harness.clock.value += 3_600
            else:
                adapter.fence_request("request-1")
            release.set()
            with pytest.raises(ProviderExecutionError):
                result.result(timeout=2)
    assert not invoked.is_set()
    assert not any(event["event_state"] == "dispatched"
                   for event in harness.store.audit_events())


def test_deadline_elapsed_in_queue_never_consumes_or_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake clock proves expiry is checked inside the actual worker."""
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    prepared = replace(
        broker.prepare(_frame(), _context(harness)), deadline_monotonic=20.0,
    )
    checked, invoked, pending = Event(), Event(), Event()
    monkeypatch.setattr(adapter, "recheck_effect_boundary", lambda *args: checked.set())
    backend = broker._backends.select(prepared.binding, production=True)
    monkeypatch.setattr(backend, "invoke", lambda envelope: invoked.set())
    clock = [10.0]
    with _blocked_worker(broker) as (release, submitted):
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(
                broker._execute_prepared, prepared, _context(harness),
                effect_scope=harness.scope.to_dict(),
                monotonic_clock=lambda: clock[0], before_dispatch=pending.set,
            )
            assert submitted.wait(2)
            clock[0] = 21.0
            release.set()
            with pytest.raises(RequestTimedOutError):
                result.result(timeout=2)
    assert not checked.is_set()
    assert not invoked.is_set()
    assert not pending.is_set()


def test_queue_timeout_is_known_unexecuted_and_cancels_future(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ambiguous external effect or backend cancel before provider start."""
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    backend = broker._backends.select(
        broker.prepare(_frame(), _context(harness)).binding, production=True,
    )
    invoked, cancelled = Event(), Event()
    monkeypatch.setattr(backend, "invoke", lambda envelope: invoked.set())
    monkeypatch.setattr(backend, "cancel", lambda request_id: cancelled.set())
    with _blocked_worker(broker) as (_release, submitted):
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(
                broker.invoke, replace(_frame(), timeout_ms=100),
                _context(harness), effect_scope=harness.scope.to_dict(),
            )
            assert submitted.wait(2)
            with pytest.raises(RequestTimedOutError):
                result.result(timeout=2)
    assert not invoked.is_set()
    assert not cancelled.is_set()
    assert not any(event["event_state"] == "dispatched"
                   for event in harness.store.audit_events())


def test_worker_guards_and_provider_keep_callers_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving the final guard preserves authenticated ambient context."""
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    marker = contextvars.ContextVar("broker-boundary-test", default="missing")
    observed = []
    original = adapter.recheck_effect_boundary

    def check(*args) -> None:
        observed.append(("authority", marker.get()))
        original(*args)

    monkeypatch.setattr(adapter, "recheck_effect_boundary", check)
    prepared = broker.prepare(_frame(), _context(harness))
    backend = broker._backends.select(prepared.binding, production=True)

    def invoke(envelope) -> ProviderOutcome:
        observed.append(("provider", marker.get()))
        return ProviderOutcome({"ok": True})

    monkeypatch.setattr(backend, "invoke", invoke)
    token = marker.set("authenticated-caller")
    try:
        result = broker.invoke_prepared(
            prepared.to_snapshot(), _context(harness), harness.scope.to_dict(),
            execute_not_after_wall=2_000.0, wall_clock=lambda: 1_000.0,
            before_dispatch=lambda: observed.append(("pending", marker.get())),
        )
    finally:
        marker.reset(token)
        broker.close()
    assert result == {"ok": True}
    assert observed == [("authority", "authenticated-caller"),
                        ("pending", "authenticated-caller"),
                        ("provider", "authenticated-caller")]


def test_parent_cancellation_while_queued_never_enters_provider() -> None:
    """The shared cancel signal fences queued work without ambiguous effects."""
    from tobkiri_host.errors import RequestCancellationRequestedError
    from tests.test_tobkiri_host_execution_integration import (
        context, frame, make_broker,
    )

    fixture = make_broker(timeout_ms=10000)
    cancelled = Event()
    with _blocked_worker(fixture.broker) as (_release, submitted):
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(
                fixture.broker.invoke, frame(), context(), effect_scope={},
                parent_cancellation=cancelled,
            )
            assert submitted.wait(2)
            cancelled.set()
            with pytest.raises(RequestCancellationRequestedError):
                result.result(timeout=2)
            assert fixture.admission.released
    assert fixture.backend.invocations == 0
    assert fixture.backend.cancelled == []
    assert "authority_effect_recheck" not in fixture.events
    assert "audit_dispatched" not in fixture.events
    assert fixture.audit.failures == [("provider_failed", False)]


def test_parent_cancellation_during_worker_guard_retains_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running guard retains its reservation but cannot later start effects."""
    from tobkiri_host.errors import RequestCancellationRequestedError
    from tests.test_tobkiri_host_execution_integration import (
        context, frame, make_broker,
    )

    fixture = make_broker(timeout_ms=10000)
    cancelled, entered, release_guard, resources_released = (
        Event(), Event(), Event(), Event()
    )
    original_release = fixture.admission.release

    def guard(*args) -> None:
        entered.set()
        assert release_guard.wait(5)

    def release(ticket) -> None:
        original_release(ticket)
        resources_released.set()

    monkeypatch.setattr(fixture.authority, "recheck_effect_boundary", guard)
    monkeypatch.setattr(fixture.admission, "release", release)
    try:
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(
                fixture.broker.invoke, frame(), context(), effect_scope={},
                parent_cancellation=cancelled,
            )
            assert entered.wait(2)
            cancelled.set()
            with pytest.raises(RequestCancellationRequestedError):
                result.result(timeout=2)
            assert not fixture.admission.released
            assert fixture.backend.invocations == 0
            assert fixture.backend.cancelled == []
            assert fixture.audit.failures == [("provider_failed", False)]
            release_guard.set()
            assert resources_released.wait(2)
        assert fixture.backend.invocations == 0
        assert fixture.events.count("reservation_released") == 1
    finally:
        release_guard.set()
        fixture.broker.close()


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
def test_started_privileged_effect_is_reconciled_after_interruption(
    monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    """A started OS effect stays uncertain while cancellation is outstanding."""
    from tobkiri_host.effects import EffectDisposition
    from tobkiri_host.errors import AmbiguousEffectError
    from tobkiri_host.models import EffectClass
    from tests.test_tobkiri_host_execution_integration import (
        context, frame, make_broker,
    )

    fixture = make_broker(effect=EffectClass.PRIVILEGED, timeout_ms=10000)
    entered, finish, cancelled, released = Event(), Event(), Event(), Event()
    clock = [10.0]
    prepared = replace(
        fixture.broker.prepare(frame(), context()), deadline_monotonic=20.0,
    )
    original_release = fixture.admission.release

    def invoke(envelope) -> ProviderOutcome:
        fixture.backend.invocations += 1
        entered.set()
        assert finish.wait(5)
        return ProviderOutcome(None, EffectDisposition.UNKNOWN)

    def release(ticket) -> None:
        original_release(ticket)
        released.set()

    monkeypatch.setattr(fixture.backend, "invoke", invoke)
    monkeypatch.setattr(fixture.admission, "release", release)
    try:
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(
                fixture.broker._execute_prepared, prepared, context(),
                effect_scope={}, monotonic_clock=lambda: clock[0],
                before_dispatch=None, cancellation_requested=cancelled,
            )
            assert entered.wait(2)
            if interruption == "timeout":
                clock[0] = 21.0
            else:
                cancelled.set()
            with pytest.raises(AmbiguousEffectError) as raised:
                result.result(timeout=2)
            record = fixture.reconciliation.get(raised.value.reconciliation_id)
            assert record.status == "needs_reconciliation"
            assert record.request_id == "request-1"
            assert record.idempotency_key == frame().idempotency_key
            assert fixture.backend.invocations == 1
            assert fixture.backend.cancelled == ["request-1"]
            assert fixture.authority.fenced == ["request-1"]
            assert fixture.audit.failures == [("ambiguous_effect", True)]
            assert not fixture.admission.released
            assert "audit_committed" not in fixture.events
            finish.set()
            assert released.wait(2)
        assert fixture.backend.invocations == 1
        assert fixture.events.count("reservation_released") == 1
    finally:
        finish.set()
        fixture.broker.close()
