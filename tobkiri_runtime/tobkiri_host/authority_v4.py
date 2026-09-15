"""Production-shaped adapter for the canonical ADR-014 authority kernel.

This module is the only bridge between the Pack v4 host DTOs and
``core_runtime.authority.v4``.  It never consults legacy authority services and
never derives a Function principal from Pack-supplied invocation fields.
"""

from __future__ import annotations

import hmac
import secrets
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
    GrantLifetime,
    GrantRecord,
    HostExtensionTrustRecord,
    InteractiveApprovalDecision,
    InteractiveApprovalRequest,
    InvocationContext,
    InvocationLease,
    LeaseState,
    ProviderAuthorityRecord,
    authority_digest,
    interactive_confirmation_digest,
)
from core_runtime.authority.ui_operator import (
    ui_operator_audit_record,
    verify_interactive_ui_operator,
)

from .contracts import ResolvedOperationBinding
from .models import OpaqueAuthorityRef, RequestContext
from .ports import (
    FinalAuthorizationQuery,
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGrantAttestation,
    InteractiveApprovalGetQuery,
    InteractiveApprovalListQuery,
    InteractiveApprovalRequestCommand,
    InteractiveApprovalStatus,
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

    def commit_provider_authority_bundle(
        self,
        *,
        provider_authorities: tuple[ProviderAuthorityRecord, ...],
        host_extension_trust: HostExtensionTrustRecord | None = None,
    ) -> None:
        """Commit Host Provider reachability without issuing a caller Grant."""

        self._kernel.commit_provider_authority_bundle(
            provider_authorities=provider_authorities,
            host_extension_trust=host_extension_trust,
        )

    def request_interactive_approval(
        self,
        command: InteractiveApprovalRequestCommand,
    ) -> InteractiveApprovalStatus:
        """Open a redacted, Host-bound approval request without authority."""

        context = command.context
        self._validate_interactive_confirmation_display(command)
        request = self._kernel.open_interactive_approval(
            request_id=context.request_id,
            request_digest=command.request_digest,
            caller=self._resolve_exact(context.caller_principal),
            target=self._resolve_exact(command.target_principal),
            profile_id=context.profile_id,
            activation_id=context.activation_id,
            activation_digest=context.activation_digest,
            plan_digest=context.plan_digest,
            profile_authority_digest=context.profile_authority_digest,
            profile_revision=context.profile_revision,
            security_epoch=context.security_epoch,
            fencing_token=context.fencing_token,
            caller_domain_id=context.caller_domain_id,
            caller_boot_epoch=context.caller_boot_epoch,
            target_domain_id=context.target_domain_id,
            target_boot_epoch=context.target_boot_epoch,
            target_backend_digest=context.target_backend_digest,
            handle_namespace=context.handle_namespace,
            base_scope=AuthorityScope.from_dict(command.base_scope),
            invocation_owner_id=command.invocation_owner_id,
            presentation_owner_principal_id=(command.presentation_owner_principal_id),
            presentation_owner_session_id=command.presentation_owner_session_id,
            caller_session_id=context.caller_session_id,
            caller_publisher_lineage=command.caller_publisher_lineage,
            target_publisher_lineage=command.target_publisher_lineage,
            expires_at=command.expires_at,
            redacted_metadata=dict(command.redacted_metadata),
            typed_confirmation_digest=(
                interactive_confirmation_digest(command.typed_confirmation_phrase)
                if command.typed_confirmation_phrase is not None
                else None
            ),
        )
        return self._interactive_status(request, "pending")

    def approve_interactive_approval(
        self,
        command: InteractiveApprovalDecisionCommand,
    ) -> InteractiveApprovalStatus:
        """Approve once after Host-side phrase and UI-provenance verification."""

        request, state = self._interactive_approval_for_presentation(
            command.context,
            command.request_id,
        )
        if state != "pending":
            raise AuthorityDenied("interactive approval is unavailable")
        ui_operator_digest = self._verified_interactive_operator_audit_digest(
            command,
            request,
            action="approve",
        )
        confirmed = self._verify_typed_confirmation(command, request)
        decided_at = self._kernel.interactive_approval_now()
        approval_id = "interactive-approval-" + secrets.token_urlsafe(18)
        grant_id = "interactive-grant-" + secrets.token_urlsafe(18)
        decision = InteractiveApprovalDecision(
            decision_id=request.request_id,
            request_id=request.request_id,
            request_snapshot_digest=request.digest,
            decision="approved",
            actor_id=command.actor_id,
            decided_at=decided_at,
            security_epoch=request.security_epoch,
            ui_operator_digest=ui_operator_digest,
            typed_confirmation_verified=confirmed,
            approval_id=approval_id,
            grant_id=grant_id,
        )
        approval = ApprovalRecord(
            approval_id=approval_id,
            snapshot_digest=request.digest,
            actor_id=command.actor_id,
            decision="approved",
            decided_at=decided_at,
            caller=request.caller,
            target=request.target,
            profile_id=request.profile_id,
            effect_bundle_digest=request.base_scope.digest,
            security_epoch=request.security_epoch,
        )
        grant = GrantRecord(
            grant_id=grant_id,
            caller=request.caller,
            target=request.target,
            profile_id=request.profile_id,
            activation_id=request.activation_id,
            profile_authority_digest=request.profile_authority_digest,
            caller_publisher_lineage=request.caller_publisher_lineage,
            target_publisher_lineage=request.target_publisher_lineage,
            scope=request.base_scope,
            lifetime=GrantLifetime.ONE_SHOT,
            security_epoch=request.security_epoch,
            approval_id=approval_id,
            issued_at=decided_at,
            expires_at=request.expires_at,
            max_uses=1,
            session_id=request.caller_session_id,
        )
        self._kernel.settle_interactive_approval(
            decision,
            approval=approval,
            grant=grant,
            confirmation_text=command.confirmation_text,
        )
        return self._interactive_status(request, "approved")

    def interactive_approval_status(
        self,
        request_id: str,
    ) -> InteractiveApprovalStatus:
        """Return the narrow, redacted lifecycle view for one Host request."""

        request, state = self._kernel.interactive_approval(request_id)
        return self._interactive_status(request, state)

    def get_interactive_approval(
        self,
        query: InteractiveApprovalGetQuery,
    ) -> InteractiveApprovalStatus:
        """Return one redacted approval visible to its authenticated presenter."""

        request, state = self._interactive_approval_for_presentation(
            query.context,
            query.request_id,
        )
        return self._interactive_status(request, state)

    def list_interactive_approvals(
        self,
        query: InteractiveApprovalListQuery,
    ) -> tuple[InteractiveApprovalStatus, ...]:
        """Return only redacted approvals owned by this exact presentation view."""

        if query.state not in {None, "pending", "approved", "denied", "expired"}:
            raise AuthorityDenied("interactive approval query is unavailable")
        statuses: list[InteractiveApprovalStatus] = []
        for request in self._kernel.store.list_interactive_approval_requests():
            try:
                self._validate_interactive_presentation_context(query.context, request)
                _request, state = self._kernel.interactive_approval(request.request_id)
            except Exception:
                continue
            if query.state is None or state == query.state:
                statuses.append(self._interactive_status(request, state))
        return tuple(sorted(statuses, key=lambda status: status.request_id))

    def assert_interactive_approval_grant(
        self,
        attestation: InteractiveApprovalGrantAttestation,
    ) -> None:
        """Prove an approved one-shot Grant matches one exact future effect.

        This is a Host-only assertion.  It deliberately returns no Grant ID,
        token, receipt, lease, or payload; callers can only advance their
        durable PendingEffect state after this full equality check succeeds.
        """

        request, state = self._kernel.interactive_approval(attestation.request_id)
        context = attestation.context
        expected_scope = AuthorityScope.from_dict(attestation.base_scope)
        caller = self._resolve_exact(context.caller_principal)
        target = self._resolve_exact(attestation.target_principal)
        if (
            state != "approved"
            or request.request_id != attestation.request_id
            or request.request_digest != attestation.request_digest
            or request.caller != caller
            or request.target != target
            or request.profile_id != context.profile_id
            or request.activation_id != context.activation_id
            or request.activation_digest != context.activation_digest
            or request.plan_digest != context.plan_digest
            or request.profile_authority_digest != context.profile_authority_digest
            or request.profile_revision != context.profile_revision
            or request.security_epoch != context.security_epoch
            or request.fencing_token != context.fencing_token
            or request.caller_domain_id != context.caller_domain_id
            or request.caller_boot_epoch != context.caller_boot_epoch
            or request.target_domain_id != context.target_domain_id
            or request.target_boot_epoch != context.target_boot_epoch
            or request.target_backend_digest != context.target_backend_digest
            or request.handle_namespace != context.handle_namespace
            or request.base_scope != expected_scope
            or request.invocation_owner_id != attestation.invocation_owner_id
            or request.caller_publisher_lineage != attestation.caller_publisher_lineage
            or request.target_publisher_lineage != attestation.target_publisher_lineage
            or request.expires_at != attestation.expires_at
        ):
            raise AuthorityDenied("interactive approval authority is unavailable")
        self._kernel.assert_interactive_approval_grant(attestation.request_id)

    def create_host_pending_effect(
        self,
        effect_id: str,
        payload: Mapping[str, object],
    ) -> int:
        """Persist a Host-only encrypted PendingEffect snapshot."""

        return self._kernel.store.create_host_pending_effect(effect_id, payload)

    def get_host_pending_effect(
        self,
        effect_id: str,
    ) -> tuple[int, Mapping[str, object]] | None:
        """Load an authenticated PendingEffect snapshot for Host recovery only."""

        result = self._kernel.store.get_host_pending_effect(effect_id)
        if result is None:
            return None
        revision, payload = result
        return revision, payload

    def compare_and_swap_host_pending_effect(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        payload: Mapping[str, object],
    ) -> int:
        """Advance one Host-only PendingEffect snapshot by revision CAS."""

        return self._kernel.store.compare_and_swap_host_pending_effect(
            effect_id,
            expected_revision=expected_revision,
            payload=payload,
        )

    def list_host_pending_effects(self) -> list[tuple[int, Mapping[str, object]]]:
        """Return encrypted Host-only snapshots for crash recovery."""

        records: list[tuple[int, Mapping[str, object]]] = [
            (revision, payload)
            for revision, payload in self._kernel.store.list_host_pending_effects()
        ]
        return records

    def deny_interactive_approval(
        self,
        command: InteractiveApprovalDecisionCommand,
    ) -> InteractiveApprovalStatus:
        """Deny once after verifying the signed authority-window provenance."""

        request, state = self._interactive_approval_for_presentation(
            command.context,
            command.request_id,
        )
        if state != "pending":
            raise AuthorityDenied("interactive approval is unavailable")
        decision = InteractiveApprovalDecision(
            decision_id=request.request_id,
            request_id=request.request_id,
            request_snapshot_digest=request.digest,
            decision="denied",
            actor_id=command.actor_id,
            decided_at=self._kernel.interactive_approval_now(),
            security_epoch=request.security_epoch,
            ui_operator_digest=self._verified_interactive_operator_audit_digest(
                command,
                request,
                action="deny",
            ),
            typed_confirmation_verified=False,
        )
        self._kernel.settle_interactive_approval(decision)
        return self._interactive_status(request, "denied")

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
            or query.evidence.executable_digest
            != context.target.function_implementation_digest
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
            or issued.target_principal_id
            != self._resolve_exact(binding.principal_ref).principal_id
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
        caller_domain, session_principal_id = (
            self._kernel.store.resolve_authenticated_session(context.caller_session_id)
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

    def _validate_interactive_presentation_context(
        self,
        context: RequestContext,
        request: InteractiveApprovalRequest,
    ) -> None:
        """Bind UI reads and decisions to the durable presentation owner tuple."""

        presenter = self._resolve_exact(context.caller_principal)
        owner_domain, owner_principal_id = (
            self._kernel.store.resolve_authenticated_session(context.caller_session_id)
        )
        if (
            presenter.principal_id != request.presentation_owner_principal_id
            or owner_principal_id != request.presentation_owner_principal_id
            or context.caller_session_id != request.presentation_owner_session_id
            or owner_domain.domain_id != context.caller_domain_id
            or owner_domain.boot_epoch != context.caller_boot_epoch
            or owner_domain.profile_id != request.profile_id
            or owner_domain.activation_id != request.activation_id
            or owner_domain.security_epoch != request.security_epoch
            or owner_domain.fencing_token != request.fencing_token
            or context.profile_id != request.profile_id
            or context.activation_id != request.activation_id
            or context.activation_digest != request.activation_digest
            or context.plan_digest != request.plan_digest
            or context.profile_authority_digest != request.profile_authority_digest
            or context.profile_revision != request.profile_revision
            or context.security_epoch != request.security_epoch
            or context.fencing_token != request.fencing_token
        ):
            raise AuthorityDenied("interactive approval is unavailable")

    def _interactive_approval_for_presentation(
        self,
        context: RequestContext,
        request_id: str,
    ) -> tuple[InteractiveApprovalRequest, str]:
        """Load one approval without revealing whether it is missing or foreign."""

        try:
            request, state = self._kernel.interactive_approval(request_id)
            self._validate_interactive_presentation_context(context, request)
            return request, state
        except Exception as exc:
            raise AuthorityDenied("interactive approval is unavailable") from exc

    @staticmethod
    def _validate_interactive_confirmation_display(
        command: InteractiveApprovalRequestCommand,
    ) -> None:
        """Require the exact non-secret phrase displayed by the approval UI."""

        phrase = command.typed_confirmation_phrase
        if phrase is None:
            return
        displayed = command.redacted_metadata.get("confirmation_phrase")
        if not isinstance(displayed, str) or not hmac.compare_digest(displayed, phrase):
            raise AuthorityDenied("interactive confirmation display is unavailable")

    @staticmethod
    def _interactive_status(
        request: InteractiveApprovalRequest,
        state: str,
    ) -> InteractiveApprovalStatus:
        """Project a request into the deliberately secret-free port response."""

        return InteractiveApprovalStatus(
            request_id=request.request_id,
            state=state,
            expires_at=request.expires_at,
            typed_confirmation_required=request.typed_confirmation_digest is not None,
            request_snapshot_digest=_interactive_ui_operator_digest(request.digest),
            typed_confirmation_digest=(
                _interactive_ui_operator_digest(request.typed_confirmation_digest)
                if request.typed_confirmation_digest is not None
                else None
            ),
            redacted_metadata=dict(request.redacted_metadata),
        )

    @staticmethod
    def _verify_typed_confirmation(
        command: InteractiveApprovalDecisionCommand,
        request: InteractiveApprovalRequest,
    ) -> bool:
        """Check an actual phrase; a payload boolean is never trusted proof."""

        if request.typed_confirmation_digest is None:
            return False
        if (
            not isinstance(command.confirmation_text, str)
            or not command.confirmation_text
        ):
            raise AuthorityDenied("typed confirmation does not match")
        supplied = interactive_confirmation_digest(command.confirmation_text)
        if not hmac.compare_digest(supplied, request.typed_confirmation_digest):
            raise AuthorityDenied("typed confirmation does not match")
        return True

    @staticmethod
    def _verified_interactive_operator_audit_digest(
        command: InteractiveApprovalDecisionCommand,
        request: InteractiveApprovalRequest,
        *,
        action: str,
    ) -> str:
        """Verify a v3 proof bound to this exact immutable Host decision."""

        if not isinstance(command.ui_operator, Mapping):
            raise AuthorityDenied("interactive ui_operator is required")
        expected_confirmation_digest = (
            _interactive_ui_operator_digest(request.typed_confirmation_digest)
            if action == "approve" and request.typed_confirmation_digest is not None
            else None
        )
        verified, reason, payload = verify_interactive_ui_operator(
            dict(command.ui_operator),
            request_id=request.request_id,
            decision=action,
            request_snapshot_digest=_interactive_ui_operator_digest(request.digest),
            typed_confirmation_digest=expected_confirmation_digest,
        )
        if not verified:
            raise AuthorityDenied(f"interactive ui_operator is invalid: {reason}")
        browser_actor = payload.get("principal_id")
        if browser_actor is not None and browser_actor != command.actor_id:
            raise AuthorityDenied("interactive ui_operator actor does not match")
        return authority_digest(
            {
                "action": action,
                "request_id": request.request_id,
                "request_snapshot_digest": request.digest,
                "ui_operator": ui_operator_audit_record(payload),
            }
        )

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


def _interactive_ui_operator_digest(value: str) -> str:
    """Convert a canonical tagged digest to the v3 wire's exact hex form."""

    prefix = "sha256:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise AuthorityDenied("interactive approval digest is unavailable")
    untagged = value.removeprefix(prefix)
    if len(untagged) != 64 or any(
        character not in "0123456789abcdef" for character in untagged
    ):
        raise AuthorityDenied("interactive approval digest is unavailable")
    return untagged


__all__ = [
    "AuthorityV4Adapter",
    "PrincipalReferenceResolver",
    "TriggerAuthorityBinding",
    "TriggerAuthorityResolver",
]
