"""Request-local Host references follow exact Provider resource drain."""

from __future__ import annotations

from dataclasses import replace
from threading import Event
import time
from typing import Any

import pytest

from tests.test_tobkiri_host_execution_integration import context, frame, make_broker
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import AuthorizationError, RequestTimedOutError
from tobkiri_host.models import EffectClass, InvocationFrame
from tobkiri_host.resource_drain import RequestResourceDrain
from tobkiri_host.runtime import V4DispatchSession


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("failure", ["none", "guard", "schema", "materialization"])
def test_owner_release_runs_once_after_resources_or_unadmitted_failure(
    prepared: bool, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-admission and admitted errors use the same one-shot ownership gate."""
    fixture = make_broker()
    snapshot = fixture.broker.prepare(frame(), context()).to_snapshot()
    calls: list[str] = []

    def guard() -> None:
        if failure == "guard":
            raise AuthorizationError("parent unavailable")

    def materialize(*_args: Any) -> Any:
        raise RuntimeError("materialization failed")

    if failure == "materialization":
        monkeypatch.setattr(fixture.backend, "materialize", materialize)

    def run() -> Any:
        options = {
            "execution_guard": guard,
            "on_resources_drained": lambda: calls.append("owner"),
        }
        if prepared:
            invalid = replace(snapshot, request_digest="sha256:" + "0" * 64)
            return fixture.broker.invoke_prepared(
                invalid if failure == "schema" else snapshot,
                context(),
                {},
                execute_not_after_wall=time.time() + 10,
                **options,
            )
        invalid_frame = InvocationFrame(
            contract_id=frame().contract_id,
            version_range=frame().version_range,
            operation_id=frame().operation_id,
            payload={"invalid": True},
        )
        return fixture.broker.invoke(
            invalid_frame if failure == "schema" else frame(),
            context(),
            effect_scope={},
            **options,
        )

    try:
        if failure == "none":
            assert run() == {"delivered": True}
        else:
            with pytest.raises(Exception):
                run()
        assert calls == ["owner"]
        assert not fixture.broker.has_undrained_requests()
        fixture.broker.retry_resource_drains()
        assert calls == ["owner"]
        if failure in {"none", "materialization"}:
            assert fixture.admission.released
    finally:
        fixture.broker.close()


def test_timeout_retains_owner_until_exact_future_really_exits() -> None:
    """Returning timeout and requesting cancellation are not resource drain."""
    fixture = make_broker(effect=EffectClass.READ, timeout_ms=40)
    entered, release, drained = Event(), Event(), Event()

    def invoke(_request: Any) -> ProviderOutcome:
        entered.set()
        assert release.wait(3)
        return ProviderOutcome({"delivered": True})

    fixture.backend.invoke = invoke
    try:
        with pytest.raises(RequestTimedOutError):
            fixture.broker.invoke(
                frame(), context(), effect_scope={}, on_resources_drained=drained.set
            )
        assert entered.is_set()
        assert not drained.is_set()
        assert not fixture.admission.released
        assert fixture.broker.has_undrained_requests()
        fixture.broker.retry_resource_drains()
        assert not drained.is_set()
        release.set()
        assert drained.wait(2)
        assert fixture.admission.released
        assert not fixture.broker.has_undrained_requests()
    finally:
        release.set()
        fixture.broker.close()


def test_owner_cleanup_failure_is_visible_and_retry_does_not_release_twice() -> None:
    """A failed Host cleanup retains Authority and a retryable exact callback."""
    fixture = make_broker()
    attempts: list[str] = []

    def cleanup() -> None:
        attempts.append("owner")
        if len(attempts) < 3:
            raise RuntimeError("owned cleanup failed")

    try:
        with pytest.raises(RuntimeError, match="owned cleanup"):
            fixture.broker.invoke(
                frame(), context(), effect_scope={}, on_resources_drained=cleanup
            )
        assert fixture.admission.released
        assert fixture.broker.has_undrained_requests()
        with pytest.raises(RuntimeError, match="cleanup remains pending"):
            fixture.broker.retry_resource_drains()
        fixture.broker.retry_resource_drains()
        fixture.broker.retry_resource_drains()
        assert attempts == ["owner"] * 3
        assert fixture.events.count("reservation_released") == 1
        assert not fixture.broker.has_undrained_requests()
    finally:
        fixture.broker.close()


def test_unadmitted_dispatch_context_error_uses_broker_cleanup_ledger() -> None:
    """Session context construction cannot orphan a retained parent reference."""
    fixture = make_broker()
    calls: list[str] = []

    def context_for(*_args: Any) -> Any:
        raise AuthorizationError("context unavailable")

    dispatch = V4DispatchSession(
        broker=fixture.broker,
        context_for=context_for,
        effect_scope_for=lambda *_args: {},
        providers={},
        profile_id="profile-1",
        plan_digest="plan",
        profile_revision="revision",
        activation_id="activation-1",
    )
    try:
        with pytest.raises(AuthorizationError, match="context unavailable"):
            dispatch.invoke(
                frame().contract_id,
                frame().operation_id,
                frame().payload,
                on_resources_drained=lambda: calls.append("owner"),
            )
        assert calls == ["owner"]
        assert not fixture.broker.has_undrained_requests()
    finally:
        fixture.broker.close()


def test_retry_never_repeats_successful_resource_stage() -> None:
    """Owner failures preserve the completed resource stage and finite handle."""
    calls: list[str] = []
    attempts = 0

    def owner() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry")
        calls.append("owner")

    drain = RequestResourceDrain(owner)
    drain.handoff(lambda: calls.append("resources"))
    drain.finish_unadmitted()
    assert calls == []
    with pytest.raises(RuntimeError):
        drain.run()
    drain.run()
    drain.run()
    assert calls == ["resources", "owner"]
