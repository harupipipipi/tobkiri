"""Root approval revocation must fence grants to dependency artifacts too."""

from dataclasses import replace

import pytest

from core_runtime.authority.pack_approval_binding import pack_approval_snapshot_digest
from core_runtime.authority.v4_models import AuthorityDenied
from tests.test_authority_v4_lifecycle import _digest, _Harness


def _bind_root_decision(harness, revision):
    reservation, _fence = harness.store.reserve_activation(
        activation_id="activation-1", profile_id="profile-1",
        plan_digest=_digest("plan"), profile_authority_digest=_digest("4"),
        security_epoch=1,
    )
    for before, after in (
        ("prepared", "ready_without_authority"),
        ("ready_without_authority", "committing"),
        ("committing", "active"),
    ):
        harness.store.transition_activation(reservation, expected_state=before, new_state=after)
    approval = replace(
        harness.approval,
        snapshot_digest=pack_approval_snapshot_digest(
            profile_id="profile-1", activation_id="activation-1",
            plan_digest=_digest("plan"), profile_authority_digest=_digest("4"),
            security_epoch=1, scope=harness.scope.to_dict(), approval_revision=revision,
        ),
    )
    harness.store.put_record(approval, replace=True)


def test_root_revoke_fences_dependency_grant_and_already_issued_lease(tmp_path):
    harness = _Harness(tmp_path)
    revision = _digest("root-decision")
    _bind_root_decision(harness, revision)
    lease = harness.kernel.authorize(harness.context(), harness.scope)
    _revocation, grants = harness.store.revoke_pack_approval(
        pack_id="root.pack", approval_revision=revision,
        profile_id="profile-1", activation_id="activation-1",
        artifact_digest=_digest("root-artifact"), reason="revoke root",
    )
    assert grants == (harness.grant.grant_id,)
    assert harness.store.is_revoked("grant", harness.grant.grant_id)
    with pytest.raises(AuthorityDenied):
        harness.kernel.dispatch(
            lease.lease_token, target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch, request_digest=_digest("5"),
        )
    assert all(event["event_state"] != "dispatched" for event in harness.store.audit_events())


@pytest.mark.parametrize("mismatch", ["revision", "profile", "activation"])
def test_other_root_or_activation_grants_are_not_selected(tmp_path, mismatch):
    harness = _Harness(tmp_path)
    revision = _digest("root-decision")
    _bind_root_decision(harness, revision)
    fields = {
        "approval_revision": revision, "profile_id": "profile-1",
        "activation_id": "activation-1",
    }
    fields[{"revision": "approval_revision", "profile": "profile_id", "activation": "activation_id"}[mismatch]] = (
        _digest("other-decision") if mismatch == "revision" else "other"
    )
    _revocation, grants = harness.store.revoke_pack_approval(
        pack_id="other.pack", artifact_digest=_digest("other-artifact"),
        reason="unrelated root", **fields,
    )
    assert not grants
    assert not harness.store.is_revoked("grant", harness.grant.grant_id)
