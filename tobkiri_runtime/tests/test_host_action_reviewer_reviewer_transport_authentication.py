"""Native encrypted lease proof authentication; test principal fixture only."""

from dataclasses import replace
from types import SimpleNamespace
import pytest
from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, LeaseState, authority_digest
from tests.test_authority_v4_lifecycle import _Harness, _digest
from core_runtime.bootstrap.action_review_v4.canonical_reviewer_port import (
    CapturedReviewerBoundary,
    GENERATE_TARGET,
)
from core_runtime.bootstrap.action_review_v4.reviewer_transport_authentication import (
    CommittedReviewerTransportAuthenticator,
)
import core_runtime.bootstrap.action_review_v4.reviewer_transport_authentication as authentication


def test_actual_native_store_committed_reviewer_lease_and_revocation(tmp_path, monkeypatch):
    scope = AuthorityScope(
        "operation.invoke",
        _digest("review-semantics"),
        dimensions={
            "contract": (GENERATE_TARGET[0],),
            "operation": (GENERATE_TARGET[1],),
            "request_surface": ("approval-review",),
            "tool_calling": ("false",),
            "allow_failover": ("false",),
        },
    )
    harness = _Harness(tmp_path, scope=scope, binding_scope=scope)
    # The fixture domains use fence 7. Advance through genuine aborted
    # reservations so the authoritative ledger reaches that exact fence.
    for sequence in range(1, 7):
        retired, _ = harness.store.reserve_activation(
            activation_id=f"aborted-{sequence}",
            profile_id="profile-1",
            plan_digest=_digest("plan"),
            profile_authority_digest=_digest("4"),
            security_epoch=1,
        )
        harness.store.transition_activation(retired, expected_state="prepared", new_state="aborted")
    reservation, fence = harness.store.reserve_activation(
        activation_id="activation-1",
        profile_id="profile-1",
        plan_digest=_digest("plan"),
        profile_authority_digest=_digest("4"),
        security_epoch=1,
    )
    assert fence == harness.target_domain.fencing_token
    for before, after in (
        ("prepared", "ready_without_authority"),
        ("ready_without_authority", "committing"),
        ("committing", "active"),
    ):
        harness.store.transition_activation(reservation, expected_state=before, new_state=after)
    result = harness.kernel.authorize(harness.context(effect_digest=scope.digest), scope)
    lease = harness.kernel.dispatch(
        result.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )
    capture = {
        "caller_principal_id": lease.caller.principal_id,
        "target_principal_id": lease.target.principal_id,
        **{
            k: getattr(lease, k)
            for k in (
                "caller_domain_id",
                "caller_boot_epoch",
                "target_domain_id",
                "target_boot_epoch",
                "profile_id",
                "activation_id",
                "activation_digest",
                "plan_digest",
                "profile_authority_digest",
                "fencing_token",
                "security_epoch",
                "provider_authority_id",
                "provider_authority_digest",
                "grant_id",
            )
        },
    }
    facts = {
        "profile_id": lease.profile_id,
        "model_reference": "fixture-reviewed-model",
        "caller_principal_id": lease.caller.principal_id,
        "authority_capture_digest": authority_digest(capture),
        "route_binding": {
            "model_id": "fixture/model",
            "provider_instance_id": "fixture/provider",
            "catalog_provider_instance_id": "fixture/catalog",
            "catalog_revision": "r1",
            "pricing_revision": "r1",
            "pricing": {"input": "0", "output": "0", "currency": "USD"},
        },
    }
    boundary = CapturedReviewerBoundary(facts, authority_digest(facts), lambda: None)
    verify = CommittedReviewerTransportAuthenticator(
        harness.store, boundary, capture, lambda: None, clock=harness.clock
    )
    evidence = {
        "transport_proof": {"lease_id": lease.lease_id, "request_digest": lease.request_digest},
        "transport_request_digest": lease.request_digest,
        "reviewer_boundary_digest": boundary.boundary_digest,
    }
    with pytest.raises(AuthorityDenied, match="not committed"):
        verify(evidence)
    harness.kernel.finish(lease.lease_id, state=LeaseState.COMMITTED, outcome_digest=_digest("7"))
    verify(evidence)
    with pytest.raises(AuthorityDenied):
        verify({**evidence, "transport_request_digest": _digest("wrong")})
    changed = CommittedReviewerTransportAuthenticator(
        harness.store,
        boundary,
        {**capture, "caller_domain_id": "foreign"},
        lambda: None,
        clock=harness.clock,
    )
    with pytest.raises(AuthorityDenied):
        changed(evidence)
    harness.store.revoke(target_kind="grant", target_id=lease.grant_id, reason="fixture revoke")
    with pytest.raises(AuthorityDenied, match="revoked"):
        verify(evidence)
    harness.store.close()
