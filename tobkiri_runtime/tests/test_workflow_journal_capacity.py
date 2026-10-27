"""Isolated persistence/lifecycle proofs, never provider or real audio executions.

Production is capped at 32 MiB; these tests use small exact byte ceilings. Data is synthetic; files live in pytest's temporary dirs.
Service guard, route, reserve, commit, finish, journal encryption, locking and
CAS paths are unmodified. Broker/authority/approval ports are fixture doubles.
"""

import copy
import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace as NS

import pytest

from core_runtime.authority.v4 import AuthorityScope
from core_runtime.workflow_v4 import attempt_store
from core_runtime.workflow_v4.attempt_capacity import completion_headroom
from core_runtime.workflow_v4.attempt_port import (
    CapturedWorkflowAttemptRouteV4,
    WorkflowAttemptServiceConfigV4,
)
from core_runtime.workflow_v4.attempt_service import HostWorkflowAttemptServiceV4
from core_runtime.workflow_v4.models import WorkflowDenied, digest
from tobkiri_host.broker import PreparedInvocationSnapshot
from tobkiri_host.interactive_effects import PendingEffectError, PendingEffectState
from tobkiri_host.models import OpaqueAuthorityRef
from tests.test_interactive_effects import _Approvals
from tests.test_tobkiri_host_execution_integration import context


def raw_size(store):
    return len(
        json.dumps(
            store._read(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def make_case(tmp_path, *, mode="profile_grant", clock=lambda: 100.0):
    ctx = context()
    approvals = _Approvals()
    operation = NS(
        contract_id="example.echo.v1",
        contract_version="1.0.0",
        revision_digest=digest("revision"),
        operation_id="echo",
        input_schema={},
        effect_class=NS(value="capability:echo"),
    )
    binding = NS(
        operation=operation,
        principal_ref=OpaqueAuthorityRef("example.echo.provider"),
        function=NS(function_id="example.echo"),
        artifact=NS(),
    )
    ceiling = AuthorityScope(
        capability="effect.execute", semantics_digest=digest("semantics")
    )
    route = CapturedWorkflowAttemptRouteV4(
        caller_principal=ctx.caller_principal,
        binding=binding,
        caller_effect_ceiling=ceiling,
        authority_mode=mode,
    )

    def prepare(frame, child_ctx):
        snapshot = PreparedInvocationSnapshot(
            contract_id=frame.contract_id,
            contract_version="1.0.0",
            operation_id=frame.operation_id,
            normalized_payload=dict(frame.payload),
            request_digest=digest(frame.payload),
            timeout_ms=frame.timeout_ms,
            idempotency_key=frame.idempotency_key,
            binding_fingerprint={
                "target_principal": binding.principal_ref.value,
                "artifact": {"publisher_lineage": "publisher.example"},
            },
            context_fingerprint={},
            allow_lossy_adapters=False,
        )
        return NS(
            binding=binding,
            request_digest=snapshot.request_digest,
            to_snapshot=lambda: snapshot,
        )

    config = WorkflowAttemptServiceConfigV4(
        broker=NS(prepare=prepare),
        authority=NS(check_static_path=lambda _: None),
        approvals=approvals,
        routes=(route,),
        context_for_attempt=lambda *_: ctx,
        execution_scope=lambda *_: nullcontext(),
        presentation_owner_scope=lambda *_: nullcontext(),
        assert_current_capture=lambda: None,
        state_path=tmp_path / "attempts.enc",
        coordinator_principal=ctx.caller_principal,
        coordinator_publisher_lineage="publisher.coordinator",
        profile_id=ctx.profile_id,
        activation_id=ctx.activation_id,
        activation_digest=ctx.activation_digest,
        plan_digest=ctx.plan_digest,
        security_epoch=ctx.security_epoch,
        admitted_invocations=(
            ("tobkiri.workflow.v4", "run.step.execute", ctx.caller_principal),
        ),
        clock=clock,
        monotonic_clock=lambda: 100.0,
    )
    service = HostWorkflowAttemptServiceV4(config)
    invocation = NS(
        assert_current=lambda: None,
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        envelope=NS(
            target_principal=ctx.caller_principal,
            contract_id="tobkiri.workflow.v4",
            operation_id="run.step.execute",
            cancellation_requested=threading.Event(),
            deadline_monotonic=1000.0,
            context=ctx,
        ),
    )
    run = dict(
        run_id="run",
        catalog_digest=digest("catalog"),
        activation_id=ctx.activation_id,
        activation_digest=ctx.activation_digest,
        security_epoch=ctx.security_epoch,
    )

    def attempt(index, size=8192):
        request = dict(
            request_id=f"request-{index}",
            contract_id=operation.contract_id,
            contract_revision_digest=operation.revision_digest,
            operation_id=operation.operation_id,
            function_principal_id=binding.principal_ref.value,
            provider_id=binding.function.function_id,
            input_schema_digest=digest({}),
            effect_ceiling=["capability:echo"],
            input={"audio_base64": "A" * size},
            timeout_ms=1000,
            idempotency_key=f"idempotency-{index}",
        )
        return dict(
            request=request, request_digest=digest(request), step_id=f"step-{index}"
        )

    return NS(
        service=service,
        invocation=invocation,
        config=config,
        run=run,
        attempt=attempt,
        approvals=approvals,
    )


def budget(store):
    return raw_size(store) + completion_headroom(store._read())


def claim(case, reservation):
    if reservation.approval_request_id:
        case.approvals.approve(reservation.approval_request_id)
    return case.service.commit(
        case.invocation,
        reservation.reservation_id,
        request_digest=reservation.request_digest,
        security_epoch=case.config.security_epoch,
    )


def dispatch_marker(case, reservation):
    """Seed the same durable marker as before_dispatch, not a provider result."""
    revision, record = case.service._store.get(reservation.reservation_id)
    if record["effect_id"]:
        case.service._controller.mark_dispatched(record["effect_id"])
    record["state"] = "dispatched"
    case.service._store.cas(reservation.reservation_id, revision, record)


def reserve_pair_at_exact_cap(tmp_path, monkeypatch, mode, *, clock=lambda: 100.0):
    monkeypatch.setattr(attempt_store, "_MAX_BYTES", 65536)
    probe = make_case(tmp_path / "probe", mode=mode, clock=clock)
    for index in range(2):
        probe.service.reserve(probe.invocation, probe.run, probe.attempt(index, 512))
    cap = budget(probe.service._store)
    assert cap < 65536
    monkeypatch.setattr(attempt_store, "_MAX_BYTES", cap)
    case = make_case(tmp_path / "case", mode=mode, clock=clock)
    reservations = [
        case.service.reserve(case.invocation, case.run, case.attempt(index, 512))
        for index in range(2)
    ]
    assert budget(case.service._store) == cap
    original = case.service._store._read()
    with pytest.raises(WorkflowDenied, match="capacity is exhausted"):
        case.service.reserve(case.invocation, case.run, case.attempt(2, 0))
    assert case.service._store._read() == original
    return case, reservations, cap


@pytest.mark.parametrize("mode", ["profile_grant", "interactive_only"])
@pytest.mark.parametrize("final", ["succeeded", "failed", "ambiguous_effect"])
def test_exact_admission_budget_protects_all_active_finishes(
    tmp_path,
    monkeypatch,
    mode,
    final,
):
    clock = [100]  # Exercise normalization of an integer clock as well.
    case, reservations, cap = reserve_pair_at_exact_cap(
        tmp_path,
        monkeypatch,
        mode,
        clock=lambda: clock[0],
    )
    previous_budget = cap
    for reservation in reservations:
        claim(case, reservation)
        assert budget(case.service._store) <= previous_budget
        previous_budget = budget(case.service._store)
        dispatch_marker(case, reservation)
        assert budget(case.service._store) <= previous_budget
        previous_budget = budget(case.service._store)
    # An arbitrary finite int clock becomes binary64, never a huge JSON int.
    clock[0] = 10**100
    for reservation in reservations:
        case.service.finish(
            case.invocation,
            reservation.reservation_id,
            state=final,
            outcome_digest=digest("successful-output"),
        )
        assert budget(case.service._store) <= previous_budget
        previous_budget = budget(case.service._store)
        assert raw_size(case.service._store) <= cap
        _, row = case.service._store.get(reservation.reservation_id)
        assert row["state"] == ("ambiguous" if final == "ambiguous_effect" else final)
        assert row["outcome_digest"] == digest("successful-output")
        assert row["request"]["input"]["audio_base64"] == "A" * 512
        assert row["snapshot"]["normalized_payload"]["audio_base64"] == "A" * 512
        if row["effect_id"]:
            pending = case.service._store.get_host_pending_effect(row["effect_id"])[1]
            assert (
                pending["prepared"]["normalized_payload"]["audio_base64"] == "A" * 512
            )
            assert pending["state"] == row["state"]
            assert isinstance(pending["updated_at"], float)
    # Authentication/encryption and original replay/idempotency fences survive.
    ciphertext = case.config.state_path.read_bytes()
    assert b'"attempts"' not in ciphertext and b"A" * 512 not in ciphertext
    restarted = HostWorkflowAttemptServiceV4(case.config)
    assert restarted._store._read() == case.service._store._read()
    with pytest.raises(WorkflowDenied, match="requires reconciliation"):
        duplicate = case.attempt(99, 0)
        duplicate["request"]["idempotency_key"] = "idempotency-0"
        duplicate["request_digest"] = digest(duplicate["request"])
        restarted.reserve(case.invocation, case.run, duplicate)


@pytest.mark.parametrize("mode", ["profile_grant", "interactive_only"])
@pytest.mark.parametrize("stage", ["reserved", "claimed", "dispatched"])
def test_cancel_at_exact_budget_retains_pre_post_dispatch_semantics(
    tmp_path,
    monkeypatch,
    mode,
    stage,
):
    case, reservations, cap = reserve_pair_at_exact_cap(tmp_path, monkeypatch, mode)
    for reservation in reservations:
        if stage != "reserved":
            claim(case, reservation)
        if stage == "dispatched":
            dispatch_marker(case, reservation)
    for reservation in reservations:
        case.service.revoke(case.invocation, reservation.reservation_id, reason="test")
        _, row = case.service._store.get(reservation.reservation_id)
        assert row["state"] == ("cancelled" if stage == "reserved" else "ambiguous")
        if row["effect_id"]:
            pending = case.service._store.get_host_pending_effect(row["effect_id"])[1]
            assert pending["state"] == (
                "ambiguous" if stage == "dispatched" else "cancelled"
            )
        assert raw_size(case.service._store) <= cap


@pytest.mark.parametrize("mode", ["profile_grant", "interactive_only"])
@pytest.mark.parametrize("stage", ["claimed", "dispatched"])
def test_recovery_at_exact_budget_retains_ambiguity(
    tmp_path,
    monkeypatch,
    mode,
    stage,
):
    clock = [100.0]
    case, reservations, cap = reserve_pair_at_exact_cap(
        tmp_path,
        monkeypatch,
        mode,
        clock=lambda: clock[0],
    )
    for reservation in reservations:
        claim(case, reservation)
        if stage == "dispatched":
            dispatch_marker(case, reservation)
    clock[0] = 1.7976931348623157e308
    recovered = HostWorkflowAttemptServiceV4(case.config)
    for reservation in reservations:
        row = recovered._store.get(reservation.reservation_id)[1]
        assert row["state"] == "ambiguous"
        assert "outcome_digest" not in row
        if row["effect_id"]:
            pending = recovered._store.get_host_pending_effect(row["effect_id"])[1]
            assert pending["state"] == ("stale" if stage == "claimed" else "ambiguous")
    assert raw_size(recovered._store) <= cap


@pytest.mark.parametrize(
    "bad", ["", "sha256:" + "f" * 65, "sha256:" + "G" * 64, "X" * 8000, None]
)
@pytest.mark.parametrize("mode", ["profile_grant", "interactive_only"])
def test_invalid_digest_cannot_partially_finish_pending(tmp_path, mode, bad):
    case = make_case(tmp_path, mode=mode)
    reservation = case.service.reserve(case.invocation, case.run, case.attempt(1, 10))
    claim(case, reservation)
    dispatch_marker(case, reservation)
    previous = case.service._store._read()
    with pytest.raises(WorkflowDenied, match="cannot be finished"):
        case.service.finish(
            case.invocation,
            reservation.reservation_id,
            state="succeeded",
            outcome_digest=bad,
        )
    assert case.service._store._read() == previous


def test_interactive_pending_admission_refuses_before_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(attempt_store, "_MAX_BYTES", 65536)
    probe = make_case(tmp_path / "probe", mode="profile_grant")
    probe.service.reserve(probe.invocation, probe.run, probe.attempt(1, 100))
    # Enough for the interactive preparing attempt, not its additional full
    # pending snapshot. That second admission occurs before approval opens.
    cap = budget(probe.service._store) + 100
    monkeypatch.setattr(attempt_store, "_MAX_BYTES", cap)
    case = make_case(tmp_path / "case", mode="interactive_only")

    def unexpected_approval(*args, **kwargs):
        pytest.fail("No approval may open for a capacity-rejected pending row")

    monkeypatch.setattr(
        case.approvals, "request_interactive_approval", unexpected_approval
    )
    with pytest.raises(PendingEffectError):
        case.service.reserve(case.invocation, case.run, case.attempt(1, 100))
    assert case.service._store.list_host_pending_effects() == []
    rows = case.service._store.list_attempts()
    assert len(rows) == 1 and rows[0][1]["state"] == "preparing"
    recovered = HostWorkflowAttemptServiceV4(case.config)
    assert recovered._store.list_attempts()[0][1]["state"] == "stale"
    assert raw_size(recovered._store) <= cap


def test_legacy_revision_digit_growth_and_normalization_are_budgeted(
    tmp_path, monkeypatch
):
    case = make_case(tmp_path, mode="interactive_only")
    reservation = case.service.reserve(case.invocation, case.run, case.attempt(1, 100))
    # Isolated authenticated legacy fixture: a wide revision, omitted optional
    # controller fields and integer time values; never rewrite production data.
    document = case.service._store._read()
    for namespace in ("attempts", "pending"):
        for row in document[namespace].values():
            row["revision"] = 99
    pending = next(iter(document["pending"].values()))["payload"]
    for key in (
        "kind",
        "correlation_id",
        "authorization_kind",
        "policy_plan",
        "policy_derived_grant_id",
    ):
        pending.pop(key)
    pending["created_at"] = pending["updated_at"] = 100
    pending["expires_at"] = 400
    case.service._store._write(document)
    cap = budget(case.service._store)
    monkeypatch.setattr(attempt_store, "_MAX_BYTES", cap)
    claim(case, reservation)
    dispatch_marker(case, reservation)
    case.service.finish(
        case.invocation,
        reservation.reservation_id,
        state="succeeded",
        outcome_digest=digest("output"),
    )
    assert raw_size(case.service._store) <= cap
    assert case.service._store.get(reservation.reservation_id)[0] == 102
    assert case.service._store.get_host_pending_effect(pending["effect_id"])[0] == 103


def test_legacy_cas_not_newly_blocked_by_unfunded_sibling(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    one = case.service.reserve(case.invocation, case.run, case.attempt(1, 10))
    case.service.reserve(case.invocation, case.run, case.attempt(2, 10))
    claim(case, one)
    dispatch_marker(case, one)
    # Legacy files may fit actual bytes without the new aggregate allowance.
    # Add exactly the terminal digest bytes; sibling's headroom remains unfunded.
    cap = raw_size(case.service._store) + 90  # dispatched -> succeeded saves 1 byte.
    assert budget(case.service._store) > cap
    monkeypatch.setattr(attempt_store, "_MAX_BYTES", cap)
    case.service.finish(
        case.invocation,
        one.reservation_id,
        state="succeeded",
        outcome_digest=digest("output"),
    )
    assert raw_size(case.service._store) == cap
    assert case.service._store.get(one.reservation_id)[1]["state"] == "succeeded"


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_pending_clock_is_rejected_before_admission(tmp_path, clock_value):
    case = make_case(tmp_path, mode="interactive_only", clock=lambda: clock_value)
    with pytest.raises((WorkflowDenied, ValueError)):
        case.service.reserve(case.invocation, case.run, case.attempt(1, 10))
    assert case.service._store.list_host_pending_effects() == []


def test_pending_transition_bounds_cover_the_controller_graph():
    from core_runtime.workflow_v4.attempt_capacity import _PENDING_REMAINING
    from tobkiri_host.interactive_effects import _permits_transition

    for current in PendingEffectState:
        for future in PendingEffectState:
            if _permits_transition(current, future):
                assert _PENDING_REMAINING[current.value] >= (
                    1 + _PENDING_REMAINING.get(future.value, 0)
                )
                assert len(future.value) <= len("approval_pending")


@pytest.mark.parametrize("mode", ["profile_grant", "interactive_only"])
def test_terminal_replay_remains_inspect_only_and_exact(tmp_path, mode):
    case = make_case(tmp_path, mode=mode)
    attempt = case.attempt(1, 100)
    reservation = case.service.reserve(case.invocation, case.run, attempt)
    claim(case, reservation)
    dispatch_marker(case, reservation)
    case.service.finish(
        case.invocation,
        reservation.reservation_id,
        state="succeeded",
        outcome_digest=digest("output"),
    )
    original = case.service._store._read()
    replay = case.service.reserve(case.invocation, case.run, attempt)
    assert replay.state.value == "revoked"
    assert case.service._store._read() == original
    with pytest.raises(WorkflowDenied, match="not authorized"):
        claim(case, replay)
    changed = copy.deepcopy(attempt)
    changed["request"]["input"]["audio_base64"] = "changed"
    changed["request_digest"] = digest(changed["request"])
    with pytest.raises(WorkflowDenied, match="retransmission changed"):
        case.service.reserve(case.invocation, case.run, changed)
    assert case.service._store._read() == original
