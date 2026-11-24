"""Regression checks for Flow lifecycle over the captured prepared-attempt port."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import threading

import pytest

from core_runtime.workflow_v4.attempt_adapter import CapturedWorkflowAttemptAdapterV4
from core_runtime.workflow_v4.attempt_port import LateBoundWorkflowAttemptPortV4
from core_runtime.workflow_v4.attempt_service import HostWorkflowAttemptServiceV4
from core_runtime.workflow_v4.models import (
    DispatchAuthority, InvocationOutcome, WorkflowCancellationUnconfirmed,
    WorkflowDenied, digest,
)


def test_captured_adapter_and_late_port_forward_identical_fence():
    calls = []
    invocation = SimpleNamespace(assert_current=lambda: None)
    def fence(request_id):
        return None

    request = {"request_id": "sealed"}
    authority = DispatchAuthority("token", "reservation", "digest", 1)
    service = SimpleNamespace(invoke=lambda *args, **kwargs: calls.append((args, kwargs)))
    port = LateBoundWorkflowAttemptPortV4()
    port.bind(service)
    adapter = CapturedWorkflowAttemptAdapterV4(store=None, port=port, invocation=invocation)
    adapter.invoke(request, authority=authority, dispatch_fence=fence)
    assert calls == [((invocation, request), {"authority": authority, "dispatch_fence": fence})]


@pytest.fixture
def prepared_service(monkeypatch):
    events = []
    request = {"request_id": "sealed", "input": {"value": "pinned"}}
    envelope = SimpleNamespace(cancellation_requested=threading.Event(), deadline_monotonic=100)

    @contextmanager
    def track(reference):
        events.append(("track", reference))
        try:
            yield
        finally:
            events.append("untrack")

    invocation = SimpleNamespace(
        envelope=envelope, cancellation=SimpleNamespace(track=track),
        presentation_owner_principal_id="owner", presentation_owner_session_id="session",
    )
    snapshot = SimpleNamespace(request_digest="prepared-digest")
    record = {
        "request": request, "request_digest": digest(request), "state": "claimed",
        "snapshot": {"sealed": True}, "context": {}, "effect_scope": {},
        "owner_id": "owner", "expires_at": 200, "effect_id": None,
    }
    authority = DispatchAuthority("token", "reservation", digest(request), 1)
    service = HostWorkflowAttemptServiceV4.__new__(HostWorkflowAttemptServiceV4)
    service._lock = threading.RLock()
    service._tokens = {"token": ("reservation", envelope)}
    service._active = {}
    service._owned = lambda *args, **kwargs: (1, record)
    service._route = lambda value: "route"
    service._context = lambda *args: "context"
    service._scope = lambda *args: SimpleNamespace(to_dict=lambda: {})
    service._guard = lambda *args, **kwargs: None
    service._store = SimpleNamespace(cas=lambda *args: events.append("cas"))

    @contextmanager
    def evidence(route, current, sealed_request):
        assert route == "route" and current is invocation and sealed_request is record["request"]
        events.append("evidence")
        yield

    def dispatch(actual_snapshot, context, effect_scope, **kwargs):
        assert actual_snapshot is snapshot
        assert context == "context" and effect_scope == {}
        kwargs["before_dispatch"]()
        events.append("dispatch")
        return {"result": "ok"}

    service._config = SimpleNamespace(
        security_epoch=1, monotonic_clock=lambda: 1, clock=lambda: 1,
        execution_scope=lambda *args: nullcontext(), evidence_scope=evidence,
        broker=SimpleNamespace(invoke_prepared=dispatch),
    )
    monkeypatch.setattr("core_runtime.workflow_v4.attempt_service._context_document", lambda _: {})
    monkeypatch.setattr(
        "core_runtime.workflow_v4.attempt_service.PreparedInvocationSnapshot",
        SimpleNamespace(from_dict=lambda value: snapshot),
    )
    monkeypatch.setattr("core_runtime.workflow_v4.attempt_service.nested_cancellation_proof_for", lambda *args: None)
    return service, invocation, request, authority, events


def test_prepared_bridge_tracks_before_fence_and_keeps_snapshot(prepared_service):
    service, invocation, request, authority, events = prepared_service
    outcome = service.invoke(
        invocation, dict(request), authority=authority,
        dispatch_fence=lambda request_id: events.append(("fence", request_id)),
    )
    assert outcome == InvocationOutcome(output={"result": "ok"})
    assert events == [("track", "sealed"), ("fence", "sealed"), "evidence", "cas", "dispatch", "untrack"]


def test_fence_denial_never_opens_evidence_or_prepared_dispatch(prepared_service):
    service, invocation, request, authority, events = prepared_service

    def deny(_):
        raise WorkflowDenied("stopped")

    outcome = service.invoke(invocation, request, authority=authority, dispatch_fence=deny)
    assert outcome.dispatched is False
    assert outcome.ambiguous_effect is False
    assert events == [("track", "sealed"), "untrack"]


def test_prepared_dispatch_exception_stays_ambiguous_after_revoke(prepared_service):
    service, invocation, request, authority, events = prepared_service

    def dispatch(*args, **kwargs):
        service._owned()[1]["state"] = "ambiguous"
        raise ValueError("provider might have run")

    service._config.broker.invoke_prepared = dispatch
    outcome = service.invoke(invocation, request, authority=authority)
    assert outcome.dispatched is True
    assert outcome.ambiguous_effect is True
    assert events[-1] == "untrack"


@pytest.mark.parametrize("principal,active,verified,expected", [
    ("execute", True, True, False),
    ("stop", False, True, False),
    ("stop", True, False, False),
    ("stop", True, True, True),
])
def test_cancel_needs_exact_stop_role_and_verified_drain(principal, active, verified, expected):
    service = HostWorkflowAttemptServiceV4.__new__(HostWorkflowAttemptServiceV4)
    service._config = SimpleNamespace(profile_id="profile", stop_principals=("stop",))
    service.revoke = lambda *args, **kwargs: None
    observed = []
    observation = SimpleNamespace(wait_for_verified_drain=lambda deadline: observed.append(deadline) or verified)
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(target_principal=principal, deadline_monotonic=100),
        cancellation=SimpleNamespace(
            active_for=lambda _: active, can_request=lambda _: True,
            request=lambda _: observation,
        ),
    )
    if expected:
        service.cancel(invocation, "request")
        assert observed == [100]
    else:
        with pytest.raises(WorkflowCancellationUnconfirmed):
            service.cancel(invocation, "request")
        if principal == "execute" or not active:
            assert observed == []


def test_context_callback_receives_trusted_reservation_identity():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Context:
        caller_principal: str = "coordinator"
        profile_id: str = "profile"
        activation_id: str = "activation"
        activation_digest: str = "activation-digest"
        plan_digest: str = "plan"
        security_epoch: int = 1
        delegation_chain: tuple = ()
        request_id: str = ""
        trace_id: str = ""

    seen = []
    invocation = object()
    route = object()
    service = HostWorkflowAttemptServiceV4.__new__(HostWorkflowAttemptServiceV4)
    service._config = SimpleNamespace(
        coordinator_principal="coordinator", profile_id="profile",
        activation_id="activation", activation_digest="activation-digest",
        plan_digest="plan", security_epoch=1,
        context_for_attempt=lambda *args: seen.append(args) or Context(),
    )
    context = service._context(invocation, route, "trusted-reservation")
    assert seen == [(route, invocation, "trusted-reservation")]
    assert context.request_id == context.trace_id == "trusted-reservation"


@pytest.fixture
def cross_operation_service():
    from tobkiri_host.models import OpaqueAuthorityRef

    advance = OpaqueAuthorityRef("advance-principal")
    resume = OpaqueAuthorityRef("resume-principal")
    canonical = OpaqueAuthorityRef("canonical-step-execute")
    contract = "tobkiri.workflow.v4"
    service = HostWorkflowAttemptServiceV4.__new__(HostWorkflowAttemptServiceV4)
    service._config = SimpleNamespace(
        coordinator_principal=canonical,
        admitted_invocations=((contract, "run.advance", advance),
                              (contract, "run.step.resume", resume)),
        stop_principals=(), assert_current_capture=lambda: None,
        profile_id="profile", activation_id="activation",
        activation_digest="activation-digest", plan_digest="plan", security_epoch=1,
        monotonic_clock=lambda: 1,
    )

    def invocation(operation, principal, session, owner="owner"):
        return SimpleNamespace(
            assert_current=lambda: None,
            presentation_owner_principal_id=owner,
            presentation_owner_session_id="authenticated-owner-session",
            envelope=SimpleNamespace(
                contract_id=contract, operation_id=operation, target_principal=principal,
                cancellation_requested=threading.Event(), deadline_monotonic=100,
                context=SimpleNamespace(
                    profile_id="profile", activation_id="activation",
                    activation_digest="activation-digest", plan_digest="plan", security_epoch=1,
                    caller_session_id=session,
                ),
            ),
        )

    original = invocation("run.advance", advance, "advance-host-session")
    fresh = invocation("run.step.resume", resume, "fresh-resume-host-session")
    record = {"owner": service._owner(original), "request": {"sealed": True}}
    service._store = SimpleNamespace(get=lambda _: (1, record))
    service._route = lambda _: "canonical-route"
    return service, original, fresh, record, invocation


def test_distinct_operation_parent_keeps_authenticated_owner(cross_operation_service):
    service, original, fresh, record, _ = cross_operation_service
    assert original.envelope.target_principal != fresh.envelope.target_principal
    assert original.envelope is not fresh.envelope
    service._guard(original)
    service._guard(fresh)
    assert service._owned(fresh, "reservation") == (1, record)
    assert "caller_session_id" not in record["owner"]
    assert service._config.coordinator_principal.value == "canonical-step-execute"


def test_cross_operation_parent_cannot_forge_admission_tuple(cross_operation_service):
    service, original, fresh, _, _ = cross_operation_service
    fresh.envelope.target_principal = original.envelope.target_principal
    with pytest.raises(WorkflowDenied, match="invocation"):
        service._guard(fresh)


def test_cross_operation_foreign_owner_is_denied(cross_operation_service):
    service, _, fresh, _, _ = cross_operation_service
    fresh.presentation_owner_principal_id = "foreign-owner"
    with pytest.raises(WorkflowDenied, match="reservation"):
        service._owned(fresh, "reservation")


def test_legacy_immediate_session_owner_is_not_silently_rewritten(cross_operation_service):
    service, original, fresh, record, _ = cross_operation_service
    record["owner"]["caller_session_id"] = original.envelope.context.caller_session_id
    with pytest.raises(WorkflowDenied, match="reservation"):
        service._owned(fresh, "reservation")
    assert record["owner"]["caller_session_id"] == "advance-host-session"
