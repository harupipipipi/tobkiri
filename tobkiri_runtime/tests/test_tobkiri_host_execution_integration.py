"""Integration and adversarial tests for canonical v4 Request execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from concurrent.futures import Future
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace
import time
from typing import Any, Mapping

import pytest

from tobkiri_host.admission import (
    AdmissionEstimate,
    ResourceReservation,
)
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.broker import AdmissionTicket, RequestBroker, RequestEnvelope
from tobkiri_host.contracts import AdapterPlanner, OperationCatalog, OperationRoute
from tobkiri_host.effects import (
    EffectDisposition,
    InMemoryReconciliationStore,
    ProviderOutcome,
)
from tobkiri_host.errors import (
    AmbiguousEffectError,
    AuditUnavailableError,
    AuthorizationError,
    ProviderExecutionError,
    RequestTimedOutError,
    RequestCancellationRequestedError,
)
from tobkiri_host.materialization import (
    MaterializationCoordinator,
    WorkloadInstanceKey,
)
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    EffectClass,
    ExecutionKind,
    FunctionArtifact,
    InvocationFrame,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
    RequestContext,
    RuntimeEvidence,
)
from tobkiri_host.ports import (
    OpaqueAuditReservation,
    OpaqueInvocationLease,
)
from tobkiri_host.triggers import TriggerRegistration, TriggerWakeKernel


def digest(character: str) -> str:
    return f"sha256:{hashlib.sha256(character.encode()).hexdigest()}"


INPUT_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"delivered": {"type": "boolean"}},
    "required": ["delivered"],
    "additionalProperties": False,
}


def fixture_artifact(
    effect: EffectClass = EffectClass.EXTERNAL_EFFECT,
    timeout_ms: int = 100,
) -> PackArtifact:
    operation = ContractOperation(
        contract_id="io.tobkiri.notification.v1",
        contract_version="1.0.0",
        revision_digest=digest("c"),
        operation_id="send",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        effect_class=effect,
        timeout_default_ms=timeout_ms,
        timeout_hard_max_ms=max(timeout_ms, 100),
        idempotency="keyed",
        reconcile_operation="status",
    )
    function = FunctionArtifact(
        function_id="notification.send",
        implementation_digest=digest("f"),
        variant_id="wasm.macos",
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id="wasm.macos",
        digest=digest("v"),
        execution_kind=ExecutionKind.WASM,
        os="macos",
        architecture="arm64",
        runtime_abi="component-v1",
        backend="wasmtime",
    )
    return PackArtifact(
        pack_id="notification.pack",
        version="1.0.0",
        digest=digest("a"),
        publisher_lineage="publisher.notification",
        package_kind=PackageKind.NORMAL,
        functions=(function,),
        variants=(variant,),
    )


def fixture_catalog(item: PackArtifact) -> OperationCatalog:
    return OperationCatalog(
        (item,),
        (
            OperationRoute(
                contract_id="io.tobkiri.notification.v1",
                operation_id="send",
                artifact_digest=item.digest,
                function_id="notification.send",
                variant_id="wasm.macos",
                execution_domain_profile="wasm.effect.v1",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef("authority:notification-send"),
            ),
        ),
    )


def context() -> RequestContext:
    return RequestContext(
        request_id="request-1",
        trace_id="trace-1",
        caller_principal=OpaqueAuthorityRef("authority:caller"),
        profile_id="profile-1",
        activation_id="activation-1",
        activation_digest=digest("x"),
        plan_digest=digest("y"),
        security_epoch=9,
        caller_session_id="caller-session",
        caller_domain_id="caller-domain",
        caller_boot_epoch=2,
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_backend_digest=digest("b"),
        profile_authority_digest=digest("profile-authority"),
        fencing_token=1,
        handle_namespace="caller-handles",
    )


class NoAdapters:
    def execute(self, adapter: object, payload: Mapping[str, Any]):
        raise AssertionError("no adapter should execute")


class FakeAdmission:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.released = False

    def estimate(self, context, binding, payload) -> AdmissionEstimate:
        self.events.append("static_admission")
        return AdmissionEstimate(10, 10, 10, 10, 10)

    def acquire(self, scope, estimate, wait_timeout_seconds) -> AdmissionTicket:
        self.events.append("queue_reserved")
        return AdmissionTicket(
            ResourceReservation("reservation-1", scope.profile_id, estimate.charge())
        )

    def release(self, ticket: AdmissionTicket) -> None:
        self.events.append("reservation_released")
        self.released = True


class FakeAuthority:
    def __init__(self, events: list[str], fail_static: bool = False) -> None:
        self.events = events
        self.fail_static = fail_static
        self.fenced: list[str] = []

    def check_static_path(self, query) -> None:
        self.events.append("authority_static")
        if self.fail_static:
            raise PermissionError("denied")

    def authorize_and_issue_lease(self, query) -> OpaqueInvocationLease:
        self.events.append("authority_final")
        return OpaqueInvocationLease(b"opaque-request-bound-lease")

    def recheck_effect_boundary(self, context, target, lease) -> None:
        self.events.append("authority_effect_recheck")

    def fence_request(self, request_id: str) -> None:
        self.events.append("authority_fenced")
        self.fenced.append(request_id)

    def issue_trigger_lease(
        self, registration_id, occurrence_id, target, security_epoch
    ) -> OpaqueInvocationLease:
        self.events.append(f"trigger_lease:{occurrence_id}")
        return OpaqueInvocationLease(b"trigger-one-shot")


class FakeAudit:
    def __init__(self, events: list[str], fail_reserve: bool = False) -> None:
        self.events = events
        self.fail_reserve = fail_reserve
        self.failures: list[tuple[str, bool]] = []

    def reserve_effect(self, context, binding, request_digest):
        self.events.append("audit_reserved")
        if self.fail_reserve:
            raise OSError("disk full")
        return OpaqueAuditReservation("audit-1")

    def mark_dispatched(self, reservation) -> None:
        self.events.append("audit_dispatched")

    def commit_effect(self, reservation, outcome_digest) -> None:
        self.events.append("audit_committed")

    def fail_effect(self, reservation, stable_code, ambiguous) -> None:
        self.events.append("audit_failed")
        self.failures.append((stable_code, ambiguous))


class FakeBackend:
    def __init__(
        self,
        events: list[str],
        outcome: ProviderOutcome | None = None,
        delay: float = 0,
        evidence: RuntimeEvidence | None = None,
    ) -> None:
        self.events = events
        self.outcome = outcome or ProviderOutcome({"delivered": True})
        self.delay = delay
        self.status = BackendStatus(
            backend_id="wasmtime",
            execution_kind=ExecutionKind.WASM,
            platform="macos-arm64",
            backend_digest=digest("b"),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self.evidence = evidence or RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef("domain:notification"),
            executable_digest=digest("f"),
            backend_digest=digest("b"),
            authenticated_channel=True,
            nonce_fresh=True,
        )
        self.starts = 0
        self.invocations = 0
        self.cancelled: list[str] = []
        self.terminated: list[str] = []
        self._lock = Lock()

    def materialize(self, binding, reservation_id) -> RuntimeEvidence:
        with self._lock:
            self.starts += 1
        self.events.append("materialized")
        return self.evidence

    def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
        self.invocations += 1
        self.events.append("provider_invoked")
        if self.delay:
            time.sleep(self.delay)
        return self.outcome

    def cancel(self, request_id: str) -> None:
        self.events.append("backend_cancelled")
        self.cancelled.append(request_id)

    def terminate(self, domain_id: str) -> None:
        self.events.append("backend_terminated")
        self.terminated.append(domain_id)


@dataclass
class BrokerFixture:
    broker: RequestBroker
    backend: FakeBackend
    authority: FakeAuthority
    audit: FakeAudit
    admission: FakeAdmission
    reconciliation: InMemoryReconciliationStore
    events: list[str]


def make_broker(
    *,
    effect: EffectClass = EffectClass.EXTERNAL_EFFECT,
    timeout_ms: int = 100,
    fail_static: bool = False,
    fail_audit: bool = False,
    backend: FakeBackend | None = None,
    max_workers: int = 16,
) -> BrokerFixture:
    events: list[str] = []
    item = fixture_artifact(effect, timeout_ms)
    selected_backend = backend or FakeBackend(events)
    selected_backend.events = events
    authority = FakeAuthority(events, fail_static)
    audit = FakeAudit(events, fail_audit)
    admission = FakeAdmission(events)
    reconciliation = InMemoryReconciliationStore()
    broker = RequestBroker(
        catalog=fixture_catalog(item),
        adapters=AdapterPlanner(()),
        adapter_executor=NoAdapters(),
        backends=BackendRegistry((selected_backend,)),
        materialization=MaterializationCoordinator(),
        admission=admission,
        authority=authority,
        audit=audit,
        reconciliation=reconciliation,
        max_workers=max_workers,
    )
    return BrokerFixture(
        broker,
        selected_backend,
        authority,
        audit,
        admission,
        reconciliation,
        events,
    )


def frame(timeout_ms: int | None = None) -> InvocationFrame:
    return InvocationFrame(
        contract_id="io.tobkiri.notification.v1",
        version_range=">=1,<2",
        operation_id="send",
        payload={"message": "hello"},
        timeout_ms=timeout_ms,
        idempotency_key="notification:request-1",
    )


def test_canonical_broker_orders_all_security_gates() -> None:
    fixture = make_broker()
    try:
        assert fixture.broker.invoke(frame(), context(), effect_scope={"user": "u1"}) == {
            "delivered": True
        }
    finally:
        fixture.broker.close()
    assert fixture.events == [
        "authority_static",
        "static_admission",
        "queue_reserved",
        "materialized",
        "authority_final",
        "audit_reserved",
        "authority_effect_recheck",
        "audit_dispatched",
        "provider_invoked",
        "audit_committed",
        "reservation_released",
    ]


def test_static_authority_denial_never_reserves_or_materializes() -> None:
    fixture = make_broker(fail_static=True)
    try:
        with pytest.raises(AuthorizationError, match="static authorization"):
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert fixture.backend.starts == 0
    assert fixture.events == ["authority_static"]


def test_audit_reservation_failure_never_dispatches_external_effect() -> None:
    fixture = make_broker(fail_audit=True)
    try:
        with pytest.raises(AuditUnavailableError):
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert fixture.backend.invocations == 0
    assert fixture.admission.released
    assert fixture.authority.fenced == ["request-1"]


def test_runtime_evidence_mismatch_terminates_candidate_before_authorization() -> None:
    events: list[str] = []
    bad_evidence = RuntimeEvidence(
        domain_ref=OpaqueAuthorityRef("domain:forged"),
        executable_digest=digest("0"),
        backend_digest=digest("b"),
        authenticated_channel=True,
        nonce_fresh=True,
    )
    fixture = make_broker(backend=FakeBackend(events, evidence=bad_evidence))
    try:
        with pytest.raises(AuthorizationError, match="evidence mismatch"):
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert fixture.backend.terminated == ["domain:forged"]
    assert fixture.backend.starts == 1
    assert "authority_final" not in fixture.events


def test_external_timeout_is_fenced_persisted_and_never_auto_retried() -> None:
    events: list[str] = []
    slow = FakeBackend(events, delay=0.1)
    fixture = make_broker(timeout_ms=20, backend=slow)
    try:
        with pytest.raises(AmbiguousEffectError) as raised:
            fixture.broker.invoke(frame(20), context(), effect_scope={})
    finally:
        fixture.broker.close()
    record = fixture.reconciliation.get(raised.value.reconciliation_id)
    assert record.status == "needs_reconciliation"
    assert record.reconcile_operation == "status"
    assert fixture.backend.invocations == 1
    assert fixture.backend.cancelled == ["request-1"]
    assert fixture.authority.fenced == ["request-1"]
    assert fixture.audit.failures == [("ambiguous_effect", True)]


def test_read_timeout_finishes_cleanup_and_later_unique_request_recovers() -> None:
    """A late first result is fenced and does not poison a normal retry."""

    release_first = Event()
    first_finished = Event()

    class RecoveringBackend(FakeBackend):
        def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
            self.invocations += 1
            self.events.append(f"provider_invoked:{request.context.request_id}")
            if request.context.request_id == "request-1":
                assert release_first.wait(timeout=10)
                first_finished.set()
            return self.outcome

        def cancel(self, request_id: str) -> None:
            super().cancel(request_id)
            if request_id == "request-1":
                release_first.set()

    fixture = make_broker(
        effect=EffectClass.READ,
        timeout_ms=20,
        backend=RecoveringBackend([]),
    )
    try:
        with pytest.raises(RequestTimedOutError):
            fixture.broker.invoke(frame(20), context(), effect_scope={})
        assert first_finished.wait(timeout=2)
        recovered = fixture.broker.invoke(
            replace(frame(20), idempotency_key="notification:request-2"),
            replace(context(), request_id="request-2", trace_id="trace-2"),
            effect_scope={},
        )
    finally:
        release_first.set()
        fixture.broker.close()

    assert recovered == {"delivered": True}
    assert fixture.backend.cancelled == ["request-1"]
    assert fixture.authority.fenced == ["request-1"]
    assert fixture.backend.invocations == 2
    assert fixture.audit.failures == [("provider_failed", False)]


def test_queued_read_timeout_never_enters_backend_and_later_request_recovers() -> None:
    """A queued timeout is removed before backend ownership and cannot run late."""

    first_entered = Event()
    release_first = Event()

    class SaturatedBackend(FakeBackend):
        def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
            self.invocations += 1
            self.events.append(f"provider_invoked:{request.context.request_id}")
            if request.context.request_id == "request-1":
                first_entered.set()
                assert release_first.wait(timeout=10)
            return self.outcome

    fixture = make_broker(
        effect=EffectClass.READ,
        timeout_ms=1000,
        backend=SaturatedBackend([]),
        max_workers=1,
    )
    first_result: list[Mapping[str, Any]] = []
    first_error: list[BaseException] = []

    def invoke_first() -> None:
        try:
            first_result.append(fixture.broker.invoke(frame(1000), context(), effect_scope={}))
        except BaseException as exc:
            first_error.append(exc)

    first_thread = Thread(target=invoke_first)
    first_thread.start()
    try:
        assert first_entered.wait(timeout=2)
        with pytest.raises(RequestTimedOutError):
            fixture.broker.invoke(
                replace(frame(20), idempotency_key="notification:request-2"),
                replace(context(), request_id="request-2", trace_id="trace-2"),
                effect_scope={},
            )
        assert fixture.backend.cancelled == []
        assert fixture.backend.invocations == 1

        release_first.set()
        first_thread.join(timeout=2)
        assert not first_thread.is_alive()
        assert first_error == []
        assert first_result == [{"delivered": True}]

        recovered = fixture.broker.invoke(
            replace(frame(1000), idempotency_key="notification:request-3"),
            replace(context(), request_id="request-3", trace_id="trace-3"),
            effect_scope={},
        )
    finally:
        release_first.set()
        first_thread.join(timeout=2)
        fixture.broker.close()

    assert recovered == {"delivered": True}
    assert fixture.backend.invocations == 2
    assert fixture.authority.fenced == ["request-2"]
    assert fixture.audit.failures == [("provider_failed", False)]


@pytest.mark.parametrize("effect", [EffectClass.READ, EffectClass.EXTERNAL_EFFECT])
@pytest.mark.parametrize("cancel_fails", [False, True])
def test_parent_cancellation_targets_inner_request_without_claiming_termination(
    effect: EffectClass, cancel_fails: bool,
) -> None:
    """A shared Host signal reaches the selected backend's exact inner ID."""
    cancelled = Event()
    release_provider = Event()

    class CancellableBackend(FakeBackend):
        def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
            assert request.cancellation_requested is cancelled
            self.invocations += 1
            cancelled.set()
            assert release_provider.wait(timeout=10)
            return self.outcome

        def cancel(self, request_id: str) -> None:
            super().cancel(request_id)
            release_provider.set()
            if cancel_fails:
                raise RuntimeError("test cancellation was not acknowledged")

    fixture = make_broker(effect=effect, timeout_ms=10000, backend=CancellableBackend([]))
    expected_error = (
        AmbiguousEffectError if effect is EffectClass.EXTERNAL_EFFECT
        else ProviderExecutionError if cancel_fails
        else RequestCancellationRequestedError
    )
    try:
        with pytest.raises(expected_error):
            fixture.broker.invoke(
                frame(), replace(context(), request_id="inner-request"),
                effect_scope={}, parent_cancellation=cancelled,
            )
    finally:
        release_provider.set()
        fixture.broker.close()
    assert fixture.backend.cancelled == ["inner-request"]
    assert fixture.backend.invocations == 1
    assert fixture.authority.fenced == ["inner-request"]
    assert "audit_committed" not in fixture.events


def test_already_cancelled_parent_never_starts_inner_work() -> None:
    """No admission, authority exercise or provider call follows a prior cancel."""
    cancelled = Event()
    cancelled.set()
    fixture = make_broker()
    try:
        with pytest.raises(RequestCancellationRequestedError):
            fixture.broker.invoke(
                frame(), context(), effect_scope={}, parent_cancellation=cancelled,
            )
        assert fixture.events == []
    finally:
        fixture.broker.close()


def test_unstopped_cancelled_provider_keeps_admission_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op cancel cannot free capacity while the provider still runs."""
    cancelled = Event()
    release_provider = Event()
    resources_released = Event()

    class UnstoppableBackend(FakeBackend):
        def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
            self.invocations += 1
            cancelled.set()
            assert release_provider.wait(timeout=10)
            return self.outcome

    fixture = make_broker(
        effect=EffectClass.READ, timeout_ms=10000, backend=UnstoppableBackend([])
    )
    original_release = fixture.admission.release

    def release(ticket) -> None:
        original_release(ticket)
        resources_released.set()

    monkeypatch.setattr(fixture.admission, "release", release)
    try:
        with pytest.raises(RequestCancellationRequestedError):
            fixture.broker.invoke(
                frame(), context(), effect_scope={}, parent_cancellation=cancelled,
            )
        assert fixture.backend.cancelled == ["request-1"]
        assert "reservation_released" not in fixture.events
    finally:
        release_provider.set()
        fixture.broker.close()
    assert resources_released.wait(timeout=10)
    assert fixture.events.count("reservation_released") == 1
    assert "audit_committed" not in fixture.events


@pytest.mark.parametrize("effect", [EffectClass.READ, EffectClass.EXTERNAL_EFFECT])
def test_completed_future_cannot_publish_a_late_result(
    monkeypatch: pytest.MonkeyPatch, effect: EffectClass,
) -> None:
    """A result ready before observation still cannot outlive its deadline."""
    import tobkiri_host.broker as broker_module

    clock = [100.0]
    monkeypatch.setattr(
        broker_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    fixture = make_broker(effect=effect)

    def submit(function, *arguments):
        future = Future()
        future.set_result(function(*arguments))
        # Model the Host being scheduled only after the deadline. The Future
        # is already done, so Future.result(timeout=0) cannot enforce expiry.
        clock[0] += 1.0
        return future

    monkeypatch.setattr(fixture.broker._executor, "submit", submit)
    error_type = AmbiguousEffectError if effect is EffectClass.EXTERNAL_EFFECT else RequestTimedOutError
    try:
        with pytest.raises(error_type):
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert fixture.backend.invocations == 1
    assert fixture.backend.cancelled == ["request-1"]
    assert fixture.authority.fenced == ["request-1"]
    assert "audit_committed" not in fixture.events


def test_local_timeout_kills_process_group_before_side_effect(tmp_path: Path) -> None:
    events: list[str] = []

    class ProcessBackend(FakeBackend):
        process: subprocess.Popen[bytes] | None = None

        def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
            self.invocations += 1
            marker = tmp_path / "late-side-effect"
            self.process = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    "import pathlib,time;time.sleep(0.3);"
                    f"pathlib.Path({str(marker)!r}).write_text('late')",
                ),
                start_new_session=True,
            )
            self.process.wait()
            return ProviderOutcome({"delivered": True})

        def cancel(self, request_id: str) -> None:
            super().cancel(request_id)
            assert self.process is not None
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=1)

    backend = ProcessBackend(events)
    fixture = make_broker(
        effect=EffectClass.WRITE,
        timeout_ms=30,
        backend=backend,
    )
    try:
        with pytest.raises(RequestTimedOutError):
            fixture.broker.invoke(frame(30), context(), effect_scope={})
    finally:
        fixture.broker.close()
    time.sleep(0.35)
    assert backend.process is not None and backend.process.poll() is not None
    assert not (tmp_path / "late-side-effect").exists()
    assert fixture.audit.failures == [("provider_failed", False)]
    assert fixture.authority.fenced == ["request-1"]


def test_cancel_transport_failure_still_audits_and_fences() -> None:
    events: list[str] = []

    class CancelFailureBackend(FakeBackend):
        def cancel(self, request_id: str) -> None:
            del request_id
            raise OSError("authenticated transport unavailable")

    fixture = make_broker(
        effect=EffectClass.WRITE,
        timeout_ms=20,
        backend=CancelFailureBackend(events, delay=0.1),
    )
    try:
        with pytest.raises(ProviderExecutionError, match="cancellation failed"):
            fixture.broker.invoke(frame(20), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert fixture.audit.failures == [("provider_failed", False)]
    assert fixture.authority.fenced == ["request-1"]


def test_accepted_effect_result_is_ambiguous_until_reconciled() -> None:
    events: list[str] = []
    accepted = ProviderOutcome(
        payload=None,
        disposition=EffectDisposition.ACCEPTED,
        receipt="receipt-1",
    )
    fixture = make_broker(backend=FakeBackend(events, outcome=accepted))
    try:
        with pytest.raises(AmbiguousEffectError) as raised:
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    record = fixture.reconciliation.get(raised.value.reconciliation_id)
    assert record.receipt == "receipt-1"
    resolved = fixture.reconciliation.resolve(
        record.reconciliation_id,
        EffectDisposition.COMPLETED,
        "signed-receipt",
    )
    assert resolved.status == "completed"


def test_provider_exception_is_sanitized_and_request_authority_is_fenced() -> None:
    events: list[str] = []

    class FailingBackend(FakeBackend):
        def invoke(self, request: RequestEnvelope) -> ProviderOutcome:
            self.invocations += 1
            raise RuntimeError("provider-secret-and-internal-path")

    fixture = make_broker(backend=FailingBackend(events))
    try:
        with pytest.raises(ProviderExecutionError) as raised:
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert "provider-secret" not in str(raised.value)
    assert fixture.authority.fenced == ["request-1"]
    assert fixture.audit.failures == [("provider_failed", False)]


def test_provider_rejection_always_releases_submitted_future() -> None:
    """Broker cleanup cancels its Future even when result retrieval raises."""

    class CancelTrackedFuture(Future[object]):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_calls = 0

        def cancel(self) -> bool:
            self.cancel_calls += 1
            return super().cancel()

    class FailedExecutor:
        def __init__(self) -> None:
            self.future = CancelTrackedFuture()

        def submit(
            self,
            _callable: object,
            *_args: object,
            **_kwargs: object,
        ) -> CancelTrackedFuture:
            self.future.set_exception(RuntimeError("provider-private-detail"))
            return self.future

        def shutdown(self, **_kwargs: object) -> None:
            return None

    fixture = make_broker(effect=EffectClass.WRITE)
    fixture.broker._executor.shutdown(wait=True, cancel_futures=True)
    executor = FailedExecutor()
    fixture.broker._executor = executor  # type: ignore[assignment]
    try:
        with pytest.raises(ProviderExecutionError):
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()

    assert executor.future.cancel_calls == 1
    assert fixture.admission.released
    assert fixture.audit.failures == [("provider_failed", False)]


@pytest.mark.parametrize("failure_at", (None, "materialize", "authorize", "release"))
@pytest.mark.parametrize("worker_memory", (1, 1024 * 1024))
def test_request_scoped_worker_cleanup_precedes_resource_release(
    failure_at: str | None,
    worker_memory: int,
) -> None:
    class WorkerBackend(FakeBackend):
        memory_reservation_bytes = worker_memory

        def materialize(self, binding, reservation_id) -> RuntimeEvidence:
            evidence = super().materialize(binding, reservation_id)
            if failure_at == "materialize":
                raise RuntimeError("partial worker start failed")
            return evidence

        def release_materialization(self, reservation_id: str) -> None:
            assert reservation_id == "reservation-1"
            self.events.append("worker_cleanup")
            if failure_at == "release":
                raise RuntimeError("worker exit could not be confirmed")

    fixture = make_broker(backend=WorkerBackend([]))
    acquire = fixture.admission.acquire

    def acquire_worker(scope, estimate, wait_timeout_seconds):
        assert estimate.charge().memory_bytes == max(10, worker_memory)
        return acquire(scope, estimate, wait_timeout_seconds)

    fixture.admission.acquire = acquire_worker
    if failure_at == "authorize":

        def deny(query: object) -> None:
            raise PermissionError("final authorization denied")

        fixture.authority.authorize_and_issue_lease = deny
    try:
        if failure_at:
            expected = AuthorizationError if failure_at == "authorize" else RuntimeError
            with pytest.raises(expected):
                fixture.broker.invoke(frame(), context(), effect_scope={"user": "u1"})
        else:
            fixture.broker.invoke(frame(), context(), effect_scope={"user": "u1"})
    finally:
        fixture.broker.close()
    assert fixture.events.count("worker_cleanup") == 1
    if failure_at == "release":
        assert not fixture.admission.released
        assert "request-1" in fixture.authority.fenced
    else:
        assert fixture.events.index("worker_cleanup") < fixture.events.index("reservation_released")
    if failure_at in ("materialize", "authorize"):
        assert fixture.backend.invocations == 0


@pytest.mark.parametrize("worker_memory", (0, -1, True, "1024"))
def test_invalid_worker_memory_reservation_never_starts_or_charges(worker_memory) -> None:
    from tobkiri_host.errors import AdmissionError

    class WorkerBackend(FakeBackend):
        memory_reservation_bytes = worker_memory

        def release_materialization(self, reservation_id: str) -> None:
            raise AssertionError("no worker or reservation should exist")

    fixture = make_broker(backend=WorkerBackend([]))
    try:
        with pytest.raises(AdmissionError, match="memory reservation is invalid"):
            fixture.broker.invoke(frame(), context(), effect_scope={"user": "u1"})
    finally:
        fixture.broker.close()
    assert fixture.backend.starts == 0
    assert "queue_reserved" not in fixture.events


@pytest.mark.parametrize("overhead", [-1, True, False, 1.5, "1", None, float("nan")])
def test_worker_floor_cannot_hide_invalid_backend_estimate(overhead: object) -> None:
    """Reject the original estimate before max() can turn it into valid input."""
    from tobkiri_host.errors import AdmissionError

    class WorkerBackend(FakeBackend):
        memory_reservation_bytes = 1024

        def release_materialization(self, reservation_id: str) -> None:
            raise AssertionError("no worker or reservation should exist")

    fixture = make_broker(backend=WorkerBackend([]))
    estimate = fixture.admission.estimate
    fixture.admission.estimate = lambda *args: replace(
        estimate(*args), backend_overhead_bytes=overhead,
    )
    try:
        with pytest.raises(AdmissionError, match="backend estimate is invalid"):
            fixture.broker.invoke(frame(), context(), effect_scope={})
    finally:
        fixture.broker.close()
    assert fixture.backend.starts == 0
    assert "queue_reserved" not in fixture.events


def test_singleflight_materialization_never_merges_distinct_principals() -> None:
    events: list[str] = []

    class SlowStartBackend(FakeBackend):
        def materialize(self, binding, reservation_id) -> RuntimeEvidence:
            time.sleep(0.05)
            return super().materialize(binding, reservation_id)

    backend = SlowStartBackend(events)
    coordinator = MaterializationCoordinator()
    item = fixture_artifact()
    binding = fixture_catalog(item).resolve("io.tobkiri.notification.v1", "send", ">=1")
    key = WorkloadInstanceKey(
        "profile-1",
        "activation-1",
        binding.principal_ref,
        "wasm.effect.v1",
        9,
    )
    barrier = Barrier(5)
    results: list[RuntimeEvidence] = []

    def run() -> None:
        barrier.wait()
        results.append(coordinator.materialize(key, backend, binding, "reservation"))

    threads = [Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 5
    assert backend.starts == 1

    other_key = replace(
        key,
        target_principal=OpaqueAuthorityRef("authority:different-operation"),
    )
    coordinator.materialize(other_key, backend, binding, "reservation-2")
    assert backend.starts == 2


def test_singleflight_keeps_concurrent_resource_reservations_separate() -> None:
    from concurrent.futures import ThreadPoolExecutor

    barrier = Barrier(2)

    class ReservedBackend(FakeBackend):
        def materialize(self, binding, reservation_id) -> RuntimeEvidence:
            barrier.wait(timeout=2)
            evidence = super().materialize(binding, reservation_id)
            return replace(evidence, resource_reservation_id=reservation_id)

    backend = ReservedBackend([])
    coordinator = MaterializationCoordinator()
    binding = fixture_catalog(fixture_artifact()).resolve(
        "io.tobkiri.notification.v1", "send", ">=1"
    )
    key = WorkloadInstanceKey(
        "profile-1", "activation-1", binding.principal_ref, "wasm.effect.v1", 9
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(coordinator.materialize, key, backend, binding, reservation)
            for reservation in ("first-reservation", "second-reservation")
        ]
        evidence = [future.result(timeout=3) for future in futures]
    assert [item.resource_reservation_id for item in evidence] == [
        "first-reservation",
        "second-reservation",
    ]
    assert backend.starts == 2


def test_trigger_kernel_deduplicates_and_issues_one_shot_lease() -> None:
    events: list[str] = []
    authority = FakeAuthority(events)
    database = sqlite3.connect(":memory:")
    now = [10.0]
    kernel = TriggerWakeKernel(database, authority, clock=lambda: now[0])
    registration = TriggerRegistration(
        registration_id="daily-summary",
        contract_id="io.tobkiri.trigger.v1",
        operation_id="deliver",
        target=OpaqueAuthorityRef("authority:workflow-start"),
        activation_digest=digest("z"),
        security_epoch=9,
        pending_quota=1,
    )
    kernel.register(registration)
    assert kernel.schedule("daily-summary", "occurrence-1", 9.0)
    assert not kernel.schedule("daily-summary", "occurrence-1", 9.0)
    delivery = kernel.claim_due()
    assert delivery is not None
    assert delivery.occurrence_id == "occurrence-1"
    assert delivery.attempt == 1
    assert events == ["trigger_lease:occurrence-1"]
    kernel.acknowledge(delivery)
    assert kernel.claim_due() is None
