"""Deep ADR-014/015 authority lifecycle and adversarial tests."""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from core_runtime.authority.v4_kernel import (
    AuthorityBinding,
    AuthorityKernel,
    AuthorityKernelProtocol,
    mint_successor_grant,
    mint_successor_provider_authority,
)
from core_runtime.authority.v4_models import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityEquivalence,
    AuthorityMode,
    AuthorityScope,
    AuthorityValidationError,
    DomainBoundary,
    DomainState,
    ExecutionDomain,
    FunctionPrincipal,
    GrantLifetime,
    GrantRecord,
    HostExtensionTrustRecord,
    InvocationContext,
    LeaseState,
    ProviderAuthorityRecord,
    SecurityEpoch,
    SuccessorEvidence,
    UpdateTrustPolicy,
    authority_digest,
    intersect_scopes,
)
from core_runtime.authority.v4_store import (
    AuditUnavailable,
    AuthorityStore,
    AuthorityStoreError,
)


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _principal(seed: str, *, operation: str = "invoke") -> FunctionPrincipal:
    return FunctionPrincipal(
        parent_artifact_digest=_digest(seed),
        function_implementation_digest=_digest(seed.upper()),
        function_id=f"function.{seed}",
        contract_revision_digest=_digest(seed.swapcase()),
        operation_id=operation,
    )


def _scope(
    paths: tuple[str, ...] = ("/safe",),
    *,
    methods: tuple[str, ...] = ("GET",),
    max_bytes: int = 1024,
    request_digest: str | None = None,
    opaque: bool = False,
) -> AuthorityScope:
    return AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("e"),
        dimensions={"path": paths, "method": methods},
        quotas={"max_bytes": max_bytes},
        exact_request_digest=request_digest,
        opaque=opaque,
    )


def _domain(
    seed: str,
    principal: FunctionPrincipal,
    *,
    epoch: int = 1,
    boot_epoch: int = 1,
    fencing_token: int = 7,
    boundary: DomainBoundary = DomainBoundary.WASM_COMPONENT,
    state: DomainState = DomainState.ACTIVE,
) -> ExecutionDomain:
    return ExecutionDomain(
        domain_id=f"domain-{seed}",
        profile_id="profile-1",
        activation_id="activation-1",
        boot_epoch=boot_epoch,
        process_identity=f"process-{seed}",
        authenticated_channel_digest=_digest(seed),
        sandbox_profile_digest=_digest(seed.upper()),
        resource_namespace=f"resource-{seed}",
        principals=(principal,),
        boundary=boundary,
        security_epoch=epoch,
        state=state,
        fencing_token=fencing_token,
    )


class _Resolver:
    def __init__(self, scope: AuthorityScope) -> None:
        self.scope = scope

    def resolve_authority_binding(
        self,
        *,
        context: InvocationContext,
        caller: FunctionPrincipal,
        target: FunctionPrincipal,
    ) -> AuthorityBinding:
        del caller, target
        return AuthorityBinding(
            caller_effect_ceiling=self.scope,
            runtime_safety_ceiling=self.scope,
            profile_admin_ceiling=self.scope,
            profile_id="profile-1",
            activation_id="activation-1",
            activation_digest=_digest("activation"),
            plan_digest=_digest("plan"),
            profile_authority_digest=_digest("4"),
            fencing_token=7,
            security_epoch=1,
        )


class _MutableClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        grant_lifetime: GrantLifetime = GrantLifetime.PERSISTENT_PROFILE,
        max_uses: int | None = 8,
        delegation_allowed: bool = False,
        max_delegation_depth: int = 0,
        audit_fault=None,
        terminated: list[str] | None = None,
        scope: AuthorityScope | None = None,
        binding_scope: AuthorityScope | None = None,
    ) -> None:
        self.clock = _MutableClock()
        self.scope = scope or _scope()
        self.store = AuthorityStore(
            tmp_path / "authority.sqlite3",
            clock=self.clock,
            audit_fault=audit_fault,
        )
        self.caller = _principal("a")
        self.target = _principal("b")
        self.caller_domain = _domain("caller", self.caller)
        self.target_domain = _domain(
            "target",
            self.target,
            boundary=DomainBoundary.DEDICATED_PROCESS,
        )
        self.kernel = AuthorityKernel(
            self.store,
            _Resolver(binding_scope or self.scope),
            clock=self.clock,
            lease_ttl_seconds=10,
            terminate_domain=(terminated.append if terminated is not None else None),
        )
        self.kernel.register_execution_domain(
            self.caller_domain,
            session_id="session-caller",
            channel_digest=self.caller_domain.authenticated_channel_digest,
            principal=self.caller,
        )
        self.kernel.register_execution_domain(
            self.target_domain,
            session_id="session-target",
            channel_digest=self.target_domain.authenticated_channel_digest,
            principal=self.target,
        )
        self.approval = ApprovalRecord(
            approval_id="approval-1",
            snapshot_digest=_digest("1"),
            actor_id="user-1",
            decision="approved",
            decided_at=self.clock(),
            caller=self.caller,
            target=self.target,
            profile_id="profile-1",
            effect_bundle_digest=_digest("2"),
            security_epoch=1,
        )
        self.provider = ProviderAuthorityRecord(
            record_id="provider-authority-1",
            provider=self.target,
            execution_domain_id=self.target_domain.domain_id,
            execution_domain_identity_digest=self.target_domain.identity_digest,
            scope=self.scope,
            authority_mode=AuthorityMode.LEASE_ONLY,
            security_epoch=1,
            trust_provenance_digest=_digest("3"),
            publisher_lineage="publisher.target",
            host_extension_id="extension-http",
            valid_from=self.clock(),
            host_broker_binding="broker.http.v1",
        )
        self.extension_trust = HostExtensionTrustRecord(
            trust_id="extension-http",
            parent_artifact_digest=self.target.parent_artifact_digest,
            publisher_lineage="publisher.target",
            provider_principal_ids=(self.target.principal_id,),
            trust_provenance_digest=_digest("3"),
            security_epoch=1,
            valid_from=self.clock(),
        )
        self.grant = GrantRecord(
            grant_id="grant-1",
            caller=self.caller,
            target=self.target,
            profile_id="profile-1",
            activation_id="activation-1",
            profile_authority_digest=_digest("4"),
            caller_publisher_lineage="publisher.caller",
            target_publisher_lineage="publisher.target",
            scope=self.scope,
            lifetime=grant_lifetime,
            security_epoch=1,
            approval_id="approval-1",
            issued_at=self.clock(),
            max_uses=max_uses,
            delegation_allowed=delegation_allowed,
            max_delegation_depth=max_delegation_depth,
        )
        self.kernel.commit_approval_bundle(
            self.approval,
            host_extension_trust=self.extension_trust,
            provider_authorities=(self.provider,),
            grants=(self.grant,),
        )

    def context(self, **updates) -> InvocationContext:
        values = {
            "request_id": "request-1",
            "request_digest": _digest("5"),
            "effect_digest": _digest("6"),
            "caller_session_id": "session-caller",
            "target": self.target,
            "target_domain_id": self.target_domain.domain_id,
            "target_boot_epoch": self.target_domain.boot_epoch,
            "profile_id": "profile-1",
            "activation_id": "activation-1",
            "activation_digest": _digest("activation"),
            "plan_digest": _digest("plan"),
            "profile_authority_digest": _digest("4"),
            "fencing_token": self.target_domain.fencing_token,
            "security_epoch": 1,
        }
        values.update(updates)
        return InvocationContext(**values)


def test_exact_authority_flow_reserves_dispatches_and_commits_audit(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert isinstance(harness.kernel, AuthorityKernelProtocol)

    result = harness.kernel.authorize(harness.context(), harness.scope)
    token_payload = base64.urlsafe_b64decode(
        result.lease_token.split(".", 1)[0].encode("ascii")
    )
    assert b"grant" not in token_payload
    assert b"provider" not in token_payload
    lease = harness.kernel.dispatch(
        result.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )
    harness.kernel.finish(
        lease.lease_id,
        state=LeaseState.COMMITTED,
        outcome_digest=_digest("7"),
    )

    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 1)
    assert [event["event_state"] for event in harness.store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]
    with pytest.raises(AuthorityDenied, match="already used"):
        harness.kernel.dispatch(
            result.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )


def test_payload_cannot_substitute_caller_target_or_activation(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    impostor = _principal("c")

    with pytest.raises(AuthorityDenied):
        harness.kernel.authorize(harness.context(target=impostor), harness.scope)
    with pytest.raises(AuthorityDenied):
        harness.kernel.authorize(
            harness.context(activation_id="activation-attacker"), harness.scope
        )
    with pytest.raises(AuthorityDenied):
        harness.kernel.authorize(
            harness.context(caller_session_id="payload-declared-session"),
            harness.scope,
        )
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)


def test_equivalent_provider_rows_resolve_as_one_exact_authority(tmp_path: Path) -> None:
    """Caller-specific rows may share one exact Provider reachability path."""

    harness = _Harness(tmp_path)
    harness.store.put_record(
        replace(harness.provider, record_id="provider-authority-equivalent")
    )

    result = harness.kernel.authorize(harness.context(), harness.scope)
    stored = harness.store.get_lease(result.lease_id)

    assert stored is not None
    assert stored[0].provider_authority_id == "provider-authority-1"


def test_materially_different_provider_rows_remain_ambiguous(tmp_path: Path) -> None:
    """A broader overlapping Provider row must still fail closed."""

    harness = _Harness(tmp_path)
    harness.store.put_record(
        replace(
            harness.provider,
            record_id="provider-authority-broader",
            scope=_scope(paths=("/safe", "/other")),
        )
    )

    with pytest.raises(AuthorityDenied, match="missing or ambiguous"):
        harness.kernel.authorize(harness.context(), harness.scope)


def test_equivalent_provider_row_revocation_is_exact_and_fails_over(
    tmp_path: Path,
) -> None:
    """Exact row revocation fences its lease before another alias is selected."""

    harness = _Harness(tmp_path)
    alias_id = "provider-authority-equivalent"
    harness.store.put_record(replace(harness.provider, record_id=alias_id))
    first = harness.kernel.authorize(harness.context(), harness.scope)
    first_stored = harness.store.get_lease(first.lease_id)
    assert first_stored is not None
    assert first_stored[0].provider_authority_id == harness.provider.record_id

    harness.kernel.revoke(
        target_kind="provider_authority",
        target_id=harness.provider.record_id,
        reason="revoke selected exact row",
    )
    with pytest.raises(AuthorityDenied, match="already used or revoked"):
        harness.kernel.dispatch(
            first.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )

    second_context = harness.context(
        request_id="request-2",
        request_digest=_digest("8"),
        effect_digest=_digest("9"),
    )
    second = harness.kernel.authorize(second_context, harness.scope)
    second_stored = harness.store.get_lease(second.lease_id)
    assert second_stored is not None
    assert second_stored[0].provider_authority_id == alias_id

    harness.kernel.revoke(
        target_kind="provider_authority",
        target_id=alias_id,
        reason="revoke remaining exact row",
    )
    with pytest.raises(AuthorityDenied, match="missing or ambiguous"):
        harness.kernel.authorize(
            harness.context(
                request_id="request-3",
                request_digest=_digest("10"),
                effect_digest=_digest("11"),
            ),
            harness.scope,
        )


def test_host_extension_trust_does_not_expand_to_another_operation(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    sibling = replace(harness.target, operation_id="admin")
    sibling_domain = _domain(
        "sibling",
        sibling,
        boundary=DomainBoundary.DEDICATED_PROCESS,
    )
    harness.kernel.register_execution_domain(
        sibling_domain,
        session_id="session-sibling",
        channel_digest=sibling_domain.authenticated_channel_digest,
        principal=sibling,
    )
    sibling_provider = replace(
        harness.provider,
        record_id="provider-authority-sibling",
        provider=sibling,
        execution_domain_id=sibling_domain.domain_id,
        execution_domain_identity_digest=sibling_domain.identity_digest,
    )
    sibling_approval = replace(
        harness.approval,
        approval_id="approval-sibling",
        target=sibling,
    )
    sibling_grant = replace(
        harness.grant,
        grant_id="grant-sibling",
        target=sibling,
        approval_id=sibling_approval.approval_id,
    )

    with pytest.raises(AuthorityDenied, match="trust"):
        harness.kernel.commit_approval_bundle(
            sibling_approval,
            host_extension_trust=harness.extension_trust,
            provider_authorities=(sibling_provider,),
            grants=(sibling_grant,),
        )


def test_effective_intersection_denies_scope_escalation(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    path_escalation = _scope(paths=("/safe", "/admin"))
    quota_escalation = _scope(max_bytes=2048)
    semantics_substitution = replace(harness.scope, semantics_digest=_digest("9"))

    for malicious_scope in (
        path_escalation,
        quota_escalation,
        semantics_substitution,
    ):
        with pytest.raises(AuthorityDenied):
            harness.kernel.authorize(harness.context(), malicious_scope)
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)


def test_scope_intersection_requires_known_semantics_and_nonempty_sets() -> None:
    first = _scope(paths=("/a", "/b"))
    second = _scope(paths=("/b", "/c"), max_bytes=100)
    effective = intersect_scopes(first, second)
    assert effective.dimensions["path"] == ("/b",)
    assert effective.quotas["max_bytes"] == 100

    with pytest.raises(AuthorityValidationError, match="semantics"):
        intersect_scopes(first, replace(second, semantics_digest=_digest("9")))
    with pytest.raises(AuthorityValidationError, match="empty"):
        intersect_scopes(_scope(paths=("/a",)), _scope(paths=("/b",)))
    with pytest.raises(AuthorityValidationError, match="integers"):
        AuthorityScope.from_dict(
            {
                **first.to_dict(),
                "quotas": {"max_bytes": True},
            }
        )
    with pytest.raises(AuthorityValidationError, match="unknown"):
        AuthorityScope.from_dict({**first.to_dict(), "allow_all": True})


def test_non_finite_lease_timing_is_rejected(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "authority.sqlite3")
    with pytest.raises(ValueError, match="TTL"):
        AuthorityKernel(store, _Resolver(_scope()), lease_ttl_seconds=float("nan"))
    with pytest.raises(AuthorityValidationError, match="positive"):
        SecurityEpoch(value=0, advanced_at=1, reason_digest=_digest("1"))


def test_authority_store_close_is_idempotent_and_restartable(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    store = AuthorityStore(database)
    assert store.security_epoch == 1

    store.close()
    store.close()
    with pytest.raises(AuthorityStoreError, match="closed"):
        _ = store.security_epoch

    renamed = tmp_path / "authority-renamed.sqlite3"
    database.rename(renamed)
    renamed.rename(database)
    with AuthorityStore(database) as restarted:
        assert restarted.security_epoch == 1
    with pytest.raises(AuthorityStoreError, match="closed"):
        _ = restarted.security_epoch


def test_authority_store_releases_database_for_deletion(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    store = AuthorityStore(database)
    key_path = store.key_path

    store.close()
    database.unlink()
    key_path.unlink()

    assert not database.exists()
    assert not key_path.exists()


def test_authoritative_audit_failure_rolls_back_grant_and_lease(tmp_path: Path) -> None:
    fault_enabled = False

    def audit_fault() -> None:
        if fault_enabled:
            raise OSError("disk full")

    harness = _Harness(tmp_path, audit_fault=audit_fault)
    baseline_events = len(harness.store.audit_events())
    fault_enabled = True

    with pytest.raises(AuditUnavailable):
        harness.kernel.authorize(harness.context(), harness.scope)

    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)
    fault_enabled = False
    assert len(harness.store.audit_events()) == baseline_events


def test_one_shot_parallel_use_has_one_winner(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
    )

    def issue(index: int) -> bool:
        try:
            harness.kernel.authorize(
                harness.context(
                    request_id=f"request-{index}",
                    request_digest=_digest(str((index % 9) + 1)),
                ),
                harness.scope,
            )
            return True
        except AuthorityDenied:
            return False

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(issue, range(12)))

    assert outcomes.count(True) == 1
    assert harness.store.grant_usage(harness.grant.grant_id) == (1, 0)


def test_unused_lease_expiry_releases_one_shot_reservation(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
    )
    result = harness.kernel.authorize(harness.context(), harness.scope)
    harness.clock.value += 11

    with pytest.raises(AuthorityDenied, match="already used or revoked"):
        harness.kernel.dispatch(
            result.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )

    stored = harness.store.get_lease(result.lease_id)
    assert stored is not None and stored[1] is LeaseState.EXPIRED
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)


def test_stale_epoch_and_boot_epoch_are_denied(tmp_path: Path) -> None:
    terminated: list[str] = []
    harness = _Harness(tmp_path, terminated=terminated)

    with pytest.raises(AuthorityDenied, match="stale"):
        harness.kernel.authorize(harness.context(target_boot_epoch=2), harness.scope)

    new_epoch = harness.kernel.advance_security_epoch("emergency response")
    assert new_epoch == 2
    assert harness.store.security_epoch_record.value == 2
    assert harness.store.security_epoch_record.reason_digest == authority_digest(
        {"reason": "emergency response"}
    )
    assert set(terminated) == {
        harness.caller_domain.domain_id,
        harness.target_domain.domain_id,
    }
    with pytest.raises(AuthorityDenied, match="stale"):
        harness.kernel.authorize(harness.context(), harness.scope)


def test_domain_lifecycle_is_forward_only_and_fences_sessions(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draining = harness.store.transition_domain(
        harness.target_domain.domain_id,
        expected_boot_epoch=1,
        expected_state=DomainState.ACTIVE,
        new_state=DomainState.DRAINING,
    )
    assert draining.state is DomainState.DRAINING
    with pytest.raises(AuthorityDenied, match="invalid"):
        harness.store.transition_domain(
            harness.target_domain.domain_id,
            expected_boot_epoch=1,
            expected_state=DomainState.DRAINING,
            new_state=DomainState.ACTIVE,
        )
    fenced = harness.store.transition_domain(
        harness.target_domain.domain_id,
        expected_boot_epoch=1,
        expected_state=DomainState.DRAINING,
        new_state=DomainState.FENCED,
    )
    assert fenced.state is DomainState.FENCED
    with pytest.raises(AuthorityDenied, match="stale"):
        harness.kernel.authorize(harness.context(), harness.scope)


def test_revocation_between_issue_and_dispatch_fences_effect(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)

    harness.kernel.revoke(
        target_kind="execution_domain",
        target_id=harness.target_domain.domain_id,
        reason="provider compromised",
    )

    with pytest.raises(AuthorityDenied, match="already used or revoked"):
        harness.kernel.dispatch(
            result.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )


@pytest.mark.parametrize(
    ("target_kind", "target_attribute"),
    [
        ("publisher", "target_publisher_lineage"),
        ("host_extension", "host_extension_id"),
        ("provider_authority", "provider_authority_id"),
        ("activation", "activation_id"),
    ],
)
def test_revocation_indices_fence_already_issued_leases(
    tmp_path: Path,
    target_kind: str,
    target_attribute: str,
) -> None:
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    stored = harness.store.get_lease(result.lease_id)
    assert stored is not None
    target_id = str(getattr(stored[0], target_attribute))

    harness.kernel.revoke(
        target_kind=target_kind,
        target_id=target_id,
        reason="adversarial revocation",
    )

    with pytest.raises(AuthorityDenied, match="already used or revoked"):
        harness.kernel.dispatch(
            result.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )


@pytest.mark.parametrize("target_kind", ["credential", "resource_root", "workflow"])
def test_unenforceable_revocation_kinds_are_rejected_without_success_audit(
    tmp_path: Path,
    target_kind: str,
) -> None:
    harness = _Harness(tmp_path)
    baseline = harness.store.audit_events()

    with pytest.raises(ValueError, match="unsupported revocation target"):
        harness.kernel.revoke(
            target_kind=target_kind,
            target_id=f"{target_kind}:review-a",
            reason="must not report an unenforceable revocation",
        )

    assert harness.store.is_revoked(target_kind, f"{target_kind}:review-a") is False
    assert harness.store.audit_events() == baseline


def test_host_extension_revocation_terminates_authority_bearing_domain(
    tmp_path: Path,
) -> None:
    terminated: list[str] = []
    harness = _Harness(tmp_path, terminated=terminated)

    harness.kernel.revoke(
        target_kind="host_extension",
        target_id=harness.provider.host_extension_id,
        reason="extension compromised",
    )

    assert terminated == [harness.target_domain.domain_id]


def test_revocation_audit_failure_triggers_global_in_memory_fence(
    tmp_path: Path,
) -> None:
    fault_enabled = False
    terminated: list[str] = []

    def audit_fault() -> None:
        if fault_enabled:
            raise OSError("audit disk full")

    harness = _Harness(
        tmp_path,
        audit_fault=audit_fault,
        terminated=terminated,
    )
    fault_enabled = True

    with pytest.raises(AuditUnavailable):
        harness.kernel.revoke(
            target_kind="host_extension",
            target_id=harness.provider.host_extension_id,
            reason="emergency",
        )

    assert set(terminated) == {
        harness.caller_domain.domain_id,
        harness.target_domain.domain_id,
    }
    with pytest.raises(AuthorityDenied, match="emergency-fenced"):
        harness.kernel.authorize(harness.context(), harness.scope)


def test_artifact_revocation_after_dispatch_prevents_effect_commit(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    lease = harness.kernel.dispatch(
        result.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )

    harness.kernel.revoke(
        target_kind="pack_artifact",
        target_id=harness.target.parent_artifact_digest,
        reason="artifact compromised",
    )

    with pytest.raises(AuthorityDenied, match="not dispatched"):
        harness.kernel.finish(
            lease.lease_id,
            state=LeaseState.COMMITTED,
            outcome_digest=_digest("7"),
        )


def test_concurrent_revoke_dispatch_never_commits_after_revocation(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)

    def dispatch() -> str:
        try:
            lease = harness.kernel.dispatch(
                result.lease_token,
                target_domain_id=harness.target_domain.domain_id,
                target_boot_epoch=harness.target_domain.boot_epoch,
                request_digest=_digest("5"),
            )
            return lease.lease_id
        except AuthorityDenied:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        dispatch_future = executor.submit(dispatch)
        revoke_future = executor.submit(
            harness.kernel.revoke,
            target_kind="execution_domain",
            target_id=harness.target_domain.domain_id,
            reason="race",
        )
        dispatch_result = dispatch_future.result()
        revoke_future.result()

    if dispatch_result != "denied":
        with pytest.raises(AuthorityDenied):
            harness.kernel.finish(
                dispatch_result,
                state=LeaseState.COMMITTED,
                outcome_digest=_digest("7"),
            )


def test_delegation_defaults_off_and_requires_attenuated_parent(tmp_path: Path) -> None:
    no_delegation = _Harness(tmp_path / "off")
    with pytest.raises(AuthorityDenied, match="delegation"):
        no_delegation.kernel.authorize(
            no_delegation.context(call_chain=(_principal("c").principal_id,)),
            no_delegation.scope,
        )

    delegated = _Harness(
        tmp_path / "on",
        delegation_allowed=True,
        max_delegation_depth=1,
    )
    result = delegated.kernel.authorize(
        delegated.context(call_chain=(_principal("c").principal_id,)),
        delegated.scope,
    )
    assert result.lease_id.startswith("lease-")

    too_deep = (_principal("c").principal_id, _principal("d").principal_id)
    with pytest.raises(AuthorityDenied, match="delegation"):
        delegated.kernel.authorize(
            delegated.context(request_id="request-2", call_chain=too_deep),
            delegated.scope,
        )


def test_approval_record_alone_is_never_runtime_authority(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    second_caller = _principal("c")
    second_domain = _domain("second", second_caller)
    harness.kernel.register_execution_domain(
        second_domain,
        session_id="session-second",
        channel_digest=second_domain.authenticated_channel_digest,
        principal=second_caller,
    )
    approval = replace(
        harness.approval,
        approval_id="approval-only",
        caller=second_caller,
    )
    harness.store.put_record(approval)

    with pytest.raises(AuthorityDenied, match="Grant"):
        harness.kernel.authorize(
            harness.context(caller_session_id="session-second"), harness.scope
        )


def test_co_location_rejects_implicit_same_pack_authority_sharing() -> None:
    first = _principal("a")
    second = _principal("b")
    with pytest.raises(AuthorityValidationError, match="equivalence"):
        ExecutionDomain(
            domain_id="domain-shared",
            profile_id="profile-1",
            activation_id="activation-1",
            boot_epoch=1,
            process_identity="native-dispatcher",
            authenticated_channel_digest=_digest("1"),
            sandbox_profile_digest=_digest("2"),
            resource_namespace="resource-shared",
            principals=(first, second),
            boundary=DomainBoundary.DEDICATED_PROCESS,
            security_epoch=1,
        )

    equivalence = AuthorityEquivalence(
        provider_ceiling_digest=_scope().digest,
        scope_class="http-read",
        trust_class="host-extension",
    )
    mutual = (first.principal_id, second.principal_id)
    domain = ExecutionDomain(
        domain_id="domain-equivalent",
        profile_id="profile-1",
        activation_id="activation-1",
        boot_epoch=1,
        process_identity="verified-equivalence-worker",
        authenticated_channel_digest=_digest("1"),
        sandbox_profile_digest=_digest("2"),
        resource_namespace="resource-equivalent",
        principals=(first, second),
        boundary=DomainBoundary.AUTHORITY_EQUIVALENCE,
        security_epoch=1,
        equivalence=equivalence,
        mutual_colocation_principals=mutual,
    )
    assert domain.principal_ids == frozenset(mutual)


def test_os_entitlement_requires_dedicated_exact_process(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    bad_provider = replace(
        harness.provider,
        record_id="provider-authority-os",
        authority_mode=AuthorityMode.OS_ENTITLEMENT,
        os_entitlements=("camera",),
        host_broker_binding=None,
    )
    harness.kernel._validate_provider_domain(bad_provider)

    wasm_target = replace(
        harness.target_domain,
        domain_id="domain-wasm-target",
        boundary=DomainBoundary.WASM_COMPONENT,
    )
    harness.store.put_record(wasm_target)
    invalid = replace(
        bad_provider,
        execution_domain_id=wasm_target.domain_id,
        execution_domain_identity_digest=wasm_target.identity_digest,
    )
    with pytest.raises(AuthorityDenied, match="dedicated process"):
        harness.kernel._validate_provider_domain(invalid)


def test_opaque_semantics_are_exact_request_one_shot_only() -> None:
    request_digest = _digest("5")
    opaque_scope = _scope(
        request_digest=request_digest,
        opaque=True,
    )
    with pytest.raises(AuthorityValidationError, match="one-shot"):
        GrantRecord(
            grant_id="grant-opaque",
            caller=_principal("a"),
            target=_principal("b"),
            profile_id="profile-1",
            activation_id="activation-1",
            profile_authority_digest=_digest("4"),
            caller_publisher_lineage="publisher.caller",
            target_publisher_lineage="publisher.target",
            scope=opaque_scope,
            lifetime=GrantLifetime.PERSISTENT_PROFILE,
            security_epoch=1,
            approval_id="approval-1",
            issued_at=1,
            max_uses=1,
        )


def test_provider_only_bundle_does_not_authorize_a_direct_effect(
    tmp_path: Path,
) -> None:
    """Provider reachability alone cannot replace an interactive one-shot Grant."""

    harness = _Harness(tmp_path)
    interactive_target = _principal("interactive")
    interactive_domain = _domain(
        "interactive-target",
        interactive_target,
        boundary=DomainBoundary.DEDICATED_PROCESS,
    )
    harness.kernel.register_execution_domain(
        interactive_domain,
        session_id="session-interactive-target",
        channel_digest=interactive_domain.authenticated_channel_digest,
        principal=interactive_target,
    )
    trust = HostExtensionTrustRecord(
        trust_id="extension-interactive",
        parent_artifact_digest=interactive_target.parent_artifact_digest,
        publisher_lineage="publisher.interactive",
        provider_principal_ids=(interactive_target.principal_id,),
        trust_provenance_digest=_digest("interactive-trust"),
        security_epoch=1,
        valid_from=harness.clock(),
    )
    provider = ProviderAuthorityRecord(
        record_id="provider-authority-interactive",
        provider=interactive_target,
        execution_domain_id=interactive_domain.domain_id,
        execution_domain_identity_digest=interactive_domain.identity_digest,
        scope=harness.scope,
        authority_mode=AuthorityMode.LEASE_ONLY,
        security_epoch=1,
        trust_provenance_digest=trust.trust_provenance_digest,
        publisher_lineage=trust.publisher_lineage,
        host_extension_id=trust.trust_id,
        valid_from=harness.clock(),
        host_broker_binding="broker.interactive.v1",
    )

    harness.kernel.commit_provider_authority_bundle(
        host_extension_trust=trust,
        provider_authorities=(provider,),
    )

    assert harness.store.get_provider_authority(provider.record_id) == provider
    assert harness.store.get_host_extension_trust(trust.trust_id) == trust
    assert all(
        item.target != interactive_target for item in harness.store.list_grants()
    )
    with pytest.raises(AuthorityDenied, match="caller Grant"):
        harness.kernel.authorize(
            harness.context(
                target=interactive_target,
                target_domain_id=interactive_domain.domain_id,
                target_boot_epoch=interactive_domain.boot_epoch,
            ),
            harness.scope,
        )


def test_nonopaque_exact_scope_still_requires_the_current_request_digest(
    tmp_path: Path,
) -> None:
    """Exact request binding is enforced even when scope semantics are visible."""

    scope = _scope(request_digest=_digest("approved-effect"), opaque=False)
    harness = _Harness(tmp_path, scope=scope)

    with pytest.raises(AuthorityDenied, match="exact scope"):
        harness.kernel.authorize(harness.context(), scope)


def test_crash_recovery_marks_dispatched_effect_ambiguous(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    lease = harness.kernel.dispatch(
        result.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )

    recovered = harness.kernel.recover()

    assert recovered == [lease.lease_id]
    stored = harness.store.get_lease(lease.lease_id)
    assert stored is not None and stored[1] is LeaseState.AMBIGUOUS
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 1)
    with pytest.raises(AuthorityDenied):
        harness.kernel.finish(
            lease.lease_id,
            state=LeaseState.COMMITTED,
            outcome_digest=_digest("7"),
        )


def test_authority_database_encrypts_records_and_detects_audit_tamper(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    database_bytes = harness.store.path.read_bytes()
    assert b"function.a" not in database_bytes
    assert b"/safe" not in database_bytes

    with sqlite3.connect(harness.store.path) as connection:
        connection.execute(
            "UPDATE authority_audit SET event_digest=? WHERE event_id=?",
            (_digest("9"), result.lease_id.replace("lease-", "audit-")),
        )
        # The reservation ID is random and not derivable from the Lease ID; tamper
        # with the latest row instead.
        connection.execute(
            "UPDATE authority_audit SET event_digest=? WHERE sequence=(SELECT MAX(sequence)"
            " FROM authority_audit)",
            (_digest("9"),),
        )
    with pytest.raises(AuthorityStoreError, match="chain"):
        harness.store.audit_events()


def test_existing_authority_database_never_regenerates_a_missing_key(
    tmp_path: Path,
) -> None:
    store = AuthorityStore(tmp_path / "authority.sqlite3")
    store.key_path.unlink()

    with pytest.raises(AuthorityStoreError, match="missing"):
        AuthorityStore(store.path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission invariant")
def test_authority_key_rejects_group_or_world_access(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "authority.sqlite3")
    store.key_path.chmod(0o644)

    with pytest.raises(AuthorityStoreError, match="permissions"):
        AuthorityStore(store.path)


def test_successor_authority_requires_every_non_expansion_proof(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    policy = UpdateTrustPolicy(
        policy_id="update-policy-1",
        publisher_lineage="publisher.lineage",
        allow_non_expanding_successor=True,
    )
    evidence = SuccessorEvidence(
        old_publisher_lineage="publisher.lineage",
        new_publisher_lineage="publisher.lineage",
        old_trust_class="host-extension",
        new_trust_class="host-extension",
        semantics_non_expanding=True,
        implementation_non_expanding=True,
        domain_non_expanding=True,
        entitlement_non_expanding=True,
        background_non_expanding=True,
        network_non_expanding=True,
        process_identity_non_expanding=True,
    )
    new_caller = replace(
        harness.caller,
        parent_artifact_digest=_digest("8"),
    )
    new_target = replace(
        harness.target,
        parent_artifact_digest=_digest("9"),
    )
    new_domain = replace(
        _domain(
            "successor",
            new_target,
            boundary=DomainBoundary.DEDICATED_PROCESS,
            epoch=2,
        ),
        activation_id="activation-2",
    )

    successor_grant = mint_successor_grant(
        harness.grant,
        new_caller=new_caller,
        new_target=new_target,
        new_activation_id="activation-2",
        new_profile_authority_digest=_digest("8"),
        security_epoch=2,
        policy=policy,
        evidence=evidence,
        issued_at=2000,
    )
    successor_provider = mint_successor_provider_authority(
        harness.provider,
        new_provider=new_target,
        new_domain=new_domain,
        new_host_extension_id="extension-http-successor",
        security_epoch=2,
        policy=policy,
        evidence=evidence,
        valid_from=2000,
    )
    assert successor_grant.caller.parent_artifact_digest == _digest("8")
    assert successor_provider.provider.parent_artifact_digest == _digest("9")
    assert successor_grant.grant_id != harness.grant.grant_id
    assert successor_provider.record_id != harness.provider.record_id

    harness.clock.value = 2000
    harness.kernel.advance_security_epoch("activate verified successor")
    harness.kernel.register_execution_domain(
        new_domain,
        session_id="session-successor-target",
        channel_digest=new_domain.authenticated_channel_digest,
        principal=new_target,
    )
    successor_trust = HostExtensionTrustRecord(
        trust_id="extension-http-successor",
        parent_artifact_digest=new_target.parent_artifact_digest,
        publisher_lineage="publisher.target",
        provider_principal_ids=(new_target.principal_id,),
        trust_provenance_digest=_digest("8"),
        security_epoch=2,
        valid_from=2000,
    )
    harness.kernel.commit_successor_authority(
        successor_provider,
        successor_grant,
        host_extension_trust=successor_trust,
    )
    assert (
        harness.store.get_provider_authority(successor_provider.record_id) is not None
    )
    assert harness.store.get_grant(successor_grant.grant_id) is not None

    expanded = replace(evidence, entitlement_non_expanding=False)
    with pytest.raises(AuthorityDenied, match="successor"):
        mint_successor_grant(
            harness.grant,
            new_caller=new_caller,
            new_target=new_target,
            new_activation_id="activation-2",
            new_profile_authority_digest=_digest("8"),
            security_epoch=2,
            policy=policy,
            evidence=expanded,
            issued_at=2000,
        )


def test_transactional_approval_failure_leaves_no_partial_authority(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    duplicate_provider = replace(
        harness.provider,
        record_id="provider-authority-new",
    )
    duplicate_grant = replace(
        harness.grant,
        grant_id="grant-new",
        approval_id="approval-new",
    )
    approval = replace(harness.approval, approval_id="approval-new")
    conflicting = replace(duplicate_grant, grant_id=harness.grant.grant_id)

    with pytest.raises(AuthorityStoreError):
        harness.kernel.commit_approval_bundle(
            approval,
            provider_authorities=(duplicate_provider,),
            grants=(duplicate_grant, conflicting),
        )

    assert harness.store.get_approval("approval-new") is None
    assert harness.store.get_provider_authority("provider-authority-new") is None
    assert harness.store.get_grant("grant-new") is None
