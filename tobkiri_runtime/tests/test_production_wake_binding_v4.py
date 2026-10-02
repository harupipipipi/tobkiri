"""Selected-operation fixtures with the real canonical Broker and audit kernel."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.captured_wake_v4 import CapturedWakeDeclarationV4, LateBoundWakePortV4
from core_runtime.authority.v4 import GrantLifetime
from core_runtime.production_wake_binding_v4 import bind_production_wake_v4
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_captured_wake_v4 import _Adapter
from tests.test_tobkiri_host_authority_v4_adapter import _adapter, _broker, _context
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.runtime import V4DispatchSession


def test_real_broker_wakes_have_fresh_clock_sessions_and_committed_audit(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    authority = _adapter(harness)
    broker = _broker(harness, authority, ProviderOutcome({"status": "ok"}))
    calls = []
    clock = [1000.0, 50.0]

    @contextmanager
    def scope(occurrence: str) -> Any:
        number = len(calls) + 1
        session = f"host-clock-{number}"
        caller_domain = replace(
            harness.caller_domain,
            domain_id=f"domain-clock-{number}",
            process_identity=f"host-clock-{number}",
        )
        harness.kernel.register_execution_domain(
            caller_domain,
            session_id=session,
            channel_digest=harness.caller_domain.authenticated_channel_digest,
            principal=harness.caller,
        )
        context = replace(
            _context(harness, request_id=f"wake-request-{number}"),
            caller_session_id=session,
            caller_domain_id=caller_domain.domain_id,
        )
        calls.append((occurrence, context))
        yield context

    port = LateBoundWakePortV4()
    driver = bind_production_wake_v4(
        port=port,
        declaration=CapturedWakeDeclarationV4("host.http", "invoke", {"operation": "tick"}, 1000),
        owner_principal_id=harness.caller.principal_id,
        target_principal_id=harness.target.principal_id,
        state_path=tmp_path / "wake.sqlite3",
        identity={
            "profile_id": "profile-1",
            "activation_digest": _context(harness).activation_digest,
            "security_epoch": 1,
            "plan_digest": _context(harness).plan_digest,
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
    assert port.status()["armed"] is False
    driver.arm(5000)
    clock[:] = [1001.0, 51.0]
    driver.deliver_due()
    assert driver.status()["armed"] is True
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 1)
    states = [event["event_state"] for event in harness.store.audit_events()]
    # Fresh Host domain registrations also append committed audit records.
    assert states.count("reserved") == states.count("dispatched") == 1
    assert "committed" in states
    clock[:] = [1002.0, 52.0]
    driver.deliver_due()
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 2)
    contexts = [context for _, context in calls]
    assert len({context.request_id for context in contexts}) == len(contexts)
    assert all(
        context.caller_principal.value == harness.caller.principal_id for context in contexts
    )
    assert all(context.caller_session_id.startswith("host-clock-") for context in contexts)
    driver.close()
    broker.close()


@pytest.mark.parametrize("status", ["accepted", "running", "unknown", "approval_required"])
def test_nonterminal_outcome_unarms_even_after_broker_commits(tmp_path: Path, status: str) -> None:
    from tests.test_captured_wake_v4 import _driver

    clock, calls = [100.0, 5.0], []
    driver = _driver(tmp_path, clock, calls)
    driver._invoke = lambda occurrence, fence, cancel: {"status": status}
    driver.arm(5000)
    clock[:] = [101.0, 6.0]
    driver.deliver_due()
    assert driver.status()["armed"] is False
    assert driver.status()["reason"] == "wake_execution_unavailable"
    driver.close()


def test_state_ancestor_symlink_is_rejected_before_opening_database(tmp_path: Path) -> None:
    from tests.test_captured_wake_v4 import _driver

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(PermissionError, match="ancestor"):
        _driver(alias, [100.0, 5.0], [])
    assert not (real / "wake.sqlite3").exists()


def test_sources_are_fenced_before_broker_shutdown() -> None:
    events = []

    class Broker:
        def close(self) -> None:
            events.append("broker")

    session = V4DispatchSession(
        broker=Broker(),
        context_for=lambda: None,
        effect_scope_for=lambda: None,
        providers={},
        profile_id="fixture",
        plan_digest="plan",
        profile_revision="revision",
        activation_id="activation",
        frontend_entry_id="entry",
        selected_pack_closure=(),
        security_epoch=1,
        before_close_callbacks=(lambda: events.append("wake"),),
    )
    session.close()
    assert events == ["wake", "broker"]


def test_insufficient_recurring_grant_cannot_promote_one_shot_authority(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, grant_lifetime=GrantLifetime.ONE_SHOT, max_uses=1)
    approval = replace(harness.approval, approval_id="approval-recurring")
    recurring = replace(
        harness.grant,
        grant_id="grant-recurring",
        approval_id=approval.approval_id,
        lifetime=GrantLifetime.PERSISTENT_PROFILE,
        max_uses=None,
        scope=replace(harness.scope, quotas={"max_bytes": 512}),
    )
    harness.kernel.commit_approval_bundle(
        approval,
        provider_authorities=(replace(harness.provider, record_id="provider-recurring"),),
        grants=(recurring,),
    )
    authority = _adapter(harness)
    broker = _broker(harness, authority, ProviderOutcome({"status": "ok"}))

    @contextmanager
    def scope(occurrence: str) -> Any:
        yield _context(harness, request_id=f"wake-{occurrence}")

    driver = bind_production_wake_v4(
        port=LateBoundWakePortV4(),
        declaration=CapturedWakeDeclarationV4("host.http", "invoke", {"operation": "tick"}, 1000),
        owner_principal_id=harness.caller.principal_id,
        target_principal_id=harness.target.principal_id,
        state_path=tmp_path / "wake.sqlite3",
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
        wall_clock=harness.clock,
        monotonic_clock=lambda: 0.0,
        adapter_factory=_Adapter,
    )
    try:
        with pytest.raises(PermissionError, match="recurring wake Grant"):
            driver.arm(5000)
        assert driver.status()["armed"] is False
        assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)
        assert harness.store.grant_usage(recurring.grant_id) == (0, 0)
    finally:
        driver.close()
        broker.close()
