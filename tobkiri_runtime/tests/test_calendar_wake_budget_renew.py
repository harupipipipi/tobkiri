"""Calendar budgets are trusted declarations; renew retains the in-flight gate."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import GrantLifetime
from core_runtime.captured_wake_v4 import CapturedWakeDeclarationV4, LateBoundWakePortV4
from core_runtime.production_wake_binding_v4 import bind_production_wake_v4
from ecosystem.rumi_scheduler_runtime_pack.runtime.clock import SchedulerClockFactoryV4
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_captured_wake_v4 import _Adapter, _driver
from tests.test_tobkiri_host_authority_v4_adapter import _adapter, _broker, _context
import tests.test_tobkiri_host_authority_v4_adapter as broker_fixture
from tobkiri_host.effects import ProviderOutcome
from tobkiri_protocol.canonical import canonical_digest


@pytest.mark.parametrize("invalid", [True, False, None, "300000", 0, -1, 300001, 1.5])
def test_invalid_trusted_timeout_is_rejected(invalid):
    with pytest.raises(ValueError, match="dispatch timeout"):
        CapturedWakeDeclarationV4("test", "tick", {}, 1000, invalid)


def test_default_old_clock_budget_and_identity_stay_unchanged():
    declaration = CapturedWakeDeclarationV4("test", "tick", {"operation": "tick"}, 1000)
    assert declaration.dispatch_timeout_ms == 30000
    assert declaration.digest == canonical_digest(
        {
            "contract_id": "test",
            "operation_id": "tick",
            "payload": {"operation": "tick"},
            "interval_ms": 1000,
        }
    )
    assert SchedulerClockFactoryV4.wake_declaration.dispatch_timeout_ms == 300000
    assert replace(declaration, dispatch_timeout_ms=300000).digest != declaration.digest
    assert (
        replace(declaration, payload={"operation": "different"}).digest
        != declaration.digest
    )


def production_driver(tmp_path, declaration, *, one_shot=False):
    harness = (
        _Harness(tmp_path, grant_lifetime=GrantLifetime.ONE_SHOT, max_uses=1)
        if one_shot
        else _Harness(tmp_path)
    )
    authority = _adapter(harness)
    broker = _broker(harness, authority, ProviderOutcome({"status": "ok"}))
    clock = [1000.0, 50.0]
    frames = []
    actual_prepare = broker.prepare

    def prepare(frame, context, *args, **kwargs):
        prepared = actual_prepare(frame, context, *args, **kwargs)
        frames.append((frame, prepared))
        return prepared

    broker.prepare = prepare

    @contextmanager
    def scope(occurrence):
        yield _context(harness, request_id=f"budget-{occurrence}")

    driver = bind_production_wake_v4(
        port=LateBoundWakePortV4(),
        declaration=declaration,
        owner_principal_id=harness.caller.principal_id,
        target_principal_id=harness.target.principal_id,
        state_path=tmp_path / "budget-wake.sqlite3",
        identity={
            "profile_id": "profile-1",
            "activation_digest": _context(harness).activation_digest,
            "security_epoch": 1,
        },
        broker=broker,
        authority=authority,
        authority_store=harness.store,
        context_scope=scope,
        effect_scope=lambda _: harness.scope.to_dict(),
        assert_current=lambda: None,
        wall_clock=lambda: clock[0],
        monotonic_clock=lambda: clock[1],
        adapter_factory=_Adapter,
    )
    return driver, broker, harness, clock, frames


@pytest.mark.parametrize("timeout", [30000, 300000])
def test_trusted_frame_budget_still_respects_signed_operation_ceiling(
    tmp_path, timeout
):
    # A payload hint cannot override the trusted declaration or signed operation.
    declaration = CapturedWakeDeclarationV4(
        "host.http",
        "invoke",
        {"operation": "tick", "timeout_ms": 999999},
        1000,
        dispatch_timeout_ms=timeout,
    )
    driver, broker, harness, clock, frames = production_driver(tmp_path, declaration)
    try:
        driver.arm(5000)
        assert frames
        frame, prepared = frames[-1]
        assert frame.timeout_ms == timeout
        assert prepared.timeout_ms == min(
            timeout, prepared.binding.operation.timeout_hard_max_ms
        )
        assert prepared.timeout_ms <= 60000
    finally:
        driver.close()
        broker.close()


def test_nondefault_budget_cannot_promote_one_shot_grant(tmp_path):
    declaration = CapturedWakeDeclarationV4(
        "host.http", "invoke", {"operation": "tick"}, 1000, 300000
    )
    driver, broker, harness, clock, frames = production_driver(
        tmp_path, declaration, one_shot=True
    )
    try:
        with pytest.raises(PermissionError, match="recurring wake Grant"):
            driver.arm(5000)
        assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)
        assert not driver.status()["armed"]
    finally:
        driver.close()
        broker.close()


def test_calendar_budget_reaches_300_seconds_only_on_signed_300_second_operation(
    tmp_path, monkeypatch
):
    original_artifact = broker_fixture._artifact

    def signed_long_operation(harness):
        artifact = original_artifact(harness)
        function = artifact.functions[0]
        operation = replace(
            function.operations[0],
            timeout_default_ms=300000,
            timeout_hard_max_ms=300000,
        )
        return replace(
            artifact, functions=(replace(function, operations=(operation,)),)
        )

    monkeypatch.setattr(broker_fixture, "_artifact", signed_long_operation)
    declaration = CapturedWakeDeclarationV4(
        "host.http", "invoke", {"operation": "tick"}, 1000, 300000
    )
    driver, broker, harness, clock, frames = production_driver(tmp_path, declaration)
    try:
        driver.arm(5000)
        assert frames[-1][1].timeout_ms == 300000
        clock[:] = [1001.0, 51.0]
        driver.deliver_due()
        assert driver.status()["armed"]
        assert harness.store.grant_usage(harness.grant.grant_id) == (0, 1)
        assert "committed" in [
            event["event_state"] for event in harness.store.audit_events()
        ]
    finally:
        driver.close()
        broker.close()


def test_finite_renew_preserves_generation_next_due_and_current_signal(tmp_path):
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    try:
        driver.arm(5000)
        before = driver._state()
        signal = driver._delivery_cancellation
        was_cancelled = signal.is_set()
        clock[:] = [103.0, 8.0]
        receipt = driver.renew(10000)
        after = driver._state()
        assert receipt["armed"] and receipt["expires_at_ms"] == 113000
        assert after[:3] == before[:3]
        assert after[4:] == before[4:]
        assert driver._delivery_cancellation is signal
        assert signal.is_set() == was_cancelled
    finally:
        driver.close()


def test_renew_requires_still_valid_actual_recurring_grant(tmp_path):
    declaration = CapturedWakeDeclarationV4(
        "host.http", "invoke", {"operation": "tick"}, 1000, 300000
    )
    driver, broker, harness, clock, frames = production_driver(tmp_path, declaration)
    try:
        driver.arm(5000)
        before = driver._state()
        harness.kernel.revoke(
            target_kind="grant",
            target_id=harness.grant.grant_id,
            reason="Calendar registration authority revoked",
        )
        with pytest.raises(PermissionError, match="recurring wake Grant"):
            driver.renew(10000)
        assert driver._state() == before
        assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)
    finally:
        driver.close()
        broker.close()


def test_self_renew_inside_live_delivery_does_not_fence_or_cancel_it(tmp_path):
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    driver.arm(5000)
    generation = driver._state()[1]

    def fresh_invoke(occurrence, fence, cancellation):
        before = driver._state()
        fence()
        receipt = driver.renew(10000)
        assert receipt["armed"]
        assert driver._state()[1:] == (
            before[1],
            before[2],
            114.0,
            before[4],
            before[5],
        )
        assert driver._delivery_cancellation is cancellation
        assert not cancellation.is_set()
        fence()
        calls.append(occurrence)
        return {"status": "ok"}

    driver._invoke = fresh_invoke
    try:
        clock[:] = [104.0, 9.0]
        driver.deliver_due()
        assert len(calls) == 1
        assert driver.status()["armed"]
        assert driver._state()[1] == generation
        assert driver._state()[5] == 0
        assert driver.status()["next_wake_at_ms"] == 105000
    finally:
        driver.close()


def test_renew_never_arms_disabled_or_expired_registration(tmp_path):
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    try:
        with pytest.raises(PermissionError, match="armed"):
            driver.renew(5000)
        driver.arm(5000)
        clock[:] = [106.0, 11.0]
        with pytest.raises(PermissionError, match="armed"):
            driver.renew(5000)
    finally:
        driver.close()


def test_renew_rechecks_recurring_authority_without_mutating_gate(tmp_path):
    clock, calls, allowed = [100.0, 5.0], [], [True]
    driver = _driver(tmp_path, clock, calls, allowed=allowed)
    try:
        driver.arm(5000)
        before = driver._state()
        signal = driver._delivery_cancellation
        allowed[0] = False
        with pytest.raises(PermissionError, match="revoked"):
            driver.renew(10000)
        assert driver._state() == before
        assert driver._delivery_cancellation is signal
    finally:
        driver.close()


def test_port_renew_requires_actual_selected_clock_principal(tmp_path):
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    port = LateBoundWakePortV4()
    port.bind(driver, "clock-owner")
    driver.arm(5000)
    try:
        foreign = SimpleNamespace(
            assert_current=lambda: None,
            envelope=SimpleNamespace(target_principal=SimpleNamespace(value="foreign")),
        )
        with pytest.raises(PermissionError, match="owner"):
            port.renew(foreign, 10000)
        current = SimpleNamespace(
            assert_current=lambda: None,
            envelope=SimpleNamespace(
                target_principal=SimpleNamespace(value="clock-owner")
            ),
        )
        assert port.renew(current, 10000)["expires_at_ms"] == 110000
    finally:
        driver.close()
