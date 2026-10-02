"""Injected-clock, isolated finite wake/lease tests without production claims."""

from dataclasses import replace
from pathlib import Path
import threading
from typing import Any

import pytest

from core_runtime.captured_wake_v4 import (
    CapturedWakeDeclarationV4,
    CapturedWakeDriverV4,
    _OccurrenceGate,
)
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.triggers import (
    TriggerDelivery,
    TriggerRegistration,
    WakeAdapterStatus,
    WakeRegistrationLease,
)


class _Adapter:
    status = WakeAdapterStatus("fixture", "host_process_only", True)

    def __init__(self, callback: Any):
        self.callback = callback
        self.leases: dict[str, Any] = {}
        self.revoked: list[str] = []

    def register(self, registration: TriggerRegistration) -> WakeRegistrationLease:
        lease = WakeRegistrationLease(
            "fixture-lease", registration.registration_id, registration.security_epoch
        )
        self.leases[registration.registration_id] = lease
        return lease

    def arm(self, lease: WakeRegistrationLease, occurrence: str, due: float) -> None:
        assert self.leases[lease.registration_id] == lease

    def revoke(self, lease: WakeRegistrationLease) -> None:
        self.revoked.append(lease.registration_id)


def _driver(
    tmp_path: Path,
    clock: list[float],
    calls: list[str],
    *,
    allowed: list[bool] | None = None,
    identity: str = "a",
    adapter: bool = True,
    failure: bool = False,
) -> CapturedWakeDriverV4:
    def check() -> None:
        if allowed is not None and not allowed[0]:
            raise PermissionError("grant revoked")

    def invoke(occurrence: str, fence: Any, cancellation: Any) -> dict[str, Any]:
        fence()
        assert not cancellation.is_set()
        calls.append(occurrence)
        if failure:
            raise PermissionError("actual Broker grant missing")
        return {"status": "ok"}

    return CapturedWakeDriverV4(
        state_path=tmp_path / "wake.sqlite3",
        declaration=CapturedWakeDeclarationV4(
            "example.action.v1", "example.tick", {"operation": "tick"}, 1000
        ),
        registration=TriggerRegistration(
            "clock",
            "example.action.v1",
            "example.tick",
            OpaqueAuthorityRef("target"),
            "activation",
            1,
        ),
        binding_identity={"activation": identity, "caller": "clock", "target": "target"},
        assert_authorized=check,
        invoke_fresh=invoke,
        current_epoch=lambda: 1,
        adapter_factory=_Adapter if adapter else None,
        wall_clock=lambda: clock[0],
        monotonic_clock=lambda: clock[1],
    )


def test_finite_wake_is_disabled_by_default_and_dispatches_on_injected_boundary(
    tmp_path: Path,
) -> None:
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    assert driver.status()["armed"] is False
    driver.arm(5000)
    driver.deliver_due()
    assert calls == []
    clock[:] = [101.0, 6.0]
    driver.deliver_due()
    assert calls == ["1:101000"]
    assert driver.status()["next_wake_at_ms"] == 102000
    driver.close()


def test_restart_reconstructs_monotonic_due_and_collapses_missed_periods(tmp_path: Path) -> None:
    clock, calls = [100.0, 5.0], []
    first = _driver(tmp_path, clock, calls)
    first.arm(10000)
    first.close()
    clock[:] = [106.0, 0.0]
    restarted = _driver(tmp_path, clock, calls)
    assert restarted.status()["armed"] is True
    restarted.deliver_due()
    assert calls == ["1:101000"]
    assert restarted.status()["next_wake_at_ms"] == 107000
    restarted.close()


def test_disarm_then_rearm_fences_old_generation(tmp_path: Path) -> None:
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    driver.arm(5000)
    driver.disarm()
    clock[:] = [101.0, 6.0]
    driver.deliver_due()
    assert calls == []
    driver.arm(5000)
    clock[:] = [102.0, 7.0]
    driver.deliver_due()
    assert calls == ["3:102000"]
    driver.close()


@pytest.mark.parametrize(
    "mode", ["missing_adapter", "grant_revoked", "expired", "binding_changed", "broker_failed"]
)
def test_missing_or_stale_edges_never_remain_armed(tmp_path: Path, mode: str) -> None:
    clock, calls, allowed = [100.0, 5.0], [], [True]
    driver = _driver(
        tmp_path,
        clock,
        calls,
        allowed=allowed,
        adapter=mode != "missing_adapter",
        failure=mode == "broker_failed",
    )
    if mode == "missing_adapter":
        with pytest.raises(PermissionError):
            driver.arm(5000)
    else:
        driver.arm(5000)
        if mode == "binding_changed":
            driver.close()
            driver = _driver(tmp_path, clock, calls, identity="different")
        else:
            if mode == "grant_revoked":
                allowed[0] = False
            clock[:] = [106.0 if mode == "expired" else 101.0, 6.0]
            driver.deliver_due()
    assert driver.status()["armed"] is False
    assert calls == (["1:101000"] if mode == "broker_failed" else [])
    driver.close()


def test_declaration_drift_fences_registered_source(tmp_path: Path) -> None:
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    driver.arm(5000)
    object.__setattr__(driver._declaration, "payload", {"operation": "different"})
    clock[:] = [101.0, 6.0]
    driver.deliver_due()
    assert calls == []
    assert driver.status()["armed"] is False
    driver.close()


def test_status_revalidates_current_grant_before_showing_armed(tmp_path: Path) -> None:
    allowed = [True]
    driver = _driver(tmp_path, [100.0, 5.0], [], allowed=allowed)
    driver.arm(5000)
    allowed[0] = False
    assert driver.status()["armed"] is False
    driver.close()


@pytest.mark.parametrize("mismatch", ["registration", "occurrence", "target", "epoch", "expiry"])
def test_wake_gate_rejects_other_occurrences_and_consumes_failed_tokens(mismatch: str) -> None:
    clock = [5.0]
    gate = _OccurrenceGate(lambda: None, lambda: clock[0])
    registration = TriggerRegistration(
        "clock", "example.action.v1", "example.tick", OpaqueAuthorityRef("target"), "activation", 1
    )
    lease = gate.issue_trigger_lease("clock", "occurrence", registration.target, 1)
    original = TriggerDelivery(registration, "occurrence", 1, lease)
    changed = original
    if mismatch == "registration":
        changed = replace(original, registration=replace(registration, registration_id="other"))
    elif mismatch == "occurrence":
        changed = replace(original, occurrence_id="other")
    elif mismatch == "target":
        changed = replace(
            original, registration=replace(registration, target=OpaqueAuthorityRef("other"))
        )
    elif mismatch == "epoch":
        changed = replace(original, registration=replace(registration, security_epoch=2))
    else:
        clock[0] = 35.0
    with pytest.raises(PermissionError):
        gate.consume(changed)
    with pytest.raises(PermissionError):
        gate.consume(original)


def test_wake_gate_is_one_shot_for_the_exact_occurrence() -> None:
    gate = _OccurrenceGate(lambda: None, lambda: 5.0)
    registration = TriggerRegistration(
        "clock", "example.action.v1", "example.tick", OpaqueAuthorityRef("target"), "activation", 1
    )
    lease = gate.issue_trigger_lease("clock", "occurrence", registration.target, 1)
    delivery = TriggerDelivery(registration, "occurrence", 1, lease)
    gate.consume(delivery)
    with pytest.raises(PermissionError):
        gate.consume(delivery)


@pytest.mark.parametrize("explicit_disarm", [False, True])
def test_rearm_cancels_old_delivery_without_disabling_new_generation(
    tmp_path: Path, explicit_disarm: bool
) -> None:
    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    entered, release = threading.Event(), threading.Event()
    cancellations: list[threading.Event] = []

    def invoke(occurrence: str, fence: Any, cancellation: threading.Event) -> dict[str, Any]:
        calls.append(occurrence)
        cancellations.append(cancellation)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5.0)
        fence()
        return {"status": "ok"}

    driver._invoke = invoke
    driver.arm(5000)
    clock[:] = [101.0, 6.0]
    worker = threading.Thread(target=driver.deliver_due)
    worker.start()
    try:
        assert entered.wait(5.0)
        if explicit_disarm:
            driver.disarm()
        driver.arm(5000)
        assert cancellations[0].is_set()
        release.set()
        worker.join(5.0)
        assert not worker.is_alive()
        assert driver.status()["armed"] is True
        clock[:] = [102.0, 7.0]
        driver.deliver_due()
        generation = 3 if explicit_disarm else 2
        assert calls == ["1:101000", f"{generation}:102000"]
        assert driver.status()["armed"] is True
    finally:
        release.set()
        worker.join(5.0)
        driver.close()
