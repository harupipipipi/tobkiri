"""Host ancestry checks must survive queueing on every backend path."""

from __future__ import annotations

from concurrent.futures import Future
from threading import Event
import time
from typing import Any, Callable

import pytest

from tests.test_tobkiri_host_execution_integration import context, frame, make_broker
from tobkiri_host.broker import PreparedInvocationSnapshot
from tobkiri_host.errors import (
    AuthorizationError,
    ProviderExecutionError,
    RequestCancellationRequestedError,
)


@pytest.mark.parametrize("prepared", [False, True])
def test_revoked_parent_is_rejected_before_admission(prepared: bool) -> None:
    """A retained Host client cannot admit work under a terminal ancestor."""

    fixture = make_broker()
    snapshot = fixture.broker.prepare(frame(), context()).to_snapshot()

    def guard() -> None:
        raise AuthorizationError("parent invocation is terminal")

    try:
        with pytest.raises(AuthorizationError, match="terminal"):
            if prepared:
                fixture.broker.invoke_prepared(
                    snapshot,
                    context(),
                    {},
                    execute_not_after_wall=time.time() + 10,
                    execution_guard=guard,
                )
            else:
                fixture.broker.invoke(
                    frame(), context(), effect_scope={}, execution_guard=guard
                )
        assert fixture.events == []
        assert fixture.backend.invocations == 0
    finally:
        fixture.broker.close()


@pytest.mark.parametrize("prepared", [False, True])
def test_parent_revocation_while_queued_never_enters_backend(
    prepared: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revocation after dispatch markers still fences actual provider entry."""

    fixture = make_broker(timeout_ms=1000)
    snapshot = fixture.broker.prepare(frame(), context()).to_snapshot()
    revoked = Event()
    original_submit = fixture.broker._executor.submit
    marks: list[str] = []

    def guard() -> None:
        if revoked.is_set():
            raise AuthorizationError("parent invocation was revoked")

    def submit_after_revocation(
        function: Callable[..., Any], *arguments: Any, **keywords: Any
    ) -> Future[Any]:
        def queued_worker() -> Any:
            revoked.set()
            return function(*arguments, **keywords)

        return original_submit(queued_worker)

    monkeypatch.setattr(fixture.broker._executor, "submit", submit_after_revocation)
    try:
        with pytest.raises(ProviderExecutionError):
            if prepared:
                fixture.broker.invoke_prepared(
                    snapshot,
                    context(),
                    {},
                    execute_not_after_wall=time.time() + 10,
                    execution_guard=guard,
                    before_dispatch=lambda: marks.append("dispatch"),
                )
            else:
                fixture.broker.invoke(
                    frame(),
                    context(),
                    effect_scope={},
                    execution_guard=guard,
                    before_dispatch=lambda: marks.append("dispatch"),
                )
        assert marks == ["dispatch"]
        assert fixture.backend.invocations == 0
        assert fixture.audit.failures == [("provider_failed", False)]
        assert fixture.authority.fenced == ["request-1"]
        assert fixture.admission.released
    finally:
        fixture.broker.close()


def test_prepared_cancellation_remains_the_actual_backend_signal() -> None:
    """A durable Workflow child receives the exact owned cancellation Event."""

    fixture = make_broker(timeout_ms=1000)
    snapshot: PreparedInvocationSnapshot = fixture.broker.prepare(
        frame(), context()
    ).to_snapshot()
    cancellation = Event()
    original_invoke = fixture.backend.invoke
    observed: list[Event] = []

    def invoke(request: Any) -> Any:
        observed.append(request.cancellation_requested)
        return original_invoke(request)

    fixture.backend.invoke = invoke
    try:
        assert fixture.broker.invoke_prepared(
            snapshot,
            context(),
            {},
            execute_not_after_wall=time.time() + 10,
            cancellation_requested=cancellation,
        ) == {"delivered": True}
        assert observed == [cancellation]
    finally:
        fixture.broker.close()


def test_prepared_child_preserves_the_current_parent_deadline() -> None:
    """Resume may use a fresh parent, but cannot extend its execution budget."""

    fixture = make_broker(timeout_ms=1000)
    snapshot = fixture.broker.prepare(frame(), context()).to_snapshot()
    original_invoke = fixture.backend.invoke
    observed: list[float] = []
    parent_deadline = time.monotonic() + 0.5

    def invoke(request: Any) -> Any:
        observed.append(request.deadline_monotonic)
        return original_invoke(request)

    fixture.backend.invoke = invoke
    try:
        fixture.broker.invoke_prepared(
            snapshot,
            context(),
            {},
            execute_not_after_wall=time.time() + 10,
            parent_deadline_monotonic=parent_deadline,
        )
        assert observed == [parent_deadline]
    finally:
        fixture.broker.close()


def test_cancelled_prepared_parent_has_no_admission_or_provider_effects() -> None:
    """Approval resume cannot dispatch after its fresh parent was cancelled."""

    fixture = make_broker()
    snapshot = fixture.broker.prepare(frame(), context()).to_snapshot()
    cancellation = Event()
    cancellation.set()
    try:
        with pytest.raises(RequestCancellationRequestedError):
            fixture.broker.invoke_prepared(
                snapshot,
                context(),
                {},
                execute_not_after_wall=time.time() + 10,
                cancellation_requested=cancellation,
            )
        assert fixture.events == []
        assert fixture.backend.invocations == 0
    finally:
        fixture.broker.close()
