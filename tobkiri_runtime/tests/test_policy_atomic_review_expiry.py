"""Actual committed generate proof must stay fresh inside grant settlement CAS."""

import pytest
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.host_contract import bind_host_contract
from tests.test_interactive_approval_v4 import _CONTRACT
from tests.test_policy_exact_grant_kernel import fixture_operation
from tests.fixtures.host_action_reviewer_v4 import build_shared_store_reviewer


@pytest.mark.parametrize("expired_part", ["transport", "evidence"])
def test_real_reviewer_expiry_between_validation_and_transaction_denies(
    tmp_path, monkeypatch, expired_part
):
    """Clock crosses expiry after real review, before the actual SQL commit."""
    bundles, facts = [], {}

    def configure(harness, *_):
        bundle = build_shared_store_reviewer(harness)
        bundles.append(bundle)
        facts.update(bundle.facts)
        return (), (), ()

    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, selection_id, operation = fixture_operation(
            tmp_path,
            monkeypatch,
            "agent",
            configure=configure,
            reviewer_capture=facts,
        )
        bundle = bundles[0]
        root.validate_review_evidence = bundle.reviewer.validate_review_evidence
        actual_review = bundle.reviewer.review_exact

        def review(retained):
            evidence = actual_review(retained)
            return evidence

        root.review_exact = review
        store = fixture.harness.store
        original = store.commit_policy_derived_grant

        def expire_before_sql(**kwargs):
            entry = kwargs["payload"]["policy_derived_grants"][kwargs["grant"].grant_id]
            lease, state = store.get_lease(entry["review_transport_lease_id"])
            assert state.value == "committed"
            stop = (
                lease.expires_at
                if expired_part == "transport"
                else entry["review_evidence_expires_at"]
            )
            store._clock = lambda: stop
            return original(**kwargs)

        monkeypatch.setattr(store, "commit_policy_derived_grant", expire_before_sql)
        with pytest.raises(AuthorityDenied, match="review transport or evidence expired"):
            kernel.derive_exact_operation_grant(selection_id, operation)
        payload = store.get_host_pending_effect(selection_id)[1]
        assert not payload.get("policy_derived_grants")
        assert not [
            event
            for event in store.audit_events()
            if event["event_type"] == "policy_derived_authority"
        ]
        bundle.broker.close()
