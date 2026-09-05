"""Focused coverage for the Host-owned durable PendingEffect state machine."""

from __future__ import annotations

import copy
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

    def _status(self, request_id: str) -> InteractiveApprovalStatus:
        state = self.statuses.get(request_id)
        if state is None:
            raise PermissionError("unknown")
        return InteractiveApprovalStatus(
            request_id=request_id,
            state=state,
            expires_at=1_000.0,
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
    )
    return status, prepared


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
