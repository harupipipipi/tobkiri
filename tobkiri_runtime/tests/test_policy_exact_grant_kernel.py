"""Actual-store policy derivation; native signatures remain fixture-only."""

from dataclasses import replace
import pytest
from core_runtime.authority.v4 import AuthorityDenied, GrantLifetime, LeaseState, authority_digest
from core_runtime.host_contract import bind_host_contract
from tests.test_interactive_approval_v4 import _CONTRACT
from tests.approval_policy_fixture import build_bounded_selection
from core_runtime.authority.policy_exact_grant import (
    CapturedReviewEvidence,
    PolicyExactGrantKernel,
    RetainedExactOperation,
)
from tobkiri_host.broker import PreparedInvocationSnapshot


class FixtureRoot:
    def __init__(self, fixture):
        self.fixture, self.command, self.operations = fixture, fixture.command, {}
        self.review_outcome, self.review_digest = "safe", None

    def resolve_current(self):
        return self.fixture.receipt.resolve("selection-1", self.command, **self.fixture.kwargs)

    def capture(self):
        return self.fixture.policy._validated_capture(self.command)

    def receipt_digest(self):
        return authority_digest(self.fixture.policy.store.read("selection-1")[1])

    def receipt_record(self):
        return self.fixture.policy.store.read("selection-1")

    def validate_prepared(self, operation):
        context = replace(self.fixture.context, request_id=operation.context.request_id)
        prepared = self.fixture.broker._prepared_from_snapshot(
            PreparedInvocationSnapshot.from_dict(operation.prepared_snapshot), context
        )
        if (
            prepared.binding.principal_ref.value != operation.context.target.principal_id
            or prepared.binding.operation.contract_id != operation.contract
            or prepared.binding.operation.operation_id != operation.operation
            or prepared.binding.operation.effect_class.value != operation.operation_class
        ):
            raise AuthorityDenied("retained operation signed route changed")

    def review_exact(self, operation):
        return CapturedReviewEvidence(
            "review-1",
            self.review_digest or operation.digest,
            authority_digest(self.capture()["reviewer"]),
            self.command.expires_at,
            self.review_outcome,
        )

    def validate_review_evidence(self, evidence, operation):
        # This isolated kernel fixture tests exact review consumption only.
        # Real generate transport is independently authenticated by reviewer/gate tests.
        if evidence.operation_capture_digest != operation.digest:
            raise AuthorityDenied("fixture review operation changed")

    def retain_operation(self, grant_id, operation):
        self.operations[grant_id] = operation

    def retained_operation(self, grant_id):
        if grant_id not in self.operations:
            raise AuthorityDenied("retained snapshot unavailable")
        return self.operations[grant_id]

    def has_operation(self, grant_id):
        return grant_id in self.operations


def fixture_operation(tmp_path, monkeypatch, mode, *, configure=None, reviewer_capture=None):
    fixture = build_bounded_selection(
        tmp_path, monkeypatch, mode, configure=configure, reviewer_capture=reviewer_capture
    )
    kernel = fixture.harness.kernel
    kernel.__class__ = PolicyExactGrantKernel
    root = FixtureRoot(fixture)
    identifier = fixture.policy.store.physical_id(
        "policy-selection-" + fixture.command.context.request_id
    )
    kernel.policy_roots = {identifier: root}
    prepared = fixture.broker.prepare(
        replace(fixture.frame, payload={"selection_id": "future-selection"}),
        replace(fixture.context, request_id="future-operation"),
    )
    scope = replace(fixture.ceiling, exact_request_digest=prepared.request_digest)
    context = fixture.harness.context(
        request_id="future-operation",
        request_digest=prepared.request_digest,
        effect_digest=scope.digest,
        activation_digest=fixture.context.activation_digest,
    )
    operation = RetainedExactOperation(
        prepared.to_snapshot().to_dict(),
        context,
        scope,
        prepared.binding.operation.contract_id,
        prepared.binding.operation.operation_id,
        prepared.binding.operation.effect_class.value,
        str(tmp_path.resolve()),
        lambda: None,
    )
    return fixture, kernel, root, identifier, operation


@pytest.mark.parametrize(
    "mode,change",
    [
        ("full", None),
        ("agent", None),
        *[
            ("full", c)
            for c in (
                "target",
                "caller",
                "scope",
                "snapshot",
                "class",
                "workspace",
                "ancestry",
                "cancel",
                "revoke",
                "stale_guard",
            )
        ],
        *[("agent", c) for c in ("danger", "digest", "replayed_review")],
    ],
)
def test_actual_policy_derived_exact_grant(tmp_path, monkeypatch, mode, change):
    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, identifier, operation = fixture_operation(
            tmp_path, monkeypatch, mode
        )
        if change in {"target", "caller", "ancestry"}:
            updates = {
                "target": {
                    "target": replace(operation.context.target, function_id="foreign.function")
                },
                "caller": {"caller_session_id": "foreign-session"},
                "ancestry": {"call_chain": ("foreign-ancestor",)},
            }
            operation = replace(operation, context=replace(operation.context, **updates[change]))
        elif change == "scope":
            operation = replace(
                operation, scope=replace(operation.scope, exact_request_digest=None)
            )
        elif change == "snapshot":
            operation = replace(
                operation, prepared_snapshot={"request_digest": authority_digest({"wrong": 1})}
            )
        elif change == "class":
            operation = replace(operation, operation_class="read_only")
        elif change == "workspace":
            operation = replace(operation, workspace_root=str(tmp_path.parent.resolve()))
        elif change == "danger":
            root.review_outcome = "danger"
        elif change == "digest":
            root.review_digest = authority_digest({"wrong": 1})
        if change in {
            "target",
            "caller",
            "scope",
            "snapshot",
            "class",
            "workspace",
            "ancestry",
            "danger",
            "digest",
        }:
            with pytest.raises((AuthorityDenied, ValueError)):
                kernel.derive_exact_operation_grant(identifier, operation)
            return
        grant = kernel.derive_exact_operation_grant(identifier, operation)
        assert grant.approval_id is None and grant.lifetime is GrantLifetime.ONE_SHOT
        assert (
            fixture.harness.store.get_interactive_approval_decision(operation.context.request_id)
            is None
        )
        if change == "replayed_review":
            prepared = fixture.broker.prepare(
                replace(fixture.frame, payload={"selection_id": "another"}),
                replace(fixture.context, request_id="another-operation"),
            )
            scope = replace(operation.scope, exact_request_digest=prepared.request_digest)
            operation = replace(
                operation,
                prepared_snapshot=prepared.to_snapshot().to_dict(),
                scope=scope,
                context=replace(
                    operation.context,
                    request_id="another-operation",
                    request_digest=prepared.request_digest,
                    effect_digest=scope.digest,
                ),
            )
            with pytest.raises(AuthorityDenied):
                kernel.derive_exact_operation_grant(identifier, operation)
            return
        original_operation = operation
        operation = root.retained_operation(grant.grant_id)
        result = kernel.authorize(operation.context, operation.scope)
        if change == "cancel":
            fixture.receipt.cancel("selection-1")
        elif change == "revoke":
            decision = fixture.harness.store.get_interactive_approval_decision(
                fixture.command.context.request_id
            )
            kernel.revoke(target_kind="grant", target_id=decision.grant_id, reason="fixture revoke")
        elif change == "stale_guard":

            def stale():
                raise AuthorityDenied("retained jail stale")

            root.operations[grant.grant_id] = replace(operation, assert_current=stale)
        kwargs = dict(
            target_domain_id=operation.context.target_domain_id,
            target_boot_epoch=operation.context.target_boot_epoch,
            request_digest=operation.context.request_digest,
        )
        if change:
            with pytest.raises(AuthorityDenied):
                kernel.dispatch(result.lease_token, **kwargs)
            return
        lease = kernel.dispatch(result.lease_token, **kwargs)
        kernel.finish(
            lease.lease_id,
            state=LeaseState.COMMITTED,
            outcome_digest=authority_digest({"finished": 1}),
        )
        assert fixture.harness.store.grant_usage(grant.grant_id) == (0, 1)
        with pytest.raises(AuthorityDenied):
            kernel.derive_exact_operation_grant(identifier, original_operation)
        with pytest.raises(AuthorityDenied):
            kernel.authorize(operation.context, operation.scope)


@pytest.mark.parametrize(
    "point,change",
    [("mint", "cancel"), ("mint", "revoke"), ("issue", "cancel"), ("dispatch", "cancel")],
)
def test_transactional_policy_root_race_denies_without_authority(
    tmp_path, monkeypatch, point, change
):
    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, identifier, operation = fixture_operation(
            tmp_path, monkeypatch, "full"
        )
        store = fixture.harness.store
        name = {
            "mint": "commit_policy_derived_grant",
            "issue": "issue_lease_with_audit",
            "dispatch": "dispatch_lease",
        }[point]
        original = getattr(store, name)

        def race(*args, **kwargs):
            if change == "cancel":
                fixture.receipt.cancel("selection-1")
            else:
                decision = store.get_interactive_approval_decision(
                    fixture.command.context.request_id
                )
                kernel.revoke(
                    target_kind="grant", target_id=decision.grant_id, reason="fixture race"
                )
            return original(*args, **kwargs)

        if point == "mint":
            monkeypatch.setattr(store, name, race)
            with pytest.raises(AuthorityDenied):
                kernel.derive_exact_operation_grant(identifier, operation)
            assert not store.get_host_pending_effect(identifier)[1].get("policy_derived_grants")
            return
        grant = kernel.derive_exact_operation_grant(identifier, operation)
        operation = root.retained_operation(grant.grant_id)
        if point == "issue":
            monkeypatch.setattr(store, name, race)
            with pytest.raises(AuthorityDenied):
                kernel.authorize(operation.context, operation.scope)
            assert store.grant_usage(grant.grant_id) == (0, 0)
            return
        result = kernel.authorize(operation.context, operation.scope)
        monkeypatch.setattr(store, name, race)
        with pytest.raises(AuthorityDenied):
            kernel.dispatch(
                result.lease_token,
                target_domain_id=operation.context.target_domain_id,
                target_boot_epoch=operation.context.target_boot_epoch,
                request_digest=operation.context.request_digest,
            )
        assert store.get_lease(result.lease_id)[1] is LeaseState.ISSUED


def test_exact_kind_uses_derivation_without_deleting_existing_broad_grant(tmp_path, monkeypatch):
    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, identifier, operation = fixture_operation(
            tmp_path, monkeypatch, "full"
        )
        broad = replace(fixture.harness.grant, grant_id="existing-broad-profile-grant")
        fixture.harness.store.put_records_atomically((broad,))
        grant = kernel.derive_exact_operation_grant(identifier, operation)
        operation = root.retained_operation(grant.grant_id)
        kernel.authorize(operation.context, operation.scope)
        assert fixture.harness.store.get_grant(broad.grant_id) == broad
        assert not fixture.harness.store.is_revoked("grant", broad.grant_id)


def test_durable_retained_operation_reloads_with_new_host_guard(tmp_path, monkeypatch):
    from tobkiri_host.policy_exact_authority_adapter import CommittedPolicyDerivationRoot

    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, identifier, operation = fixture_operation(
            tmp_path, monkeypatch, "full"
        )
        grant = kernel.derive_exact_operation_grant(identifier, operation)
        checks = []
        reopened = CommittedPolicyDerivationRoot(
            policy=fixture.policy,
            command=fixture.command,
            expected=fixture.kwargs,
            prepared_validator=root.validate_prepared,
            current_guard=lambda op: checks.append(op.digest),
            saved_root_guard=lambda *args: None,
        )
        kernel.policy_roots[identifier] = reopened
        retained = reopened.retained_operation(grant.grant_id)
        assert retained.digest == root.retained_operation(grant.grant_id).digest
        kernel.authorize(retained.context, retained.scope)
        assert checks


@pytest.mark.parametrize("state", ["approved", "claimed", "cancelled", "expired"])
def test_actual_store_derived_effect_requires_claim_winner(tmp_path, monkeypatch, state):
    """A canceled or losing pending effect cannot execute its minted grant."""
    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, identifier, operation = fixture_operation(
            tmp_path, monkeypatch, "full"
        )
        store = fixture.harness.store
        effect_id = "pending-effect-policy-race"
        scope = replace(
            operation.scope,
            dimensions={**operation.scope.dimensions, "pending_effect_id": (effect_id,)},
        )
        operation = replace(
            operation, scope=scope, context=replace(operation.context, effect_digest=scope.digest)
        )
        payload = {
            "effect_id": effect_id,
            "expires_at": fixture.command.expires_at,
            "state": "approved",
            "authorization_kind": "native_selected_policy_exact_v1",
            "policy_plan": {"selection_id": identifier},
            "prepared": {"request_digest": operation.context.request_digest},
        }
        revision = store.create_host_pending_effect(effect_id, payload)
        grant = kernel.derive_exact_operation_grant(identifier, operation)
        payload = {
            **payload,
            "state": "claimed" if state == "expired" else state,
            "expires_at": kernel._clock() - 1 if state == "expired" else fixture.command.expires_at,
            "policy_derived_grant_id": grant.grant_id
            if state in {"claimed", "expired"}
            else "losing-grant",
        }
        store.compare_and_swap_host_pending_effect(
            effect_id, expected_revision=revision, payload=payload
        )
        retained = root.retained_operation(grant.grant_id)
        if state == "claimed":
            assert kernel.authorize(retained.context, retained.scope).lease_token
        else:
            with pytest.raises(AuthorityDenied):
                kernel.authorize(retained.context, retained.scope)


@pytest.mark.parametrize("state", ["prepared", "claimed", "cancelled", "expired"])
def test_initial_effect_settlement_rejects_invalid_pending_state(tmp_path, monkeypatch, state):
    """Only an unexpired approved effect can begin the atomic claim settlement."""
    with bind_host_contract(_CONTRACT):
        fixture, kernel, root, identifier, operation = fixture_operation(
            tmp_path, monkeypatch, "full"
        )
        effect_id = "pending-effect-invalid-initial-state"
        scope = replace(
            operation.scope,
            dimensions={**operation.scope.dimensions, "pending_effect_id": (effect_id,)},
        )
        operation = replace(
            operation, scope=scope, context=replace(operation.context, effect_digest=scope.digest)
        )
        fixture.harness.store.create_host_pending_effect(
            effect_id,
            {
                "effect_id": effect_id,
                "expires_at": kernel._clock() - 1
                if state == "expired"
                else fixture.command.expires_at,
                "state": "approved" if state == "expired" else state,
                "authorization_kind": "native_selected_policy_exact_v1",
                "policy_plan": {"selection_id": identifier},
                "prepared": {"request_digest": operation.context.request_digest},
            },
        )
        with pytest.raises(AuthorityDenied):
            kernel.derive_exact_operation_grant(identifier, operation)
        assert not fixture.harness.store.get_host_pending_effect(identifier)[1].get(
            "policy_derived_grants"
        )
