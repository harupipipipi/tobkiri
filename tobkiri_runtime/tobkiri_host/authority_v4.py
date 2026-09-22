"""Production-shaped adapter for the canonical ADR-014 authority kernel.

This module is the only bridge between the Pack v4 host DTOs and
``core_runtime.authority.v4``.  It never consults legacy authority services and
never derives a Function principal from Pack-supplied invocation fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Mapping, Protocol

from core_runtime.authority.v4 import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityKernel,
    AuthorityScope,
    ExecutionDomain,
    FunctionPrincipal,
    GrantRecord,
    HostExtensionTrustRecord,
    InvocationContext,
    InvocationLease,
    LeaseState,
    ProviderAuthorityRecord,
    authority_digest,
)

from .contracts import ResolvedOperationBinding
from .models import OpaqueAuthorityRef, RequestContext
from .ports import (
    FinalAuthorizationQuery,
    OpaqueAuditReservation,
    OpaqueInvocationLease,
    StaticAuthorityQuery,
)


class PrincipalReferenceResolver(Protocol):
    """Resolve Host-minted opaque references from the captured plan only."""

    def resolve_principal(self, reference: OpaqueAuthorityRef) -> FunctionPrincipal:
        """Return the one exact principal represented by ``reference``."""


@dataclass(frozen=True)
class TriggerAuthorityBinding:
    """Host-captured authority inputs for one durable Trigger registration."""

    context: InvocationContext
    scope: AuthorityScope


class TriggerAuthorityResolver(Protocol):
    """Resolve durable Trigger registrations without running Scheduler code."""

    def resolve_trigger_authority(
        self,
        *,
        registration_id: str,
        occurrence_id: str,
        target: FunctionPrincipal,
        security_epoch: int,
    ) -> TriggerAuthorityBinding:
        """Return an occurrence-specific, Host-authenticated binding."""


@dataclass(frozen=True)
class _IssuedLease:
    lease_id: str
    request_id: str
    request_digest: str
    target_principal_id: str


class AuthorityV4Adapter:
    """Implement both Host authority and authoritative-audit ports with v4.

    ``AuthorityKernel.authorize`` atomically reserves Grant use, audit, and the
    InvocationLease.  Consequently the AuditPort methods below are lifecycle
    projections of that same durable record; they never create a second,
    best-effort audit stream.
    """

    def __init__(
        self,
        kernel: AuthorityKernel,
        principal_resolver: PrincipalReferenceResolver,
        *,
        trigger_resolver: TriggerAuthorityResolver | None = None,
    ) -> None:
        self._kernel = kernel
        self._principals = principal_resolver
        self._triggers = trigger_resolver
        self._issued_by_request: dict[str, _IssuedLease] = {}
        self._lock = RLock()

    def register_execution_domain(
        self,
        domain: ExecutionDomain,
        *,
        session_id: str,
        channel_digest: str,
        principal_ref: OpaqueAuthorityRef,
    ) -> None:
        """Register a Host-spawned domain using one exact principal reference."""

        self._kernel.register_execution_domain(
            domain,
            session_id=session_id,
            channel_digest=channel_digest,
            principal=self._resolve_exact(principal_ref),
        )

    def commit_approval_bundle(
        self,
        approval: ApprovalRecord,
        *,
        host_extension_trust: HostExtensionTrustRecord | None = None,
        provider_authorities: tuple[ProviderAuthorityRecord, ...],
        grants: tuple[GrantRecord, ...],
    ) -> None:
        """Commit explicit Approval/ProviderAuthority/Grant records atomically."""

        self._kernel.commit_approval_bundle(
            approval,
            host_extension_trust=host_extension_trust,
            provider_authorities=provider_authorities,
            grants=grants,
        )

    def check_static_path(self, query: StaticAuthorityQuery) -> None:
        """Perform the read-only canonical authority preflight."""

        context, scope = self._translate_query(
            query.context,
            query.target_principal,
            query.request_digest,
            query.effect_scope,
        )
        self._kernel.check_static_path(context, scope)

    def authorize_and_issue_lease(
        self,
        query: FinalAuthorizationQuery,
    ) -> OpaqueInvocationLease:
        """Issue exactly one v4 lease after evidence and identity revalidation."""

        context, scope = self._translate_query(
            query.context,
            query.target_principal,
            query.request_digest,
            query.effect_scope,
        )
        if (
            query.evidence.domain_ref.value != query.context.target_domain_id
            or query.evidence.executable_digest != context.target.function_implementation_digest
            or query.evidence.backend_digest != query.context.target_backend_digest
            or not query.evidence.authenticated_channel
            or not query.evidence.nonce_fresh
        ):
            raise AuthorityDenied("runtime evidence does not match captured binding")
        result = self._kernel.authorize(context, scope)
        issued = _IssuedLease(
            lease_id=result.lease_id,
            request_id=context.request_id,
            request_digest=context.request_digest,
            target_principal_id=context.target.principal_id,
        )
        with self._lock:
            if context.request_id in self._issued_by_request:
                self._kernel.fence_request(context.request_id)
                raise AuthorityDenied("request already has an InvocationLease")
            self._issued_by_request[context.request_id] = issued
        return OpaqueInvocationLease(result.lease_token.encode("ascii"))

    def recheck_effect_boundary(
        self,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        lease: OpaqueInvocationLease,
    ) -> None:
        """Atomically consume the lease at the final Provider boundary."""

        target_principal = self._resolve_exact(target)
        token = self._decode_transport(lease)
        durable, state = self._kernel.store.inspect_lease_token(token)
        if state is not LeaseState.ISSUED:
            raise AuthorityDenied("InvocationLease was already consumed")
        self._validate_lease_context(durable, context, target_principal)
        self._kernel.dispatch(
            token,
            target_domain_id=context.target_domain_id,
            target_boot_epoch=context.target_boot_epoch,
            request_digest=durable.request_digest,
        )

    def fence_request(self, request_id: str) -> None:
        """Durably revoke unused authority associated with one request."""

        self._kernel.fence_request(request_id)
        with self._lock:
            self._issued_by_request.pop(request_id, None)

    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        """Issue a canonical occurrence-bound, single-use Trigger lease."""

        if self._triggers is None:
            raise AuthorityDenied("Trigger authority is not configured")
        principal = self._resolve_exact(target)
        binding = self._triggers.resolve_trigger_authority(
            registration_id=registration_id,
            occurrence_id=occurrence_id,
            target=principal,
            security_epoch=security_epoch,
        )
        expected_request = authority_digest(
            {
                "registration_id": registration_id,
                "occurrence_id": occurrence_id,
                "target_principal_id": principal.principal_id,
                "security_epoch": security_epoch,
            }
        )
        if (
            binding.context.target != principal
            or binding.context.request_digest != expected_request
            or binding.context.security_epoch != security_epoch
        ):
            raise AuthorityDenied("Trigger authority binding does not match occurrence")
        result = self._kernel.authorize(binding.context, binding.scope)
        return OpaqueInvocationLease(result.lease_token.encode("ascii"))

    def reserve_effect(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        request_digest: str,
    ) -> OpaqueAuditReservation:
        """Return the audit reservation created atomically with authorization."""

        with self._lock:
            issued = self._issued_by_request.get(context.request_id)
        if (
            issued is None
            or issued.request_digest != request_digest
            or issued.target_principal_id != self._resolve_exact(binding.principal_ref).principal_id
        ):
            raise AuthorityDenied("authoritative audit reservation is unavailable")
        return OpaqueAuditReservation(issued.lease_id)

    def mark_dispatched(self, reservation: OpaqueAuditReservation) -> None:
        """Verify canonical dispatch was durably journaled."""

        _lease, state = self._require_lease(reservation)
        if state is not LeaseState.DISPATCHED:
            raise AuthorityDenied("effect was not durably dispatched")

    def commit_effect(
        self,
        reservation: OpaqueAuditReservation,
        outcome_digest: str,
    ) -> None:
        """Durably commit the effect in the canonical audit transaction."""

        lease, state = self._require_lease(reservation)
        if state is not LeaseState.DISPATCHED:
            raise AuthorityDenied("effect cannot be committed")
        self._kernel.finish(
            lease.lease_id,
            state=LeaseState.COMMITTED,
            outcome_digest=outcome_digest,
        )
        self._forget(lease.request_id)

    def fail_effect(
        self,
        reservation: OpaqueAuditReservation,
        stable_code: str,
        ambiguous: bool,
    ) -> None:
        """Durably record a failed or ambiguous Provider outcome."""

        lease, state = self._require_lease(reservation)
        if state is not LeaseState.DISPATCHED:
            raise AuthorityDenied("effect failure cannot be recorded")
        outcome_digest = authority_digest(
            {
                "stable_code": stable_code,
                "ambiguous": ambiguous,
                "lease_id": lease.lease_id,
            }
        )
        self._kernel.finish(
            lease.lease_id,
            state=LeaseState.AMBIGUOUS if ambiguous else LeaseState.FAILED,
            outcome_digest=outcome_digest,
        )
        self._forget(lease.request_id)

    def recover(self) -> list[str]:
        """Recover crash-surviving dispatched effects as ambiguous."""

        recovered = self._kernel.recover()
        with self._lock:
            for request_id, issued in tuple(self._issued_by_request.items()):
                if issued.lease_id in recovered:
                    self._issued_by_request.pop(request_id, None)
        return recovered

    def revoke(self, *, target_kind: str, target_id: str, reason: str) -> str:
        """Durably revoke exact v4 authority through the canonical kernel."""

        return self._kernel.revoke(
            target_kind=target_kind,
            target_id=target_id,
            reason=reason,
        )

    def advance_security_epoch(self, reason: str) -> int:
        """Advance the Host SecurityEpoch and discard all cached correlations."""

        epoch = self._kernel.advance_security_epoch(reason)
        with self._lock:
            self._issued_by_request.clear()
        return epoch

    def _translate_query(
        self,
        context: RequestContext,
        target_ref: OpaqueAuthorityRef,
        request_digest: str,
        effect_scope: Mapping[str, object],
    ) -> tuple[InvocationContext, AuthorityScope]:
        target = self._resolve_exact(target_ref)
        caller = self._resolve_exact(context.caller_principal)
        caller_domain, session_principal_id = self._kernel.store.resolve_authenticated_session(
            context.caller_session_id
        )
        if (
            session_principal_id != caller.principal_id
            or caller_domain.domain_id != context.caller_domain_id
            or caller_domain.boot_epoch != context.caller_boot_epoch
        ):
            raise AuthorityDenied("caller identity or boot epoch changed")
        target_domain = self._kernel.store.get_domain(context.target_domain_id)
        if (
            target_domain is None
            or target_domain.boot_epoch != context.target_boot_epoch
            or target.principal_id not in target_domain.principal_ids
        ):
            raise AuthorityDenied("target identity or boot epoch changed")
        scope = AuthorityScope.from_dict(effect_scope)
        invocation = InvocationContext(
            request_id=context.request_id,
            request_digest=request_digest,
            effect_digest=scope.digest,
            caller_session_id=context.caller_session_id,
            target=target,
            target_domain_id=context.target_domain_id,
            target_boot_epoch=context.target_boot_epoch,
            profile_id=context.profile_id,
            activation_id=context.activation_id,
            activation_digest=context.activation_digest,
            plan_digest=context.plan_digest,
            profile_authority_digest=context.profile_authority_digest,
            fencing_token=context.fencing_token,
            security_epoch=context.security_epoch,
            call_chain=tuple(item.value for item in context.delegation_chain),
        )
        return invocation, scope

    def _resolve_exact(self, reference: OpaqueAuthorityRef) -> FunctionPrincipal:
        principal = self._principals.resolve_principal(reference)
        if reference.value != principal.principal_id:
            raise AuthorityDenied("opaque principal reference is not exact")
        return principal

    @staticmethod
    def _decode_transport(lease: OpaqueInvocationLease) -> str:
        try:
            return lease.token.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthorityDenied("InvocationLease transport is malformed") from exc

    @staticmethod
    def _validate_lease_context(
        lease: InvocationLease,
        context: RequestContext,
        target: FunctionPrincipal,
    ) -> None:
        if (
            lease.request_id != context.request_id
            or lease.target != target
            or lease.caller_domain_id != context.caller_domain_id
            or lease.caller_boot_epoch != context.caller_boot_epoch
            or lease.target_domain_id != context.target_domain_id
            or lease.target_boot_epoch != context.target_boot_epoch
            or lease.profile_id != context.profile_id
            or lease.activation_id != context.activation_id
            or lease.activation_digest != context.activation_digest
            or lease.plan_digest != context.plan_digest
            or lease.profile_authority_digest != context.profile_authority_digest
            or lease.fencing_token != context.fencing_token
            or lease.security_epoch != context.security_epoch
        ):
            raise AuthorityDenied("InvocationLease Host context changed")

    def _require_lease(
        self, reservation: OpaqueAuditReservation
    ) -> tuple[InvocationLease, LeaseState]:
        result = self._kernel.store.get_lease(reservation.value)
        if result is None:
            raise AuthorityDenied("authoritative audit reservation is unknown")
        return result

    def _forget(self, request_id: str) -> None:
        with self._lock:
            self._issued_by_request.pop(request_id, None)


__all__ = [
    "AuthorityV4Adapter",
    "PrincipalReferenceResolver",
    "TriggerAuthorityBinding",
    "TriggerAuthorityResolver",
]
