"""Fail-closed ADR-014/015 authority kernel and Host integration protocol."""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from .v4_models import (
    ApprovalRecord,
    AuthorityDenied,
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
    InvocationLease,
    LeaseState,
    ProviderAuthorityRecord,
    SuccessorEvidence,
    UpdateTrustPolicy,
    intersect_scopes,
)
from .v4_store import AuditUnavailable, AuthorityStore, AuthorityStoreError


@dataclass(frozen=True)
class AuthorityBinding:
    """Trusted ResolvedPlan/Activation ceilings supplied by ``tobkiri_host``."""

    caller_effect_ceiling: AuthorityScope
    runtime_safety_ceiling: AuthorityScope
    profile_admin_ceiling: AuthorityScope
    profile_id: str
    activation_id: str
    activation_digest: str
    plan_digest: str
    profile_authority_digest: str
    fencing_token: int
    security_epoch: int

    def validates_context(self, context: InvocationContext) -> bool:
        """Return whether this captured binding exactly matches the invocation."""

        return all(
            (
                self.profile_id == context.profile_id,
                self.activation_id == context.activation_id,
                self.activation_digest == context.activation_digest,
                self.plan_digest == context.plan_digest,
                self.profile_authority_digest == context.profile_authority_digest,
                self.fencing_token == context.fencing_token,
                self.security_epoch == context.security_epoch,
            )
        )


class AuthorityBindingResolver(Protocol):
    """TCB callback implemented by the future Host ResolvedPlan service."""

    def resolve_authority_binding(
        self,
        *,
        context: InvocationContext,
        caller: FunctionPrincipal,
        target: FunctionPrincipal,
    ) -> AuthorityBinding:
        """Return exact, Host-authenticated ceilings for one activation binding."""


@dataclass(frozen=True)
class AuthorizationResult:
    """Opaque Provider-facing result; it intentionally contains no Grant record."""

    lease_token: str
    lease_id: str
    expires_at: float
    authority_mode: AuthorityMode
    resource_namespace: str


@runtime_checkable
class AuthorityKernelProtocol(Protocol):
    """Narrow call surface consumed by the separate ``tobkiri_host`` package."""

    def register_execution_domain(
        self,
        domain: ExecutionDomain,
        *,
        session_id: str,
        channel_digest: str,
        principal: FunctionPrincipal,
    ) -> None:
        """Register and authenticate one Host-spawned execution domain."""

    def authorize(
        self, context: InvocationContext, request_scope: AuthorityScope
    ) -> AuthorizationResult:
        """Evaluate the full intersection and issue an audited one-use Lease."""

    def check_static_path(self, context: InvocationContext, request_scope: AuthorityScope) -> None:
        """Check an exact potential authority path without issuing authority."""

    def dispatch(
        self,
        lease_token: str,
        *,
        target_domain_id: str,
        target_boot_epoch: int,
        request_digest: str,
    ) -> InvocationLease:
        """Consume a Lease at the Provider effect boundary."""

    def finish(self, lease_id: str, *, state: LeaseState, outcome_digest: str) -> None:
        """Commit the authoritative effect outcome."""

    def fence_request(self, request_id: str) -> list[str]:
        """Revoke unused authority for one exact request."""

    def revoke(self, *, target_kind: str, target_id: str, reason: str) -> str:
        """Revoke exact authority and fence affected runtime state."""

    def advance_security_epoch(self, reason: str) -> int:
        """Fence all previous-epoch runtime authority."""

    def commit_successor_authority(
        self,
        provider: ProviderAuthorityRecord,
        grant: GrantRecord,
        *,
        host_extension_trust: HostExtensionTrustRecord | None = None,
    ) -> None:
        """Atomically persist already-verified non-expanding successor records."""


class AuthorityKernel:
    """Canonical v4 authority lifecycle, unreachable from legacy dispatch."""

    def __init__(
        self,
        store: AuthorityStore,
        binding_resolver: AuthorityBindingResolver,
        *,
        clock: Callable[[], float] = time.time,
        lease_ttl_seconds: float = 30.0,
        terminate_domain: Callable[[str], None] | None = None,
    ) -> None:
        if (
            isinstance(lease_ttl_seconds, bool)
            or not math.isfinite(float(lease_ttl_seconds))
            or lease_ttl_seconds <= 0
            or lease_ttl_seconds > 300
        ):
            raise ValueError("InvocationLease TTL must be between 0 and 300 seconds")
        self.store = store
        self._binding_resolver = binding_resolver
        self._clock = clock
        self._lease_ttl_seconds = float(lease_ttl_seconds)
        self._terminate_domain = terminate_domain or (lambda _domain_id: None)
        self._emergency_stop = False

    def register_execution_domain(
        self,
        domain: ExecutionDomain,
        *,
        session_id: str,
        channel_digest: str,
        principal: FunctionPrincipal,
    ) -> None:
        """Register a domain and bind its authenticated channel.

        Domain identity is supplied by Host spawn/routing.  No payload-supplied
        Function ID or domain field participates in this operation.
        """

        if domain.security_epoch != self.store.security_epoch:
            raise AuthorityDenied("execution domain has a stale SecurityEpoch")
        if domain.state is not DomainState.ACTIVE:
            raise AuthorityDenied("only active execution domains can register")
        if principal.principal_id not in domain.principal_ids:
            raise AuthorityDenied("principal is not assigned to execution domain")
        prepared = replace(domain, state=DomainState.STARTING)
        self.store.put_record(prepared)
        active = self.store.transition_domain(
            domain.domain_id,
            expected_boot_epoch=domain.boot_epoch,
            expected_state=DomainState.STARTING,
            new_state=DomainState.ACTIVE,
        )
        self.store.bind_authenticated_session(
            session_id=session_id,
            domain=active,
            channel_digest=channel_digest,
            principal_id=principal.principal_id,
        )

    def commit_approval_bundle(
        self,
        approval: ApprovalRecord,
        *,
        host_extension_trust: HostExtensionTrustRecord | None = None,
        provider_authorities: tuple[ProviderAuthorityRecord, ...],
        grants: tuple[GrantRecord, ...],
    ) -> None:
        """Atomically commit Provider trust and caller-specific use authority."""

        if approval.decision != "approved":
            raise AuthorityValidationError("denied Approval cannot mint authority")
        if approval.security_epoch != self.store.security_epoch:
            raise AuthorityDenied("Approval snapshot has a stale SecurityEpoch")
        if not provider_authorities or not grants:
            raise AuthorityValidationError("approval bundle cannot be partial")
        if host_extension_trust is not None and (
            host_extension_trust.security_epoch != approval.security_epoch
            or host_extension_trust.revoked
        ):
            raise AuthorityValidationError("Host Extension trust snapshot mismatch")
        provider_ids = {item.provider.principal_id for item in provider_authorities}
        for provider in provider_authorities:
            if provider.provider != approval.target:
                raise AuthorityValidationError("Provider authority does not match Approval target")
            self._validate_provider_domain(provider, trust_override=host_extension_trust)
            if provider.security_epoch != approval.security_epoch:
                raise AuthorityValidationError("Provider authority epoch mismatch")
        for grant in grants:
            if (
                grant.approval_id != approval.approval_id
                or grant.caller != approval.caller
                or grant.target != approval.target
                or grant.profile_id != approval.profile_id
                or grant.target.principal_id not in provider_ids
                or grant.security_epoch != approval.security_epoch
            ):
                raise AuthorityValidationError("Grant does not match Approval snapshot")
            matching_provider = next(
                item for item in provider_authorities if item.provider == grant.target
            )
            if grant.target_publisher_lineage != matching_provider.publisher_lineage:
                raise AuthorityValidationError("Grant target publisher does not match")
            if not grant.scope.is_subset_of(matching_provider.scope):
                raise AuthorityValidationError("Grant exceeds Provider authority")
            provider_domain = self.store.get_domain(matching_provider.execution_domain_id)
            if (
                provider_domain is None
                or provider_domain.profile_id != grant.profile_id
                or provider_domain.activation_id != grant.activation_id
            ):
                raise AuthorityValidationError("Grant activation does not match Provider domain")
        records = (
            ((host_extension_trust,) if host_extension_trust is not None else ())
            + (approval,)
            + provider_authorities
            + grants
        )
        self.store.put_records_atomically(records)

    def commit_policy_ephemeral_grant(self, grant: GrantRecord) -> None:
        """Persist a low-risk policy Grant through the same runtime path."""

        if grant.lifetime is not GrantLifetime.POLICY_EPHEMERAL:
            raise AuthorityValidationError("Grant is not policy_ephemeral")
        if grant.approval_id is not None:
            raise AuthorityValidationError("policy_ephemeral Grant has no Approval")
        if grant.security_epoch != self.store.security_epoch:
            raise AuthorityDenied("policy Grant has a stale SecurityEpoch")
        self.store.put_records_atomically((grant,))

    def commit_successor_authority(
        self,
        provider: ProviderAuthorityRecord,
        grant: GrantRecord,
        *,
        host_extension_trust: HostExtensionTrustRecord | None = None,
    ) -> None:
        """Atomically commit exact successor records minted by update policy."""

        if (
            provider.security_epoch != self.store.security_epoch
            or grant.security_epoch != self.store.security_epoch
        ):
            raise AuthorityDenied("successor authority has a stale SecurityEpoch")
        if (
            grant.target != provider.provider
            or grant.target_publisher_lineage != provider.publisher_lineage
            or not grant.scope.is_subset_of(provider.scope)
        ):
            raise AuthorityValidationError(
                "successor Grant exceeds or mismatches Provider authority"
            )
        self._validate_provider_domain(provider, trust_override=host_extension_trust)
        provider_domain = self.store.get_domain(provider.execution_domain_id)
        if (
            provider_domain is None
            or provider_domain.profile_id != grant.profile_id
            or provider_domain.activation_id != grant.activation_id
        ):
            raise AuthorityValidationError(
                "successor Grant activation does not match Provider domain"
            )
        records: list[ProviderAuthorityRecord | GrantRecord | HostExtensionTrustRecord] = [
            provider,
            grant,
        ]
        if host_extension_trust is not None:
            records.insert(0, host_extension_trust)
        self.store.put_records_atomically(records)

    def authorize(
        self, context: InvocationContext, request_scope: AuthorityScope
    ) -> AuthorizationResult:
        """Evaluate effective authority and reserve an audited InvocationLease."""

        if self._emergency_stop:
            raise AuthorityDenied("authority kernel is emergency-fenced")
        self.store.expire_leases()
        now = self._clock()
        epoch = self.store.security_epoch
        if context.security_epoch != epoch:
            raise AuthorityDenied("invocation has a stale SecurityEpoch", code="stale_epoch")
        caller_domain, caller_principal_id = self.store.resolve_authenticated_session(
            context.caller_session_id
        )
        caller = self._principal_in_domain(caller_domain, caller_principal_id)
        if (
            caller_domain.profile_id != context.profile_id
            or caller_domain.activation_id != context.activation_id
            or caller_domain.fencing_token != context.fencing_token
        ):
            raise AuthorityDenied("caller execution-domain binding is stale")
        target_domain = self.store.get_domain(context.target_domain_id)
        if target_domain is None:
            raise AuthorityDenied("target execution domain is unavailable")
        self._validate_target_domain(context, target_domain)

        binding = self._binding_resolver.resolve_authority_binding(
            context=context,
            caller=caller,
            target=context.target,
        )
        if not binding.validates_context(context):
            raise AuthorityDenied("ResolvedPlan authority binding does not match")

        provider = self._select_provider_authority(
            context=context,
            target_domain=target_domain,
            request_scope=request_scope,
            now=now,
        )
        grant = self._select_grant(
            context=context,
            caller=caller,
            request_scope=request_scope,
            now=now,
        )
        reserved_uses, committed_uses = self.store.grant_usage(grant.grant_id)
        if grant.max_uses is not None and reserved_uses + committed_uses >= grant.max_uses:
            raise AuthorityDenied("Grant use limit is exhausted")
        ceilings = (
            binding.caller_effect_ceiling,
            binding.runtime_safety_ceiling,
            binding.profile_admin_ceiling,
            grant.scope,
            provider.scope,
        )
        effective_scope = intersect_scopes(*ceilings)
        if not request_scope.is_subset_of(effective_scope):
            raise AuthorityDenied("request exceeds effective authority intersection")
        if request_scope.opaque and request_scope.exact_request_digest != context.request_digest:
            raise AuthorityDenied("opaque scope is not bound to this request")

        self._validate_call_chain(context, caller, grant, request_scope)
        lease = InvocationLease(
            lease_id="lease-" + secrets.token_hex(16),
            request_id=context.request_id,
            caller=caller,
            target=context.target,
            caller_domain_id=caller_domain.domain_id,
            caller_boot_epoch=caller_domain.boot_epoch,
            target_domain_id=target_domain.domain_id,
            target_boot_epoch=target_domain.boot_epoch,
            request_digest=context.request_digest,
            effect_digest=context.effect_digest,
            authorized_scope=request_scope,
            resource_namespace=target_domain.resource_namespace,
            profile_id=context.profile_id,
            activation_id=context.activation_id,
            activation_digest=context.activation_digest,
            plan_digest=context.plan_digest,
            profile_authority_digest=context.profile_authority_digest,
            fencing_token=context.fencing_token,
            caller_publisher_lineage=grant.caller_publisher_lineage,
            target_publisher_lineage=grant.target_publisher_lineage,
            host_extension_id=provider.host_extension_id,
            provider_authority_id=provider.record_id,
            provider_authority_digest=provider.digest,
            grant_id=grant.grant_id,
            audit_reservation_id="audit-" + secrets.token_hex(16),
            security_epoch=epoch,
            issued_at=now,
            expires_at=now + self._lease_ttl_seconds,
            call_chain=context.call_chain,
        )
        revocation_targets = (
            ("function_principal", caller.principal_id),
            ("function_principal", context.target.principal_id),
            ("pack_artifact", caller.parent_artifact_digest),
            ("pack_artifact", context.target.parent_artifact_digest),
            ("publisher", grant.caller_publisher_lineage),
            ("publisher", grant.target_publisher_lineage),
            ("host_extension", provider.host_extension_id),
            ("execution_domain", caller_domain.domain_id),
            ("execution_domain", target_domain.domain_id),
            ("profile", context.profile_id),
            ("activation", context.activation_id),
            ("grant", grant.grant_id),
            ("provider_authority", provider.record_id),
        )
        token = self.store.issue_lease_with_audit(
            grant=grant,
            lease=lease,
            audit_payload={
                "lease_id": lease.lease_id,
                "request_id": context.request_id,
                "request_digest": context.request_digest,
                "effect_digest": context.effect_digest,
                "caller_principal_id": caller.principal_id,
                "target_principal_id": context.target.principal_id,
                "caller_domain_id": caller_domain.domain_id,
                "target_domain_id": target_domain.domain_id,
                "profile_id": context.profile_id,
                "activation_id": context.activation_id,
                "activation_digest": context.activation_digest,
                "plan_digest": context.plan_digest,
                "profile_authority_digest": context.profile_authority_digest,
                "fencing_token": context.fencing_token,
                "security_epoch": epoch,
                "authority_mode": provider.authority_mode.value,
                "scope_digest": request_scope.digest,
                "provider_authority_digest": provider.digest,
                "grant_digest": grant.digest,
            },
            revocation_targets=revocation_targets,
        )
        return AuthorizationResult(
            lease_token=token,
            lease_id=lease.lease_id,
            expires_at=lease.expires_at,
            authority_mode=provider.authority_mode,
            resource_namespace=target_domain.resource_namespace,
        )

    def check_static_path(self, context: InvocationContext, request_scope: AuthorityScope) -> None:
        """Validate a potential exact authority path without reserving a use.

        Static admission is deliberately read-only: it neither creates a lease
        nor appends an audit event.  Final authorization repeats every check.
        """

        if self._emergency_stop:
            raise AuthorityDenied("authority kernel is emergency-fenced")
        if context.security_epoch != self.store.security_epoch:
            raise AuthorityDenied("invocation has a stale SecurityEpoch", code="stale_epoch")
        caller_domain, caller_principal_id = self.store.resolve_authenticated_session(
            context.caller_session_id
        )
        caller = self._principal_in_domain(caller_domain, caller_principal_id)
        if (
            caller_domain.profile_id != context.profile_id
            or caller_domain.activation_id != context.activation_id
            or caller_domain.fencing_token != context.fencing_token
        ):
            raise AuthorityDenied("caller execution-domain binding is stale")
        target_domain = self.store.get_domain(context.target_domain_id)
        if target_domain is None:
            raise AuthorityDenied("target execution domain is unavailable")
        self._validate_target_domain(context, target_domain)
        binding = self._binding_resolver.resolve_authority_binding(
            context=context,
            caller=caller,
            target=context.target,
        )
        if not binding.validates_context(context):
            raise AuthorityDenied("ResolvedPlan authority binding does not match")
        now = self._clock()
        provider = self._select_provider_authority(
            context=context,
            target_domain=target_domain,
            request_scope=request_scope,
            now=now,
        )
        grant = self._select_grant(
            context=context,
            caller=caller,
            request_scope=request_scope,
            now=now,
        )
        reserved_uses, committed_uses = self.store.grant_usage(grant.grant_id)
        if grant.max_uses is not None and reserved_uses + committed_uses >= grant.max_uses:
            raise AuthorityDenied("Grant use limit is exhausted")
        effective_scope = intersect_scopes(
            binding.caller_effect_ceiling,
            binding.runtime_safety_ceiling,
            binding.profile_admin_ceiling,
            grant.scope,
            provider.scope,
        )
        if not request_scope.is_subset_of(effective_scope):
            raise AuthorityDenied("request exceeds effective authority intersection")
        if request_scope.opaque and request_scope.exact_request_digest != context.request_digest:
            raise AuthorityDenied("opaque scope is not bound to this request")
        self._validate_call_chain(context, caller, grant, request_scope)

    def dispatch(
        self,
        lease_token: str,
        *,
        target_domain_id: str,
        target_boot_epoch: int,
        request_digest: str,
    ) -> InvocationLease:
        """Consume a Lease once, immediately before a Host effect begins."""

        if self._emergency_stop:
            raise AuthorityDenied("authority kernel is emergency-fenced")
        return self.store.dispatch_lease(
            lease_token,
            target_domain_id=target_domain_id,
            target_boot_epoch=target_boot_epoch,
            request_digest=request_digest,
        )

    def finish(self, lease_id: str, *, state: LeaseState, outcome_digest: str) -> None:
        """Durably commit success, failure, or ambiguous external effect."""

        if self._emergency_stop:
            raise AuthorityDenied("authority kernel is emergency-fenced")
        self.store.finish_lease(
            lease_id,
            state=state,
            outcome_digest=outcome_digest,
        )

    def recover(self) -> list[str]:
        """Recover crash-surviving effects as ambiguous without retrying them."""

        self.store.expire_leases()
        return self.store.recover_incomplete_effects()

    def fence_request(self, request_id: str) -> list[str]:
        """Fence unused request leases, emergency-stopping on audit failure."""

        try:
            return self.store.fence_request(request_id)
        except (AuditUnavailable, AuthorityStoreError):
            self._emergency_stop = True
            for domain in self.store.list_domains():
                self._terminate_domain(domain.domain_id)
            raise

    def revoke(self, *, target_kind: str, target_id: str, reason: str) -> str:
        """Durably revoke exact authority before terminating affected domains."""

        try:
            revocation_id = self.store.revoke(
                target_kind=target_kind,
                target_id=target_id,
                reason=reason,
            )
        except (AuditUnavailable, AuthorityStoreError):
            self._emergency_stop = True
            for domain in self.store.list_domains():
                self._terminate_domain(domain.domain_id)
            raise
        if target_kind == "execution_domain":
            self._terminate_domain(target_id)
        elif target_kind in {
            "provider_authority",
            "host_extension",
            "publisher",
        }:
            for provider in self.store.list_provider_authorities():
                if (
                    (target_kind == "provider_authority" and provider.record_id == target_id)
                    or (target_kind == "host_extension" and provider.host_extension_id == target_id)
                    or (target_kind == "publisher" and provider.publisher_lineage == target_id)
                ):
                    self._terminate_domain(provider.execution_domain_id)
        elif target_kind in {
            "function_principal",
            "profile",
            "activation",
            "pack_artifact",
            "global",
        }:
            for domain in self.store.list_domains():
                if (
                    target_kind == "global"
                    or (target_kind == "function_principal" and target_id in domain.principal_ids)
                    or (target_kind == "profile" and target_id == domain.profile_id)
                    or (target_kind == "activation" and target_id == domain.activation_id)
                    or (
                        target_kind == "pack_artifact"
                        and any(
                            target_id == principal.parent_artifact_digest
                            for principal in domain.principals
                        )
                    )
                ):
                    self._terminate_domain(domain.domain_id)
        return revocation_id

    def advance_security_epoch(self, reason: str) -> int:
        """Advance epoch, revoke old runtime state, and stop old domains."""

        old_domains = self.store.list_domains()
        try:
            epoch = self.store.advance_security_epoch(reason)
        except (AuditUnavailable, AuthorityStoreError):
            self._emergency_stop = True
            for domain in old_domains:
                self._terminate_domain(domain.domain_id)
            raise
        for domain in old_domains:
            if domain.security_epoch < epoch:
                self._terminate_domain(domain.domain_id)
        return epoch

    def _select_provider_authority(
        self,
        *,
        context: InvocationContext,
        target_domain: ExecutionDomain,
        request_scope: AuthorityScope,
        now: float,
    ) -> ProviderAuthorityRecord:
        matches: list[ProviderAuthorityRecord] = []
        for record in self.store.list_provider_authorities():
            if (
                not record.revoked
                and record.provider == context.target
                and record.execution_domain_id == target_domain.domain_id
                and record.execution_domain_identity_digest == target_domain.identity_digest
                and record.security_epoch == context.security_epoch
                and record.valid_from <= now
                and (record.expires_at is None or now < record.expires_at)
                and request_scope.is_subset_of(record.scope)
                and not self.store.is_revoked("provider_authority", record.record_id)
            ):
                self._validate_provider_domain(record)
                matches.append(record)
        if len(matches) != 1:
            raise AuthorityDenied("exact Provider authority is missing or ambiguous")
        return matches[0]

    def _select_grant(
        self,
        *,
        context: InvocationContext,
        caller: FunctionPrincipal,
        request_scope: AuthorityScope,
        now: float,
    ) -> GrantRecord:
        matches: list[GrantRecord] = []
        for grant in self.store.list_grants():
            if (
                not grant.revoked
                and grant.caller == caller
                and grant.target == context.target
                and grant.profile_id == context.profile_id
                and grant.activation_id == context.activation_id
                and grant.profile_authority_digest == context.profile_authority_digest
                and grant.security_epoch == context.security_epoch
                and grant.issued_at <= now
                and (grant.expires_at is None or now < grant.expires_at)
                and request_scope.is_subset_of(grant.scope)
                and not self.store.is_revoked("grant", grant.grant_id)
            ):
                if grant.lifetime is GrantLifetime.SESSION:
                    if grant.session_id != context.caller_session_id:
                        continue
                if grant.approval_id is not None:
                    approval = self.store.get_approval(grant.approval_id)
                    if approval is None or approval.decision != "approved":
                        continue
                elif grant.lifetime is not GrantLifetime.POLICY_EPHEMERAL:
                    continue
                matches.append(grant)
        if len(matches) != 1:
            raise AuthorityDenied("exact caller Grant is missing or ambiguous")
        return matches[0]

    def _validate_target_domain(
        self, context: InvocationContext, target_domain: ExecutionDomain
    ) -> None:
        if (
            target_domain.state is not DomainState.ACTIVE
            or target_domain.security_epoch != context.security_epoch
            or target_domain.profile_id != context.profile_id
            or target_domain.activation_id != context.activation_id
            or target_domain.boot_epoch != context.target_boot_epoch
            or target_domain.fencing_token != context.fencing_token
            or context.target.principal_id not in target_domain.principal_ids
        ):
            raise AuthorityDenied("target execution-domain binding is stale")

    def _validate_provider_domain(
        self,
        provider: ProviderAuthorityRecord,
        *,
        trust_override: HostExtensionTrustRecord | None = None,
    ) -> None:
        domain = self.store.get_domain(provider.execution_domain_id)
        if domain is None or domain.identity_digest != provider.execution_domain_identity_digest:
            raise AuthorityDenied("Provider execution domain does not match authority")
        if provider.provider.principal_id not in domain.principal_ids:
            raise AuthorityDenied("Provider principal is not enforced by its domain")
        if provider.authority_mode is AuthorityMode.OS_ENTITLEMENT:
            if (
                domain.boundary is not DomainBoundary.DEDICATED_PROCESS
                or len(domain.principals) != 1
            ):
                raise AuthorityDenied("os_entitlement Provider requires an exact dedicated process")
        elif domain.boundary is DomainBoundary.AUTHORITY_EQUIVALENCE:
            if (
                domain.equivalence is None
                or domain.equivalence.provider_ceiling_digest != provider.scope.digest
            ):
                raise AuthorityDenied("co-located Provider authority is not equivalent")
        if provider.host_extension_id != "runtime-tcb":
            trust = trust_override or self.store.get_host_extension_trust(
                provider.host_extension_id
            )
            now = self._clock()
            if (
                trust is None
                or trust.trust_id != provider.host_extension_id
                or trust.revoked
                or trust.security_epoch != provider.security_epoch
                or trust.publisher_lineage != provider.publisher_lineage
                or trust.parent_artifact_digest != provider.provider.parent_artifact_digest
                or provider.provider.principal_id not in trust.provider_principal_ids
                or trust.valid_from > now
                or (trust.expires_at is not None and now >= trust.expires_at)
                or self.store.is_revoked("host_extension", trust.trust_id)
            ):
                raise AuthorityDenied("Host Extension trust is unavailable")

    @staticmethod
    def _principal_in_domain(domain: ExecutionDomain, principal_id: str) -> FunctionPrincipal:
        for principal in domain.principals:
            if principal.principal_id == principal_id:
                return principal
        raise AuthorityDenied("authenticated caller principal is unavailable")

    def _validate_call_chain(
        self,
        context: InvocationContext,
        caller: FunctionPrincipal,
        grant: GrantRecord,
        request_scope: AuthorityScope,
    ) -> None:
        chain = context.call_chain
        if len(chain) > 4 or len(set(chain)) != len(chain):
            raise AuthorityDenied("delegation chain is cyclic or too deep")
        if caller.principal_id in chain or context.target.principal_id in chain:
            raise AuthorityDenied("delegation chain substitutes an active principal")
        if not chain:
            return
        # An indirect invocation still requires an exact immediate-caller Grant.
        # Delegation metadata may only be present when that Grant explicitly opts in.
        if not grant.delegation_allowed or len(chain) > grant.max_delegation_depth:
            raise AuthorityDenied("Grant does not permit this delegation chain")
        if context.parent_lease_id:
            parent_result = self.store.get_lease(context.parent_lease_id)
            if parent_result is None:
                raise AuthorityDenied("parent InvocationLease is unavailable")
            parent, parent_state = parent_result
            if (
                parent_state is not LeaseState.DISPATCHED
                or parent.target != caller
                or not request_scope.is_subset_of(parent.authorized_scope)
            ):
                raise AuthorityDenied("child authority is not attenuated from parent")


def mint_successor_grant(
    old: GrantRecord,
    *,
    new_caller: FunctionPrincipal,
    new_target: FunctionPrincipal,
    new_activation_id: str,
    new_profile_authority_digest: str,
    security_epoch: int,
    policy: UpdateTrustPolicy,
    evidence: SuccessorEvidence,
    issued_at: float,
) -> GrantRecord:
    """Mint a new-parent Grant only after verified non-expanding update evidence."""

    if not evidence.permits_successor(policy):
        raise AuthorityDenied("update evidence does not permit successor authority")
    for previous, successor in ((old.caller, new_caller), (old.target, new_target)):
        if (
            previous.function_id != successor.function_id
            or previous.operation_id != successor.operation_id
            or previous.contract_revision_digest != successor.contract_revision_digest
        ):
            raise AuthorityDenied("successor Function semantics changed")
    if old.caller == new_caller and old.target == new_target:
        raise AuthorityValidationError("successor must bind a new exact artifact identity")
    return replace(
        old,
        grant_id="grant-" + secrets.token_hex(16),
        caller=new_caller,
        target=new_target,
        activation_id=new_activation_id,
        profile_authority_digest=new_profile_authority_digest,
        security_epoch=security_epoch,
        issued_at=issued_at,
        revoked=False,
    )


def mint_successor_provider_authority(
    old: ProviderAuthorityRecord,
    *,
    new_provider: FunctionPrincipal,
    new_domain: ExecutionDomain,
    new_host_extension_id: str | None = None,
    security_epoch: int,
    policy: UpdateTrustPolicy,
    evidence: SuccessorEvidence,
    valid_from: float,
) -> ProviderAuthorityRecord:
    """Mint a new exact Provider record without reusing old-artifact authority."""

    if not evidence.permits_successor(policy):
        raise AuthorityDenied("update evidence does not permit successor authority")
    if (
        old.provider.function_id != new_provider.function_id
        or old.provider.operation_id != new_provider.operation_id
        or old.provider.contract_revision_digest != new_provider.contract_revision_digest
    ):
        raise AuthorityDenied("successor Provider semantics changed")
    if old.provider == new_provider:
        raise AuthorityValidationError("successor Provider must have a new artifact identity")
    if old.host_extension_id != "runtime-tcb" and not new_host_extension_id:
        raise AuthorityValidationError("Host Extension successor requires a new exact trust record")
    return replace(
        old,
        record_id="provider-authority-" + secrets.token_hex(16),
        provider=new_provider,
        execution_domain_id=new_domain.domain_id,
        execution_domain_identity_digest=new_domain.identity_digest,
        host_extension_id=new_host_extension_id or old.host_extension_id,
        security_epoch=security_epoch,
        valid_from=valid_from,
        revoked=False,
    )


__all__ = [
    "AuditUnavailable",
    "AuthorityBinding",
    "AuthorityBindingResolver",
    "AuthorityKernel",
    "AuthorityKernelProtocol",
    "AuthorityStoreError",
    "AuthorizationResult",
    "mint_successor_grant",
    "mint_successor_provider_authority",
]
