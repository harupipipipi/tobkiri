"""Dedicated hook transition tests; native grant proof is covered separately."""

from types import SimpleNamespace
import pytest
from core_runtime.authority.v4 import AuthorityDenied
from tests.test_interactive_effects import _MemoryPendingEffects, _Approvals, _scope
from tests.test_tobkiri_host_execution_integration import make_broker, context, frame
from tobkiri_host.interactive_effects import (
    PendingEffectController,
    PendingEffectState,
    PendingEffectError,
)
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.policy_effect_authorization import PolicyDerivedEffectPlan


class Port:
    """Isolated fixture, never a production authorization source."""

    current = True

    def assert_effect_policy(self, attestation):
        if not self.current:
            raise AuthorityDenied("revoked fixture")

    def settle_effect_policy(self, attestation, inherited, owned_guard):
        self.assert_effect_policy(attestation)
        if inherited is None:
            raise AuthorityDenied("fresh parent required")
        owned_guard()
        return SimpleNamespace(
            scope=attestation.effect_scope,
            grant_id="fixture-grant",
            assert_current=lambda: self.assert_effect_policy(attestation),
        )


def fixture():
    broker = make_broker().broker
    persistence, approvals, port = _MemoryPendingEffects(), _Approvals(), Port()
    controller = PendingEffectController(
        persistence=persistence,
        approvals=approvals,
        policy_authorizations=port,
        coordinator_principal=OpaqueAuthorityRef("authority:caller"),
        coordinator_publisher_lineage="publisher.coordinator",
        clock=lambda: 100.0,
    )
    ctx = context()
    prepared = broker.prepare(frame(), ctx)
    plan = PolicyDerivedEffectPlan(
        "selection",
        "real-root-request",
        "boundary",
        "saved-root",
        "turn",
        "/workspace",
        prepared.request_digest,
    )
    status = controller.prepare(
        prepared=prepared,
        context=ctx,
        effect_scope=_scope(prepared.request_digest, plan_digest=ctx.plan_digest),
        invocation_owner_id="owner-1",
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        presentation_metadata={},
        expires_at=1000.0,
        policy_plan=plan,
    )
    return broker, approvals, port, controller, status


def test_no_future_native_approval_and_fresh_parent_claim():
    broker, approvals, port, controller, status = fixture()
    try:
        assert status.state is PendingEffectState.APPROVED
        assert approvals.commands == [] and approvals.attestations == []
        assert (
            controller.claim(status.effect_id, policy_inheritance=object()).state
            is PendingEffectState.CLAIMED
        )
        with pytest.raises(PendingEffectError):
            controller.claim(status.effect_id, policy_inheritance=object())
    finally:
        broker.close()


def test_revoked_policy_stales_before_claim():
    broker, approvals, port, controller, status = fixture()
    try:
        port.current = False
        assert controller.observe_approval(status.effect_id).state is PendingEffectState.STALE
        with pytest.raises(PendingEffectError):
            controller.claim(status.effect_id, policy_inheritance=object())
        assert approvals.commands == []
    finally:
        broker.close()


def test_missing_fresh_parent_terminally_stales_policy_effect():
    broker, approvals, port, controller, status = fixture()
    try:
        with pytest.raises(AuthorityDenied):
            controller.claim(status.effect_id)
        assert controller.status(status.effect_id).state is PendingEffectState.STALE
    finally:
        broker.close()


def test_selected_policy_resumes_on_live_host_call_even_with_zero_dispatch_grace():
    """Scheduling keeps genuine Host parent alive; native async has own tests."""
    import threading

    broker, approvals, port, controller, status = fixture()
    original = broker.invoke_prepared
    observed = []
    caller = threading.get_ident()

    def invoke(*args, **kwargs):
        observed.append(threading.get_ident())
        return original(*args, **kwargs)

    broker.invoke_prepared = invoke
    try:
        result = controller.resume(
            status.effect_id,
            broker,
            policy_inheritance=object(),
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
            dispatch_grace_seconds=0.0,
        )
        assert result.state is PendingEffectState.SUCCEEDED
        assert observed == [caller] and approvals.commands == []
    finally:
        broker.close()


def test_authenticated_review_denial_survives_private_broker_wrapping():
    """Preserve only the exact authenticated type through terminal settlement."""
    from tobkiri_host.policy_review_errors import AuthenticatedPolicyReviewDenied

    broker, approvals, port, controller, status = fixture()
    denial = AuthenticatedPolicyReviewDenied("危険な操作のため停止")

    def invoke(*args, **kwargs):
        raise RuntimeError("private provider detail") from denial

    broker.invoke_prepared = invoke
    try:
        with pytest.raises(PendingEffectError) as caught:
            controller.resume(
                status.effect_id,
                broker,
                policy_inheritance=object(),
                wall_clock=lambda: 100.0,
                monotonic_clock=lambda: 10.0,
            )
        assert caught.value.__cause__ is denial
        assert str(caught.value) == "pending effect is unavailable"
        assert denial.safe_public_reason == "危険な操作のため停止"
    finally:
        broker.close()
