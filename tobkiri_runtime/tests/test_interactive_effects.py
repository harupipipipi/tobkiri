"""Focused coverage for the Host-owned durable PendingEffect state machine."""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Mapping

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, authority_digest
from tobkiri_host.interactive_effects import (
    PendingEffectController,
    PendingEffectError,
    PendingEffectState,
)
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.ports import InteractiveApprovalStatus

from tests.test_tobkiri_host_execution_integration import context, frame, make_broker
from tests.test_authority_v4_lifecycle import _Harness


class _MemoryPendingEffects:
    """Revisioned Host persistence double which never exposes an owner index."""

    def __init__(self) -> None:
        self.records: dict[str, tuple[int, dict[str, Any]]] = {}

    def create_host_pending_effect(
        self,
        effect_id: str,
        payload: Mapping[str, Any],
    ) -> int:
        if effect_id in self.records:
            raise RuntimeError("duplicate")
        self.records[effect_id] = (1, copy.deepcopy(dict(payload)))
        return 1

    def get_host_pending_effect(
        self,
        effect_id: str,
    ) -> tuple[int, Mapping[str, Any]] | None:
        result = self.records.get(effect_id)
        if result is None:
            return None
        revision, payload = result
        return revision, copy.deepcopy(payload)

    def compare_and_swap_host_pending_effect(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        payload: Mapping[str, Any],
    ) -> int:
        revision, _stored = self.records[effect_id]
        if revision != expected_revision:
            raise RuntimeError("stale")
        next_revision = revision + 1
        self.records[effect_id] = (next_revision, copy.deepcopy(dict(payload)))
        return next_revision

    def list_host_pending_effects(self) -> list[tuple[int, Mapping[str, Any]]]:
        return [
            (revision, copy.deepcopy(payload))
            for revision, payload in self.records.values()
        ]


class _Approvals:
    """Narrow approval-port double with no Grant material surface."""

    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}
        self.expiry_overrides: dict[str, float] = {}
        self.commands: list[Any] = []
        self.attestations: list[Any] = []
        self.reject_attestation = False

    def request_interactive_approval(self, command: Any) -> InteractiveApprovalStatus:
        self.commands.append(command)
        self.statuses[command.context.request_id] = "pending"
        return self._status(command.context.request_id)

    def interactive_approval_status(self, request_id: str) -> InteractiveApprovalStatus:
        return self._status(request_id)

    def assert_interactive_approval_grant(self, attestation: Any) -> None:
        if self.reject_attestation:
            raise PermissionError("grant unavailable")
        if self.statuses.get(attestation.request_id) != "approved":
            raise PermissionError("grant unavailable")
        self.attestations.append(attestation)

    def approve(self, request_id: str) -> None:
        self.statuses[request_id] = "approved"

    def deny(self, request_id: str) -> None:
        self.statuses[request_id] = "denied"

    def expire(self, request_id: str) -> None:
        """Keep the request pending but move its expiry behind the clock."""

        self.expiry_overrides[request_id] = 50.0

    def _status(self, request_id: str) -> InteractiveApprovalStatus:
        state = self.statuses.get(request_id)
        if state is None:
            raise PermissionError("unknown")
        return InteractiveApprovalStatus(
            request_id=request_id,
            state=state,
            expires_at=self.expiry_overrides.get(request_id, 1_000.0),
            typed_confirmation_required=True,
            request_snapshot_digest="a" * 64,
            typed_confirmation_digest="b" * 64,
            redacted_metadata={"summary": "effect"},
        )


def _scope(request_digest: str, *, plan_digest: str) -> dict[str, object]:
    """Build the exact scope required by the pending-effect authority invariant."""

    return AuthorityScope(
        capability="effect.execute",
        semantics_digest=authority_digest({"semantics": "effect"}),
        dimensions={
            "invocation_owner_id": ("owner-1",),
            "caller_session_id": ("caller-session",),
            "plan_digest": (plan_digest,),
        },
        exact_request_digest=request_digest,
        opaque=True,
    ).to_dict()


def _controller(
    persistence: _MemoryPendingEffects,
    approvals: _Approvals,
) -> PendingEffectController:
    """Return a controller with the execution fixture's coordinator identity."""

    return PendingEffectController(
        persistence=persistence,
        approvals=approvals,
        coordinator_principal=OpaqueAuthorityRef("authority:caller"),
        coordinator_publisher_lineage="publisher.coordinator",
        clock=lambda: 100.0,
    )


def _prepare(
    controller: PendingEffectController,
    broker: Any,
    correlation_id: str | None = None,
) -> tuple[object, Any]:
    """Prepare one canonical Broker snapshot behind a pending approval."""

    current_context = context()
    prepared = broker.prepare(frame(), current_context)
    status = controller.prepare(
        prepared=prepared,
        context=current_context,
        effect_scope=_scope(
            prepared.request_digest,
            plan_digest=current_context.plan_digest,
        ),
        invocation_owner_id="owner-1",
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        presentation_metadata={
            "summary": "Send notification",
            "confirmation_phrase": "SEND",
        },
        expires_at=1_000.0,
        typed_confirmation_phrase="SEND",
        correlation_id=correlation_id,
    )
    return status, prepared


def test_prepare_reply_lookup_is_owned_read_only_and_survives_controller_restart() -> None:
    """A correlation ID recovers a receipt, never authority or an execution."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    correlation = str(uuid.uuid4())
    try:
        status, _ = _prepare(_controller(persistence, approvals), fixture.broker, correlation)
        original = copy.deepcopy(persistence.records)
        restarted = _controller(persistence, approvals)
        query = dict(
            correlation_id=correlation,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            context=context(),
            contract_id=frame().contract_id,
            operation_id=frame().operation_id,
            target_principal=OpaqueAuthorityRef("authority:notification-send"),
        )
        for _ in range(2):
            assert restarted.find_for_presentation(**query) == status
        for patch in (
            {"correlation_id": str(uuid.uuid4())},
            {"presentation_owner_principal_id": "authority:other"},
            {"presentation_owner_session_id": "other-session"},
            {"operation_id": "other-operation"},
            {"contract_id": "other.contract.v1"},
            {"target_principal": OpaqueAuthorityRef("authority:other-target")},
            {"context": replace(context(), profile_id="other")},
            {"context": replace(context(), activation_id="activation:other")},
            {"context": replace(context(), plan_digest="sha256:" + "f" * 64)},
            {"context": replace(context(), security_epoch=context().security_epoch + 1)},
        ):
            with pytest.raises(PendingEffectError, match="unavailable"):
                restarted.find_for_presentation(**{**query, **patch})
        assert persistence.records == original
        assert len(approvals.commands) == 1
        assert approvals.attestations == []
        _prepare(restarted, fixture.broker, correlation)
        duplicate_records = copy.deepcopy(persistence.records)
        with pytest.raises(PendingEffectError, match="unavailable"):
            restarted.find_for_presentation(**query)
        assert persistence.records == duplicate_records
    finally:
        fixture.broker.close()


@pytest.mark.parametrize("correlation", ["", "../receipt", "A" * 36, 42, True])
def test_invalid_prepare_correlation_cannot_create_effect(correlation: Any) -> None:
    """Malformed correlation fails before durable state or approval creation."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    try:
        with pytest.raises(PendingEffectError):
            _prepare(_controller(persistence, approvals), fixture.broker, correlation)
        assert persistence.records == {}
        assert approvals.commands == []
    finally:
        fixture.broker.close()


def test_prepare_uses_canonical_broker_snapshot_and_redacts_payload() -> None:
    """Pending state contains the Broker snapshot only in Host-private storage."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        status, prepared = _prepare(controller, fixture.broker)
    finally:
        fixture.broker.close()

    _revision, payload = persistence.records[status.effect_id]
    assert payload["prepared"] == prepared.to_snapshot().to_dict()
    assert payload["prepared"]["schema"] == "tobkiri.prepared-invocation.v1"
    assert "normalized_payload" not in status.__dict__
    assert "hello" not in repr(status)
    assert status.presentation_metadata["confirmation_phrase"] == "SEND"
    assert approvals.commands[0].target_principal.value == "authority:notification-send"
    assert (
        approvals.commands[0].presentation_owner_principal_id == "authority:presenter"
    )


def test_prepare_rejects_scope_without_exact_owner_session_and_plan_binding() -> None:
    """The pending store cannot be populated with a reusable approval scope."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    current_context = context()
    try:
        prepared = fixture.broker.prepare(frame(), current_context)
        scope = _scope(prepared.request_digest, plan_digest=current_context.plan_digest)
        scope["dimensions"] = {
            **scope["dimensions"],
            "invocation_owner_id": ["different-owner"],
        }
        with pytest.raises(PendingEffectError):
            controller.prepare(
                prepared=prepared,
                context=current_context,
                effect_scope=scope,
                invocation_owner_id="owner-1",
                presentation_owner_principal_id="authority:presenter",
                presentation_owner_session_id="presenter-session",
                presentation_metadata={"summary": "Send notification"},
                expires_at=1_000.0,
            )
    finally:
        fixture.broker.close()

    assert persistence.records == {}


def test_owner_scoped_status_resume_and_cancel_do_not_authorize_by_effect_id() -> None:
    """A foreign principal/session cannot inspect, resume, or cancel a known ID."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        for action in (
            lambda: controller.status_for_presentation(
                effect_id=pending.effect_id,
                presentation_owner_principal_id="authority:foreign",
                presentation_owner_session_id="presenter-session",
            ),
            lambda: controller.resume_for_presentation(
                effect_id=pending.effect_id,
                presentation_owner_principal_id="authority:presenter",
                presentation_owner_session_id="foreign-session",
                broker=fixture.broker,
            ),
            lambda: controller.cancel_for_presentation(
                effect_id=pending.effect_id,
                presentation_owner_principal_id="authority:foreign",
                presentation_owner_session_id="foreign-session",
            ),
        ):
            with pytest.raises(PendingEffectError):
                action()
        owned = controller.cancel_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
        )
    finally:
        fixture.broker.close()

    assert owned.state is PendingEffectState.CANCELLED


def test_owner_resume_returns_pending_without_claiming_or_dispatching() -> None:
    """An owned but unapproved effect remains pending and cannot run."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        result = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
        )
    finally:
        fixture.broker.close()

    assert result.state is PendingEffectState.APPROVAL_PENDING
    assert (
        controller.status(pending.effect_id).state
        is PendingEffectState.APPROVAL_PENDING
    )
    assert fixture.backend.invocations == 0


def test_authority_store_encrypts_pending_effect_payload_and_enforces_cas(
    tmp_path,
) -> None:
    """The persistence primitive leaves only opaque state metadata in SQLite."""

    harness = _Harness(tmp_path)
    payload = {
        "effect_id": "pending-effect-encrypted-1",
        "state": "prepared",
        "normalized_payload": {"credential": "secret-pending-effect-value"},
        "presentation_owner": "principal-private-value",
    }
    assert harness.store.create_host_pending_effect(payload["effect_id"], payload) == 1
    with harness.store._connection() as connection:  # Host-only storage inspection.
        row = connection.execute(
            "SELECT state, encrypted_payload FROM host_pending_effects"
        ).fetchone()

    assert row is not None
    assert row["state"] == "prepared"
    assert b"secret-pending-effect-value" not in bytes(row["encrypted_payload"])
    assert b"principal-private-value" not in bytes(row["encrypted_payload"])
    revision, restored = harness.store.get_host_pending_effect(
        payload["effect_id"]
    ) or (
        0,
        {},
    )
    assert revision == 1
    assert restored["normalized_payload"] == payload["normalized_payload"]
    updated = {**restored, "state": "approval_pending"}
    assert (
        harness.store.compare_and_swap_host_pending_effect(
            payload["effect_id"],
            expected_revision=1,
            payload=updated,
        )
        == 2
    )
    with pytest.raises(AuthorityDenied):
        harness.store.compare_and_swap_host_pending_effect(
            payload["effect_id"],
            expected_revision=1,
            payload=updated,
        )


def test_resume_marks_dispatched_before_provider_and_returns_only_status() -> None:
    """The Broker callback makes host dispatch durable before backend execution."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        def invoke(envelope: Any) -> Any:
            assert (
                controller.status(pending.effect_id).state
                is PendingEffectState.DISPATCHED
            )
            return fixture.backend.outcome

        fixture.backend.invoke = invoke
        complete = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
        )
    finally:
        fixture.broker.close()

    assert complete.state is PendingEffectState.SUCCEEDED
    assert "audit_dispatched" in fixture.events
    assert len(approvals.attestations) >= 2


@pytest.mark.parametrize("provider_fails", [False, True])
def test_resume_scopes_persisted_presentation_owner_and_releases_after_dispatch(provider_fails):
    fixture = make_broker()
    persistence, approvals = _MemoryPendingEffects(), _Approvals()
    bound = []

    @contextmanager
    def owner_scope(execute_context, principal, session):
        assert execute_context.caller_session_id == "caller-session"
        bound.append((principal, session))
        try:
            yield
        finally:
            bound.pop()

    controller = PendingEffectController(
        persistence=persistence, approvals=approvals,
        coordinator_principal=OpaqueAuthorityRef("authority:caller"),
        coordinator_publisher_lineage="publisher.coordinator",
        presentation_owner_scope=owner_scope, clock=lambda: 100.0,
    )
    try:
        pending, _ = _prepare(controller, fixture.broker)
        assert bound == []
        approvals.approve(pending.approval_request_id)

        def invoke(envelope):
            assert bound == [("authority:presenter", "presenter-session")]
            if provider_fails:
                raise RuntimeError("fixture failure")
            return fixture.backend.outcome

        fixture.backend.invoke = invoke
        result = controller.resume(
            pending.effect_id, fixture.broker,
            wall_clock=lambda: 100.0, monotonic_clock=lambda: 10.0,
        )
        assert result.state is (
            PendingEffectState.AMBIGUOUS if provider_fails else PendingEffectState.SUCCEEDED
        )
        assert bound == []
    finally:
        fixture.broker.close()


def test_resume_fails_closed_as_stale_before_dispatch() -> None:
    """Catalog/context drift before the callback is stale and never dispatched."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        fixture.broker._catalog = object()  # type: ignore[assignment]
        result = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
        )
    finally:
        fixture.broker.close()

    assert result.state is PendingEffectState.STALE
    assert fixture.backend.invocations == 0


def test_resume_conservatively_marks_post_dispatch_error_ambiguous() -> None:
    """Provider failure after durable dispatch cannot be retried as a clean failure."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        def invoke(_envelope: Any) -> Any:
            raise OSError("provider transport broke")

        fixture.backend.invoke = invoke
        result = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
        )
    finally:
        fixture.broker.close()

    assert result.state is PendingEffectState.AMBIGUOUS


def test_claim_rechecks_attestation_and_recovery_never_retries_dispatched_work() -> (
    None
):
    """A revoked one-shot grant is stale; a crash after dispatch is ambiguous."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        approvals.reject_attestation = True
        with pytest.raises(PendingEffectError):
            controller.claim(pending.effect_id)
        assert controller.status(pending.effect_id).state is PendingEffectState.STALE

        approvals.reject_attestation = False
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        controller.claim(pending.effect_id)
        controller.mark_dispatched(pending.effect_id)
        recovered = controller.recover()
    finally:
        fixture.broker.close()

    assert any(
        item.effect_id == pending.effect_id
        and item.state is PendingEffectState.AMBIGUOUS
        for item in recovered
    )


def _await_state(
    controller: PendingEffectController,
    effect_id: str,
    wanted: PendingEffectState | frozenset[PendingEffectState] | set,
    timeout: float = 5.0,
) -> Any:
    """Poll the durable state until it reaches ``wanted`` or the deadline."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = controller.status(effect_id)
        if isinstance(wanted, PendingEffectState):
            if status.state is wanted:
                return status
        elif status.state in wanted:
            return status
        time.sleep(0.01)
    return controller.status(effect_id)


def test_resume_returns_live_state_while_provider_still_runs() -> None:
    """A slow Provider cannot pin the caller past the bounded resume wait.

    The claim and durable dispatch marker stay synchronous, but Provider
    completion settles on a detached worker.  The caller receives the live
    CLAIMED/DISPATCHED state and the terminal outcome still lands durably.
    """

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    provider_release = threading.Event()
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        invocations = {"count": 0}

        def invoke(envelope: Any) -> Any:
            invocations["count"] += 1
            provider_release.wait(timeout=30.0)
            return fixture.backend.outcome

        fixture.backend.invoke = invoke
        result: dict[str, Any] = {}

        def run() -> None:
            result["status"] = controller.resume(
                pending.effect_id,
                fixture.broker,
                wall_clock=lambda: 100.0,
                monotonic_clock=lambda: 10.0,
                dispatch_grace_seconds=0.2,
            )

        caller = threading.Thread(target=run, daemon=True)
        caller.start()
        caller.join(timeout=5.0)
        assert not caller.is_alive(), "resume blocked on Provider completion"
        assert result["status"].state in {
            PendingEffectState.CLAIMED,
            PendingEffectState.DISPATCHED,
        }
        provider_release.set()
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.SUCCEEDED
        )
    finally:
        provider_release.set()
        fixture.broker.close()

    assert settled.state is PendingEffectState.SUCCEEDED
    assert invocations["count"] == 1


def test_resume_deadline_does_not_reenter_contended_store_after_dispatch() -> None:
    """The response deadline stays bounded while the durable worker continues."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    provider_entered = threading.Event()
    provider_release = threading.Event()
    original_get = persistence.get_host_pending_effect

    def contended_get(effect_id: str) -> tuple[int, Mapping[str, Any]] | None:
        if provider_entered.is_set():
            provider_release.wait(timeout=0.5)
        return original_get(effect_id)

    persistence.get_host_pending_effect = contended_get  # type: ignore[method-assign]
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        def invoke(envelope: Any) -> Any:
            provider_entered.set()
            provider_release.wait(timeout=5.0)
            return fixture.backend.outcome

        fixture.backend.invoke = invoke
        started = time.monotonic()
        status = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
            dispatch_grace_seconds=0.01,
        )
        elapsed = time.monotonic() - started
        assert status.state is PendingEffectState.DISPATCHED
        assert elapsed < 0.15
        provider_release.set()
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.SUCCEEDED
        )
    finally:
        provider_release.set()
        fixture.broker.close()

    assert settled.state is PendingEffectState.SUCCEEDED


def test_resume_deadline_bounds_claim_store_contention() -> None:
    """A blocked claim cannot extend the synchronous resume response budget."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    store_release = threading.Event()
    store_entered = threading.Event()
    original_get = persistence.get_host_pending_effect
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        def contended_get(effect_id: str) -> tuple[int, Mapping[str, Any]] | None:
            store_entered.set()
            store_release.wait(timeout=5.0)
            return original_get(effect_id)

        persistence.get_host_pending_effect = contended_get  # type: ignore[method-assign]
        started = time.monotonic()
        with pytest.raises(PendingEffectError, match="unavailable"):
            controller.resume_for_presentation(
                effect_id=pending.effect_id,
                presentation_owner_principal_id="authority:presenter",
                presentation_owner_session_id="presenter-session",
                broker=fixture.broker,
                dispatch_grace_seconds=0.01,
            )
        elapsed = time.monotonic() - started
        assert store_entered.is_set()
        assert elapsed < 0.15

        store_release.set()
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.SUCCEEDED
        )
    finally:
        store_release.set()
        fixture.broker.close()

    assert settled.state is PendingEffectState.SUCCEEDED


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX flock contention")
def test_owner_resume_deadline_bounds_real_authority_store_guard(
    tmp_path: Any,
) -> None:
    """A real AuthorityStore flock holder cannot outlive the response grace."""

    import fcntl

    fixture = make_broker()
    harness = _Harness(tmp_path)
    approvals = _Approvals()
    controller = _controller(harness.store, approvals)
    guard = open(harness.store._guard_path, "r+b")
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        started = time.monotonic()
        with pytest.raises(PendingEffectError, match="unavailable"):
            controller.resume_for_presentation(
                effect_id=pending.effect_id,
                presentation_owner_principal_id="authority:presenter",
                presentation_owner_session_id="presenter-session",
                broker=fixture.broker,
                dispatch_grace_seconds=0.01,
            )
        assert time.monotonic() - started < 0.15

        fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.SUCCEEDED
        )
    finally:
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        finally:
            guard.close()
            harness.store.close()
            fixture.broker.close()

    assert settled.state is PendingEffectState.SUCCEEDED


def test_resume_failure_after_grace_settles_ambiguous_asynchronously() -> None:
    """A Provider error after the caller returned still lands as ambiguous."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    provider_gate = threading.Event()
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        invocations = {"count": 0}

        def invoke(envelope: Any) -> Any:
            invocations["count"] += 1
            provider_gate.wait(timeout=30.0)
            raise OSError("provider transport broke")

        fixture.backend.invoke = invoke
        status = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
            dispatch_grace_seconds=0.2,
        )
        assert status.state in {
            PendingEffectState.CLAIMED,
            PendingEffectState.DISPATCHED,
        }
        provider_gate.set()
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.AMBIGUOUS
        )
    finally:
        provider_gate.set()
        fixture.broker.close()

    assert settled.state is PendingEffectState.AMBIGUOUS
    assert invocations["count"] == 1


def test_resume_replay_never_dispatches_twice() -> None:
    """Settled and in-flight resumes are idempotent reads, never re-executions."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    provider_release = threading.Event()
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        invocations = {"count": 0}

        def invoke(envelope: Any) -> Any:
            invocations["count"] += 1
            provider_release.wait(timeout=30.0)
            return fixture.backend.outcome

        fixture.backend.invoke = invoke
        first = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
            dispatch_grace_seconds=0.2,
        )
        assert first.state in {
            PendingEffectState.CLAIMED,
            PendingEffectState.DISPATCHED,
        }
        duplicate = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
        )
        assert duplicate.state in {
            PendingEffectState.CLAIMED,
            PendingEffectState.DISPATCHED,
        }
        with pytest.raises(PendingEffectError, match="unavailable"):
            controller.resume(
                pending.effect_id,
                fixture.broker,
                wall_clock=lambda: 100.0,
                monotonic_clock=lambda: 10.0,
                dispatch_grace_seconds=0.0,
            )
        provider_release.set()
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.SUCCEEDED
        )
        replayed = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
        )
    finally:
        provider_release.set()
        fixture.broker.close()

    assert settled.state is PendingEffectState.SUCCEEDED
    assert replayed.state is PendingEffectState.SUCCEEDED
    assert invocations["count"] == 1


def test_foreign_or_replayed_resume_cannot_settle_an_owned_live_dispatch() -> None:
    """Only the worker that won the claim may settle its dispatch failures."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    provider_entered = threading.Event()
    provider_release = threading.Event()
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)

        def invoke(envelope: Any) -> Any:
            provider_entered.set()
            provider_release.wait(timeout=5.0)
            return fixture.backend.outcome

        fixture.backend.invoke = invoke
        first = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
            dispatch_grace_seconds=0.05,
        )
        assert first.state is PendingEffectState.DISPATCHED
        assert provider_entered.is_set()

        with pytest.raises(PendingEffectError, match="unavailable"):
            controller.resume_for_presentation(
                effect_id=pending.effect_id,
                presentation_owner_principal_id="authority:foreign",
                presentation_owner_session_id="presenter-session",
                broker=fixture.broker,
                dispatch_grace_seconds=0.05,
            )
        replayed = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
            dispatch_grace_seconds=0.05,
        )
        assert replayed.state is PendingEffectState.DISPATCHED
        with pytest.raises(PendingEffectError, match="unavailable"):
            controller.resume(
                pending.effect_id,
                fixture.broker,
                dispatch_grace_seconds=0.05,
            )
        assert controller.status(pending.effect_id).state is PendingEffectState.DISPATCHED

        provider_release.set()
        settled = _await_state(
            controller, pending.effect_id, PendingEffectState.SUCCEEDED
        )
    finally:
        provider_release.set()
        fixture.broker.close()

    assert settled.state is PendingEffectState.SUCCEEDED
    assert fixture.backend.invocations == 1


def test_denied_approval_cancels_resume_without_dispatch() -> None:
    """Deny settles durably cancelled and never reaches the Provider."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.deny(pending.approval_request_id)
        result = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
        )
        status = controller.status_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
        )
    finally:
        fixture.broker.close()

    assert result.state is PendingEffectState.CANCELLED
    assert status.state is PendingEffectState.CANCELLED
    assert fixture.backend.invocations == 0


def test_expired_approval_fails_closed_as_cancelled() -> None:
    """A request pending past its expiry cannot be claimed or dispatched."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.expire(pending.approval_request_id)
        result = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
        )
    finally:
        fixture.broker.close()

    assert result.state is PendingEffectState.CANCELLED
    assert fixture.backend.invocations == 0


def test_resume_after_effect_expiry_fails_closed_before_dispatch() -> None:
    """An expired claimed effect never reaches the Provider boundary."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        status = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 5_000.0,
            monotonic_clock=lambda: 10.0,
            dispatch_grace_seconds=0.2,
        )
    finally:
        fixture.broker.close()

    assert status.state is PendingEffectState.STALE
    assert fixture.backend.invocations == 0


def test_resume_gate_overrun_never_dispatches_and_settles_stale() -> None:
    """A slow final Broker gate settles the claim stale, never ambiguous."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    clock = [10.0]
    recheck = fixture.authority.recheck_effect_boundary

    def slow_recheck(context_arg, target, lease) -> None:
        clock[0] += 1.0
        recheck(context_arg, target, lease)

    fixture.authority.recheck_effect_boundary = slow_recheck
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        status = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: clock[0],
            dispatch_grace_seconds=0.2,
        )
    finally:
        fixture.broker.close()

    assert status.state is PendingEffectState.STALE
    assert fixture.backend.invocations == 0
    assert "audit_dispatched" not in fixture.events


def test_resume_marker_overrun_never_invokes_and_settles_ambiguous() -> None:
    """A dispatch marker which overruns expiry stays honestly ambiguous."""

    fixture = make_broker()
    persistence = _MemoryPendingEffects()
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    clock = [10.0]
    mark_dispatched = fixture.audit.mark_dispatched

    def slow_mark(reservation) -> None:
        mark_dispatched(reservation)
        clock[0] += 1.0

    fixture.audit.mark_dispatched = slow_mark
    try:
        pending, _prepared = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        status = controller.resume(
            pending.effect_id,
            fixture.broker,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: clock[0],
            dispatch_grace_seconds=0.2,
        )
    finally:
        fixture.broker.close()

    assert status.state is PendingEffectState.AMBIGUOUS
    assert fixture.backend.invocations == 0
    assert "audit_dispatched" in fixture.events
    assert "provider_invoked" not in fixture.events
