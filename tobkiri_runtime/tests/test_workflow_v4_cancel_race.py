"""Deterministic regressions for the stop-before/after-dispatch races.

The execute side drives the real ``HostAttemptInvokerV4`` against a fake
invocation whose cancellation binding mirrors ``OwnedCancellationHandles``
semantics (tracked scope, ``active_for``, ``request`` +
``wait_for_verified_drain``); the stop side runs ``cancel_run`` on a second
engine bound to the real ``HostStopAttemptInvokerV4`` — the same store,
the same barrier-controlled binding — matching the production
execute/stop-role split.

Windows covered without sleeps:

* stop lands after the durable RUNNING transition but before the invoker
  registers its tracked child — the durable cancellation fence must stop
  dispatch; ``active_for == False`` is not proof the attempt is drained;
* the Provider returned and track exited but the engine has not stored the
  outcome yet — Stop remains unconfirmed and cannot erase a real effect;
  the executor may subsequently commit its authenticated outcome;
* stop lands between the post-invoke RUNNING read and the durable terminal
  transition — the conflict must reconcile to evidence, never crash;
* execute commits SUCCEEDED while ``cancel_run`` is between its pre-drain
  read and the cancellation transition — the committed truth stands and
  the run still cancels.

The mirrored case — stop signalling a genuinely tracked in-flight child —
still waits for the verified drain observation before the cancel records.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.workflow_v4.engine import WorkflowEngineV4
from core_runtime.workflow_v4.host_adapters import (
    HostAttemptInvokerV4,
    HostStopAttemptInvokerV4,
)
from core_runtime.workflow_v4.models import (
    ApprovalState,
    DefinitionState,
    DispatchAuthority,
    InvocationOutcome,
    RunState,
    StepAttemptState,
    WorkflowCancellationUnconfirmed,
    WorkflowDenied,
    digest,
)
from core_runtime.workflow_v4.store import WorkflowStoreV4
from tests.test_workflow_v4 import (
    Authority,
    Catalog,
    Validator,
    definition,
)


class _FakeEnvelope:
    """Stop invocations read only ``deadline_monotonic`` from it."""

    deadline_monotonic = 1_000.0


class _WindowBinding:
    """Minimal cancellation binding with a barrier inside registration."""

    def __init__(self) -> None:
        self.active = ""
        self.requested: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.drain_waits = 0

    @contextmanager
    def track(self, reference: str) -> Iterator[None]:
        self.entered.set()
        assert self.release.wait(10)
        self.active = reference
        try:
            yield
        finally:
            self.active = ""

    def active_for(self, reference: str) -> bool:
        return self.active == reference

    def can_request(self, reference: str) -> bool:
        return self.active == reference

    def request(self, reference: str) -> object:
        self.requested.append(reference)

        class _Observation:
            def __init__(self, binding: _WindowBinding) -> None:
                self._binding = binding

            def wait_for_verified_drain(self, deadline: float) -> bool:
                del deadline
                self._binding.drain_waits += 1
                return True

        return _Observation(self)


class _WindowInvocation:
    """Fake ``HostProviderInvocationContextV4`` for the real adapters."""

    def __init__(
        self,
        binding: _WindowBinding,
        *,
        result: Mapping[str, Any] | None = None,
        dispatch_error: BaseException | None = None,
        pause_dispatch: bool = True,
    ) -> None:
        self._binding = binding
        self._result = {"ok": True} if result is None else dict(result)
        self._dispatch_error = dispatch_error
        self._pause_dispatch = pause_dispatch
        self.envelope = _FakeEnvelope()
        self.dispatched: list[str] = []
        self.in_dispatch = threading.Event()
        self.release_dispatch = threading.Event()

    def assert_current(self) -> None:
        return None

    @property
    def cancellation(self) -> _WindowBinding:
        return self._binding

    def dispatch_bounded(self, **kwargs: Any) -> Mapping[str, Any]:
        self.dispatched.append(str(kwargs["idempotency_key"]))
        if self._pause_dispatch:
            self.in_dispatch.set()
            assert self.release_dispatch.wait(10)
        if self._dispatch_error is not None:
            raise self._dispatch_error
        return dict(self._result)


def _engines(
    tmp_path: Path,
    invocation: _WindowInvocation,
    binding: _WindowBinding,
    *,
    store: WorkflowStoreV4 | None = None,
    authority: Authority | None = None,
) -> tuple[WorkflowEngineV4, WorkflowEngineV4]:
    """One execute engine plus one stop engine over the shared store."""

    shared = store or WorkflowStoreV4(
        tmp_path / "workflow.sqlite3", clock=lambda: 100.0
    )
    execute = WorkflowEngineV4(
        store=shared,
        catalog=Catalog(),
        authority=authority or Authority(),
        invoker=HostAttemptInvokerV4(
            invocation=invocation,
            allowed_contract_ids=frozenset({"example.echo.v1"}),
            consumer_pack_id="tobkiri_workflow_pack",
        ),
        validator=Validator(),
        clock=lambda: 100.0,
    )
    stop_invocation = _WindowInvocation(binding)
    stop = WorkflowEngineV4(
        store=shared,
        catalog=Catalog(),
        authority=Authority(),
        invoker=HostStopAttemptInvokerV4(invocation=stop_invocation),
        validator=Validator(),
        clock=lambda: 100.0,
    )
    return execute, stop


def _publish_run(
    engine: WorkflowEngineV4,
    run_id: str,
    steps: int = 1,
    *,
    independent: bool = False,
) -> None:
    document = definition()
    if steps == 2:
        document["steps"].append(
            {
                "id": "echo_two",
                "request": {
                    "contract_id": "example.echo.v1",
                    "contract_revision_digest": (
                        "sha256:" + "1" * 64
                    ),
                    "operation_id": "echo",
                    "function_principal_id": "example.echo.provider",
                    "input": {"message": "hi2"},
                    **({} if independent else {"depends_on": ["echo"]}),
                },
                "retry": {"max_attempts": 1, "backoff_ms": 0},
            }
        )
    engine.store.create_definition("workflow.race", document)
    created = engine.store.get_definition("workflow.race")
    compiled = engine.compile_preview(created["document"])
    engine.store.transition_definition(
        "workflow.race",
        if_match=created["etag"],
        expected=DefinitionState.DRAFT,
        target=DefinitionState.PUBLISHED,
        compiled=compiled,
    )
    engine.start_run(
        definition_id="workflow.race", inputs={"message": "hi"}, run_id=run_id
    )


def _run_execute(
    engine: WorkflowEngineV4, run_id: str, step_id: str = "echo"
) -> tuple[threading.Event, list[BaseException], threading.Thread]:
    done = threading.Event()
    error: list[BaseException] = []

    def execute() -> None:
        try:
            engine.execute_step(run_id, step_id)
        except BaseException as exc:  # noqa: BLE001 - surface any failure
            error.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=execute)
    worker.start()
    return done, error, worker


def test_stop_before_track_blocks_dispatch_and_never_claims_effect(
    tmp_path: Path,
) -> None:
    """Cancel before track registration: fence stops dispatch, zero effect."""

    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, stop_engine = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "run-race-before")

    done, error, worker = _run_execute(engine, "run-race-before")
    assert binding.entered.wait(10)
    # The attempt is durably RUNNING but the child is not yet registered, so
    # active_for is False — the durable fence must carry the stop instead.
    stop_engine.cancel_run("run-race-before")
    binding.release.set()
    assert done.wait(10)
    worker.join(10)
    assert not error

    attempt = engine.store.list_attempts("run-race-before")[-1]
    assert attempt["state"] == StepAttemptState.CANCELLED.value
    assert engine.store.get_run("run-race-before")["state"] == "cancelled"
    assert invocation.dispatched == []
    assert binding.requested == []
    assert attempt.get("outcome") in (None, {})


def test_stop_after_dispatch_preserves_later_committed_outcome(
    tmp_path: Path,
) -> None:
    """Cancel after Provider return: effect evidence wins over clean cancel."""

    binding = _WindowBinding()
    binding.release.set()  # no barrier: track registers immediately
    invocation = _WindowInvocation(binding)
    invocation.release_dispatch.set()  # dispatch completes; store read pauses
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0)
    engine, stop_engine = _engines(tmp_path, invocation, binding, store=store)
    _publish_run(engine, "run-race-after")

    # Barrier: pause the engine between invoke() returning and the durable
    # outcome write by intercepting the post-invoke get_attempt read.
    original_get_attempt = store.get_attempt
    outcome_returned = threading.Event()
    release_write = threading.Event()
    gate = threading.Event()

    def gated_get_attempt(attempt_id: str) -> dict[str, Any]:
        if invocation.dispatched and not gate.is_set():
            gate.set()
            outcome_returned.set()
            assert release_write.wait(10)
        return original_get_attempt(attempt_id)

    store.get_attempt = gated_get_attempt  # type: ignore[method-assign]

    done, error, worker = _run_execute(engine, "run-race-after")
    assert outcome_returned.wait(10)
    # The Provider already returned and track exited; no live proof remains.
    # Stop records intent but must not declare the attempt cancelled.
    with pytest.raises(WorkflowCancellationUnconfirmed):
        stop_engine.cancel_run("run-race-after")
    assert store.get_run("run-race-after")["cancel_requested"] is True
    release_write.set()
    assert done.wait(10)
    worker.join(10)
    assert not error

    attempt = engine.store.list_attempts("run-race-after")[-1]
    assert attempt["state"] == StepAttemptState.SUCCEEDED.value
    # Unconfirmed Stop cannot erase the subsequently committed real outcome.
    assert attempt["outcome"] == {"ok": True}
    assert attempt.get("outcome_digest")
    assert engine.store.get_run("run-race-after")["state"] == "succeeded"


def test_stop_during_tracked_dispatch_signals_and_waits_for_drain(
    tmp_path: Path,
) -> None:
    """The mirrored side: a tracked child is signalled and drain-verified."""

    binding = _WindowBinding()
    binding.release.set()  # track registers immediately; dispatch pauses
    invocation = _WindowInvocation(binding)
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0)
    engine, stop_engine = _engines(tmp_path, invocation, binding, store=store)
    _publish_run(engine, "run-race-tracked")

    done, error, worker = _run_execute(engine, "run-race-tracked")
    assert binding.entered.wait(10)
    assert invocation.in_dispatch.wait(10)
    # The child is tracked and mid-dispatch: stop must signal it and verify
    # drain before the cancel records — never claim cancelled before proof.
    stop_engine.cancel_run("run-race-tracked")
    assert binding.requested
    assert binding.drain_waits == 1
    invocation.release_dispatch.set()
    assert done.wait(10)
    worker.join(10)
    assert not error

    attempt = engine.store.list_attempts("run-race-tracked")[-1]
    assert attempt["state"] == StepAttemptState.CANCELLED.value
    # Dispatch ran before stop signalled: the outcome is real, so the run
    # escalates to reconciliation instead of pretending no effect happened.
    assert attempt["outcome"] == {"ok": True}
    assert engine.store.get_run("run-race-tracked")["state"] == (
        "needs_reconciliation"
    )


def test_unconfirmed_stop_during_outcome_write_keeps_evidence(tmp_path: Path) -> None:
    """Stop lands between the RUNNING read and the durable commit.

    The barrier is the real ``transition_attempt`` call: cancel runs
    synchronously inside it, wins the durable race, and the execute side's
    conflicting SUCCEEDED write must reconcile to outcome evidence rather
    than crash or silently lose the effect.
    """

    binding = _WindowBinding()
    binding.release.set()
    invocation = _WindowInvocation(binding, pause_dispatch=False)
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0)
    engine, stop_engine = _engines(tmp_path, invocation, binding, store=store)
    _publish_run(engine, "run-race-finalize")

    original = store.transition_attempt
    fired = threading.Event()

    def intercepted(
        attempt_id: str,
        *,
        expected: set[StepAttemptState],
        target: StepAttemptState,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if target is StepAttemptState.SUCCEEDED and not fired.is_set():
            fired.set()
            # Stop wins the window between the post-invoke RUNNING read and
            # this durable transition.
            with pytest.raises(WorkflowCancellationUnconfirmed):
                stop_engine.cancel_run("run-race-finalize")
        return original(
            attempt_id, expected=expected, target=target, updates=updates
        )

    store.transition_attempt = intercepted  # type: ignore[method-assign]

    engine.execute_step("run-race-finalize", "echo")

    attempt = engine.store.list_attempts("run-race-finalize")[-1]
    assert attempt["state"] == StepAttemptState.SUCCEEDED.value
    assert attempt["outcome"] == {"ok": True}
    assert attempt.get("outcome_digest")
    assert engine.store.get_run("run-race-finalize")["state"] == "succeeded"


def test_execute_commit_winning_during_cancel_keeps_truth(tmp_path: Path) -> None:
    """Execute commits SUCCEEDED while cancel is mid-drain: truth stands.

    ``cancel_run`` reads the RUNNING attempt, then the execute side commits
    SUCCEEDED before the cancellation transition lands — the conflicting
    CANCELLED write must keep the committed outcome, and the run still
    records the stop as cancelled.
    """

    binding = _WindowBinding()
    binding.release.set()
    invocation = _WindowInvocation(binding, pause_dispatch=False)
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0)
    engine, stop_engine = _engines(tmp_path, invocation, binding, store=store)
    # Two steps so the run can never reach SUCCEEDED mid-test: the cancel
    # owns the run transition deterministically.
    _publish_run(engine, "run-race-commit", steps=2)

    # Hold the execute side between invoke() returning and the outcome
    # write, exactly like the post-dispatch window above.
    original_get_attempt = store.get_attempt
    outcome_returned = threading.Event()
    release_write = threading.Event()
    gate = threading.Event()

    def gated_get_attempt(attempt_id: str) -> dict[str, Any]:
        if invocation.dispatched and not gate.is_set():
            gate.set()
            outcome_returned.set()
            assert release_write.wait(10)
        return original_get_attempt(attempt_id)

    store.get_attempt = gated_get_attempt  # type: ignore[method-assign]

    succeeded_committed = threading.Event()
    original_transition = store.transition_attempt

    def intercepted(
        attempt_id: str,
        *,
        expected: set[StepAttemptState],
        target: StepAttemptState,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = original_transition(
            attempt_id, expected=expected, target=target, updates=updates
        )
        if target is StepAttemptState.SUCCEEDED:
            succeeded_committed.set()
        return record

    store.transition_attempt = intercepted  # type: ignore[method-assign]

    def observe_after_commit(reference: str) -> bool:
        del reference
        release_write.set()
        assert succeeded_committed.wait(10)
        return False
    binding.active_for = observe_after_commit

    done, error, worker = _run_execute(engine, "run-race-commit")
    assert outcome_returned.wait(10)
    cancelled = stop_engine.cancel_run("run-race-commit")
    assert done.wait(10)
    worker.join(10)
    assert not error

    attempt = engine.store.list_attempts("run-race-commit")[-1]
    assert attempt["state"] == StepAttemptState.SUCCEEDED.value
    assert attempt["outcome"] == {"ok": True}
    assert cancelled["state"] == "cancelled"


def _dispatch_request() -> Mapping[str, Any]:
    return {
        "request_id": "req-narrow-1",
        "contract_id": "example.echo.v1",
        "operation_id": "echo",
        "input": {"message": "hi"},
        "idempotency_key": "key-1",
        "timeout_ms": 1000,
    }


def _dispatch_authority() -> DispatchAuthority:
    return DispatchAuthority(
        dispatch_token="token-1",
        reservation_id="reservation-1",
        request_digest="sha256:" + "2" * 64,
        security_epoch=7,
    )


def _invoker_for(invocation: _WindowInvocation) -> HostAttemptInvokerV4:
    return HostAttemptInvokerV4(
        invocation=invocation,
        allowed_contract_ids=frozenset({"example.echo.v1"}),
        consumer_pack_id="tobkiri_workflow_pack",
    )


def test_dispatch_value_error_is_ambiguous_not_undispatched() -> None:
    """ValueError inside dispatch may follow a real Provider side effect."""

    binding = _WindowBinding()
    binding.release.set()
    invocation = _WindowInvocation(
        binding,
        dispatch_error=ValueError("mid-dispatch failure"),
        pause_dispatch=False,
    )
    outcome = _invoker_for(invocation).invoke(
        _dispatch_request(),
        authority=_dispatch_authority(),
    )
    assert invocation.dispatched  # dispatch was attempted
    assert outcome.dispatched
    assert outcome.ambiguous_effect
    assert outcome.error_code == "provider_dispatch_failed"


def test_dispatch_permission_error_is_ambiguous_not_undispatched() -> None:
    binding = _WindowBinding()
    binding.release.set()
    invocation = _WindowInvocation(
        binding,
        dispatch_error=PermissionError("post-effect denial"),
        pause_dispatch=False,
    )
    outcome = _invoker_for(invocation).invoke(
        _dispatch_request(),
        authority=_dispatch_authority(),
    )
    assert outcome.dispatched
    assert outcome.ambiguous_effect
    assert outcome.error_code == "provider_dispatch_failed"


class _FailingEntryBinding:
    """Binding whose tracked scope entry itself is denied."""

    def track(self, reference: str) -> object:
        del reference

        class _Scope:
            def __enter__(self) -> None:
                raise PermissionError("scope unavailable")

            def __exit__(self, *args: object) -> None:
                del args

        return _Scope()

    def active_for(self, reference: str) -> bool:
        del reference
        return False

    def can_request(self, reference: str) -> bool:
        del reference
        return False

    def request(self, reference: str) -> object:
        raise AssertionError("never signalled")


class _EntryInvocation:
    def __init__(self, binding: _FailingEntryBinding) -> None:
        self._binding = binding

    def assert_current(self) -> None:
        return None

    @property
    def cancellation(self) -> _FailingEntryBinding:
        return self._binding

    def dispatch_bounded(self, **kwargs: Any) -> Mapping[str, Any]:
        raise AssertionError("dispatch must not run")


def test_track_entry_failure_is_provably_undispatched() -> None:
    """Scope entry failing is provable pre-dispatch validation."""

    binding = _FailingEntryBinding()
    invocation = _EntryInvocation(binding)
    outcome = _invoker_for(invocation).invoke(
        _dispatch_request(),
        authority=_dispatch_authority(),
    )
    assert not outcome.dispatched
    assert outcome.error_code == "cancellation_scope_unavailable"


def test_fence_denial_and_fence_error_stay_predispatch() -> None:
    """Fence failures happen before dispatch — provably undispatched."""

    binding = _WindowBinding()
    binding.release.set()
    invocation = _WindowInvocation(binding, pause_dispatch=False)
    invoker = _invoker_for(invocation)

    def denied_fence(request_id: str) -> None:
        raise WorkflowDenied("fenced")

    def broken_fence(request_id: str) -> None:
        raise RuntimeError("fence blew up")

    denied = invoker.invoke(
        _dispatch_request(),
        authority=_dispatch_authority(),
        dispatch_fence=denied_fence,
    )
    assert not denied.dispatched
    assert denied.error_code == "dispatch_fenced"

    failed = invoker.invoke(
        _dispatch_request(),
        authority=_dispatch_authority(),
        dispatch_fence=broken_fence,
    )
    assert not failed.dispatched
    assert failed.error_code == "dispatch_fence_error"
    assert invocation.dispatched == []


def test_cancel_intent_fences_attempt_admission_across_connections(
    tmp_path: Path,
) -> None:
    """Separate store connections: once the durable stop intent commits,
    create_attempt on the other connection is refused — no new attempt can
    enter after Stop began and slip past the drain list."""

    path = tmp_path / "workflow.sqlite3"
    execute_store = WorkflowStoreV4(path, clock=lambda: 100.0)
    stop_store = WorkflowStoreV4(path, clock=lambda: 100.0)
    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, _stop = _engines(
        tmp_path, invocation, binding, store=execute_store
    )
    _publish_run(engine, "run-admission")

    flagged = stop_store.request_cancel("run-admission")
    assert flagged["cancel_requested"] is True

    with pytest.raises(WorkflowDenied, match="attempts are fenced"):
        execute_store.create_attempt(
            run_id="run-admission",
            step_id="echo",
            attempt_number=1,
            request={"request_id": "req-admission", "input": {}},
        )
    with pytest.raises(WorkflowDenied, match="attempts are fenced"):
        engine.execute_step("run-admission", "echo")
    assert execute_store.list_attempts("run-admission") == []

    run = execute_store.transition_run(
        "run-admission",
        expected={RunState.RUNNING},
        target=RunState.CANCELLED,
    )
    assert run["state"] == "cancelled"
    assert run["cancel_requested"] is True


def test_attempt_committed_before_stop_intent_enters_the_drain_list(
    tmp_path: Path,
) -> None:
    """Ordering guarantee: an attempt committed before the stop intent is
    always inside the drain list and is cancelled coherently."""

    path = tmp_path / "workflow.sqlite3"
    execute_store = WorkflowStoreV4(path, clock=lambda: 100.0)
    stop_store = WorkflowStoreV4(path, clock=lambda: 100.0)
    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, stop_engine = _engines(
        tmp_path, invocation, binding, store=execute_store
    )
    # The stop engine's own connection is a separate store instance; route
    # its store through stop_store so every side uses a real connection.
    stop_engine.store = stop_store  # type: ignore[assignment]
    _publish_run(engine, "run-order")
    execute_store.transition_run(
        "run-order", expected={RunState.QUEUED}, target=RunState.RUNNING
    )
    attempt = execute_store.create_attempt(
        run_id="run-order",
        step_id="echo",
        attempt_number=1,
        request={"request_id": "req-order"},
    )

    stop_engine.cancel_run("run-order")

    refreshed = execute_store.get_attempt(attempt["attempt_id"])
    assert refreshed["state"] == StepAttemptState.CANCELLED.value
    cancelled = execute_store.get_run("run-order")
    assert cancelled["state"] == "cancelled"
    assert cancelled["cancel_requested"] is True


def test_parallel_waiting_approvals_resume_sequentially(tmp_path: Path) -> None:
    """Two independent steps may wait on approval together; the run keeps
    waiting_approval until the last reservation resumes."""

    binding = _WindowBinding()
    binding.release.set()  # registration is not the window under test
    invocation = _WindowInvocation(binding)
    invocation.release_dispatch.set()  # dispatch completes immediately
    authority = Authority(state=ApprovalState.WAITING_APPROVAL)
    engine, _stop = _engines(
        tmp_path, invocation, binding, authority=authority
    )
    _publish_run(engine, "run-approvals", steps=2, independent=True)

    engine.advance_run("run-approvals")

    attempts = engine.store.list_attempts("run-approvals")
    assert {item["step_id"] for item in attempts} == {"echo", "echo_two"}
    assert all(
        item["state"] == StepAttemptState.WAITING_APPROVAL.value
        for item in attempts
    )
    assert invocation.dispatched == []
    assert (
        engine.store.get_run("run-approvals")["state"] == "waiting_approval"
    )

    # Approve and resume the first: the run must keep waiting_approval so
    # the second approval checkpoint is still reachable from the UI.
    first_reservation = next(item["authority_reservation_id"] for item in attempts if item["step_id"] == "echo")
    authority.reservations[first_reservation] = replace(
        authority.reservations[first_reservation],
        state=ApprovalState.APPROVED,
    )
    engine.execute_step("run-approvals", "echo")
    assert len(invocation.dispatched) == 1
    assert (
        engine.store.get_run("run-approvals")["state"] == "waiting_approval"
    )

    second_reservation = next(item["authority_reservation_id"] for item in attempts if item["step_id"] == "echo_two")
    authority.reservations[second_reservation] = replace(
        authority.reservations[second_reservation],
        state=ApprovalState.APPROVED,
    )
    engine.execute_step("run-approvals", "echo_two")
    assert len(invocation.dispatched) == 2
    assert engine.store.get_run("run-approvals")["state"] == "succeeded"


def test_second_cancelled_outcome_reconcile_preserves_reconciliation(
    tmp_path: Path,
) -> None:
    """Two cancelled in-flight outcomes racing to reconcile: the first
    escalates the run to needs_reconciliation and the second keeps that
    state instead of surfacing a WorkflowConflict."""

    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, _stop = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "run-reconcile", steps=2)
    store = engine.store
    store.transition_run(
        "run-reconcile",
        expected={RunState.QUEUED},
        target=RunState.RUNNING,
    )
    run = store.get_run("run-reconcile")

    first = store.create_attempt(
        run_id="run-reconcile",
        step_id="echo",
        attempt_number=1,
        request={"request_id": "req-reconcile-1"},
    )
    second = store.create_attempt(
        run_id="run-reconcile",
        step_id="echo_two",
        attempt_number=1,
        request={"request_id": "req-reconcile-2"},
    )
    for item in (first, second):
        store.transition_attempt(
            item["attempt_id"],
            expected={StepAttemptState.PENDING},
            target=StepAttemptState.DISPATCHING,
        )
        store.transition_attempt(
            item["attempt_id"],
            expected={StepAttemptState.DISPATCHING},
            target=StepAttemptState.RUNNING,
        )
        store.transition_attempt(
            item["attempt_id"],
            expected={StepAttemptState.RUNNING},
            target=StepAttemptState.CANCELLED,
        )
    outcome = InvocationOutcome(output={"ok": True})

    engine._record_cancelled_dispatch_outcome(
        run, first, outcome, digest({"outcome": 1}), "reservation-1"
    )
    assert (
        store.get_run("run-reconcile")["state"] == "needs_reconciliation"
    )
    kept = engine._record_cancelled_dispatch_outcome(
        run, second, outcome, digest({"outcome": 2}), "reservation-2"
    )
    # The second writer preserves the reconciliation state rather than
    # conflicting on the version bump.
    assert kept["state"] == "needs_reconciliation"
    assert store.get_attempt(second["attempt_id"])["outcome"] == {"ok": True}


@pytest.mark.parametrize("canceller", ["stop", "execute", "unavailable"])
@pytest.mark.parametrize("legacy,state", [(False, "running"), (True, "running"), (True, "dispatching")])
def test_restart_without_live_handle_cannot_erase_uncertain_dispatch(tmp_path, legacy, state, canceller):
    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, _ = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "restart-stop")
    request = dict(_dispatch_request())
    attempt = engine.store.create_attempt(run_id="restart-stop", step_id="echo", attempt_number=1, request=request)
    engine.store.transition_run("restart-stop", expected={RunState.QUEUED}, target=RunState.RUNNING)
    engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.PENDING}, target=StepAttemptState.DISPATCHING)
    if state == "running":
        engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.DISPATCHING}, target=StepAttemptState.RUNNING)
        if not legacy:
            engine.store.admit_dispatch(attempt["attempt_id"], request["request_id"])
    if legacy:
        # Authenticated fixture representing a record written before the marker existed.
        record = engine.store.get_attempt(attempt["attempt_id"])
        record.pop("dispatch_admission")
        payload, seal = engine.store._encode(record, kind="attempt")
        engine.store._connection.execute("UPDATE workflow_attempts SET payload=?, seal=? WHERE attempt_id=?", (payload, seal, attempt["attempt_id"]))
    engine.store.close()
    reopened = WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda:100.0)
    execute, stop = _engines(tmp_path, invocation, binding, store=reopened)
    if canceller == "execute":
        stop._invoker = execute._invoker
    elif canceller == "unavailable":
        from core_runtime.workflow_v4.attempt_adapter import CapturedWorkflowAttemptAdapterV4
        stop._invoker = CapturedWorkflowAttemptAdapterV4(
            store=reopened, port=None, invocation=invocation,
        )
    before = reopened.get_attempt(attempt["attempt_id"])
    with pytest.raises(WorkflowCancellationUnconfirmed):
        stop.cancel_run("restart-stop")
    assert reopened.get_attempt(attempt["attempt_id"]) == before
    assert reopened.get_run("restart-stop")["state"] == "running"
    assert reopened.get_run("restart-stop")["cancel_requested"] is True
    assert binding.requested == [] and binding.drain_waits == 0
    assert stop.reconcile_recovery("restart-stop")["state"] == "needs_reconciliation"
    assert reopened.get_attempt(attempt["attempt_id"])["state"] == "ambiguous_effect"
    reopened.close()


def test_stop_in_ambiguous_attempt_commit_gap_preserves_reconciliation(tmp_path):
    binding = _WindowBinding()
    binding.release.set()
    invocation = _WindowInvocation(binding, pause_dispatch=False, dispatch_error=ValueError("uncertain effect"))
    engine, stop = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "ambiguous-stop")
    original = engine.store.transition_attempt
    def interrupt(attempt_id, *, expected, target, updates=None):
        record = original(attempt_id, expected=expected, target=target, updates=updates)
        if target is StepAttemptState.AMBIGUOUS_EFFECT:
            assert stop.cancel_run("ambiguous-stop")["state"] == "needs_reconciliation"
        return record
    engine.store.transition_attempt = interrupt
    engine.execute_step("ambiguous-stop", "echo")
    assert engine.store.list_attempts("ambiguous-stop")[0]["state"] == "ambiguous_effect"
    assert engine.store.get_run("ambiguous-stop")["state"] == "needs_reconciliation"


def test_dispatch_admission_is_exact_single_use_and_fenced_by_stop(tmp_path):
    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, _ = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "admission-stop")
    request = dict(_dispatch_request())
    attempt = engine.store.create_attempt(run_id="admission-stop", step_id="echo", attempt_number=1, request=request)
    engine.store.transition_run("admission-stop", expected={RunState.QUEUED}, target=RunState.RUNNING)
    engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.PENDING}, target=StepAttemptState.DISPATCHING)
    engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.DISPATCHING}, target=StepAttemptState.RUNNING)
    with pytest.raises(WorkflowDenied):
        engine.store.admit_dispatch(attempt["attempt_id"], "wrong")
    assert engine.store.get_attempt(attempt["attempt_id"])["dispatch_admission"] == "not_admitted"
    engine.store.admit_dispatch(attempt["attempt_id"], request["request_id"])
    with pytest.raises(WorkflowDenied):
        engine.store.admit_dispatch(attempt["attempt_id"], request["request_id"])
    engine.store.request_cancel("admission-stop")
    with pytest.raises(WorkflowDenied):
        engine.store.admit_dispatch(attempt["attempt_id"], request["request_id"])


def test_execute_role_signal_is_not_drain_confirmation():
    binding = _WindowBinding()
    binding.active = "owned"
    invocation = _WindowInvocation(binding)
    invoker = _invoker_for(invocation)
    with pytest.raises(WorkflowCancellationUnconfirmed):
        invoker.cancel("owned")
    assert binding.requested == ["owned"]
    assert binding.drain_waits == 0


def test_stop_intent_prevents_first_dispatch_admission(tmp_path):
    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, _ = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "cancel-before-admit")
    request = dict(_dispatch_request())
    attempt = engine.store.create_attempt(run_id="cancel-before-admit", step_id="echo", attempt_number=1, request=request)
    engine.store.transition_run("cancel-before-admit", expected={RunState.QUEUED}, target=RunState.RUNNING)
    engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.PENDING}, target=StepAttemptState.DISPATCHING)
    engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.DISPATCHING}, target=StepAttemptState.RUNNING)
    before = engine.store.get_attempt(attempt["attempt_id"])
    other = WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda:100.0)
    try:
        other.request_cancel("cancel-before-admit")
        with pytest.raises(WorkflowDenied):
            engine.store.admit_dispatch(attempt["attempt_id"], request["request_id"])
        assert engine.store.get_attempt(attempt["attempt_id"]) == before
    finally:
        other.close()


def test_reconciliation_retry_keeps_a_concurrent_finalizer(tmp_path):
    binding = _WindowBinding()
    invocation = _WindowInvocation(binding)
    engine, _ = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "reconcile-race")
    engine.store.transition_run("reconcile-race", expected={RunState.QUEUED}, target=RunState.CANCELLED)
    original = engine.store.transition_run
    def race(run_id, *, expected, target, updates=None):
        if expected == {RunState.CANCELLED} and target is RunState.NEEDS_RECONCILIATION:
            original(run_id, expected=expected, target=target)
        return original(run_id, expected=expected, target=target, updates=updates)
    engine.store.transition_run = race
    result = engine._run_transition_preserving_cancel("reconcile-race", expected={RunState.RUNNING}, target=RunState.NEEDS_RECONCILIATION)
    assert result["state"] == "needs_reconciliation"


def test_unverified_drain_does_not_prevent_signalling_later_attempts(tmp_path):
    class Binding:
        def __init__(self):
            self.signals = []
        def active_for(self, reference):
            return True
        def can_request(self, reference):
            return True
        def request(self, reference):
            self.signals.append(reference)
            class Observation:
                def wait_for_verified_drain(self, deadline):
                    return False
            return Observation()
    binding = Binding()
    invocation = _WindowInvocation(binding)
    engine, stop = _engines(tmp_path, invocation, binding)
    _publish_run(engine, "partial-drain", steps=2)
    engine.store.transition_run("partial-drain", expected={RunState.QUEUED}, target=RunState.RUNNING)
    for step in ("first", "second"):
        request = {**_dispatch_request(), "request_id": step}
        attempt = engine.store.create_attempt(run_id="partial-drain", step_id=step, attempt_number=1, request=request)
        engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.PENDING}, target=StepAttemptState.DISPATCHING)
        engine.store.transition_attempt(attempt["attempt_id"], expected={StepAttemptState.DISPATCHING}, target=StepAttemptState.RUNNING)
        engine.store.admit_dispatch(attempt["attempt_id"], step)
    with pytest.raises(WorkflowCancellationUnconfirmed):
        stop.cancel_run("partial-drain")
    assert binding.signals == ["first", "second"]
    assert all(item["state"] == "running" for item in engine.store.list_attempts("partial-drain"))
    assert engine.store.get_run("partial-drain")["cancel_requested"] is True
