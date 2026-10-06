"""Actual root review method plus independent real generate Broker/Store fixture."""

from types import SimpleNamespace
from dataclasses import replace
import pytest
from tests.test_authority_v4_lifecycle import _Harness, _digest
from core_runtime.host_contract import bind_host_contract
from tests.test_interactive_approval_v4 import _CONTRACT
from core_runtime.authority.v4 import AuthorityScope, AuthorityDenied
from core_runtime.authority.policy_exact_grant import RetainedExactOperation, CapturedReviewEvidence
from tobkiri_host.policy_exact_authority_adapter import CommittedPolicyDerivationRoot
from tests.fixtures.host_action_reviewer_v4 import build_shared_store_reviewer


def test_actual_root_review_method_uses_separate_real_generate_lease(tmp_path):
    harness = _Harness(tmp_path)
    bundle = build_shared_store_reviewer(harness)
    scope = AuthorityScope(
        "operation.invoke",
        _digest("read-semantics"),
        dimensions={"contract": ("fixture.file",), "operation": ("file.read",)},
        exact_request_digest=_digest("request"),
    )
    context = harness.context(request_digest=_digest("request"), effect_digest=scope.digest)
    retained = RetainedExactOperation(
        {
            "contract_id": "fixture.file",
            "operation_id": "file.read",
            "normalized_payload": {"path": str(tmp_path / "hello.txt")},
            "request_digest": _digest("request"),
        },
        context,
        scope,
        "fixture.file",
        "file.read",
        "read",
        str(tmp_path),
        lambda: None,
    )
    with bind_host_contract(_CONTRACT):
        evidence = CommittedPolicyDerivationRoot.review_exact(
            SimpleNamespace(reviewer=bundle.reviewer), retained
        )
    assert isinstance(evidence, CapturedReviewEvidence)
    assert evidence.outcome == "safe"
    assert evidence.provenance["transport_request_digest"] != evidence.provenance["payload_digest"]
    stored = harness.store.get_lease(evidence.provenance["transport_proof"]["lease_id"])
    assert stored[1].value == "committed"
    assert stored[0].caller.principal_id != harness.caller.principal_id
    assert stored[0].target.principal_id != harness.target.principal_id
    assert stored[0].grant_id == "review-independent-grant"
    assert len(bundle.transport.transports) == 1
    bundle.reviewer.validate_review_evidence(evidence, retained)
    harness.store.revoke(
        target_kind="grant", target_id="review-independent-grant", reason="fixture revoke"
    )
    with pytest.raises(AuthorityDenied):
        bundle.reviewer.validate_review_evidence(evidence, retained)
    harness.store.close()
