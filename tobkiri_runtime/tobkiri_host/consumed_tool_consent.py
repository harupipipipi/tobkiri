"""Host-only current proof of a successfully consumed native consent Grant."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from core_runtime.authority.v4 import AuthorityScope, GrantLifetime, LeaseState
from tobkiri_host.models import RequestContext
from tobkiri_host.ports import InteractiveApprovalRequestCommand


@dataclass(frozen=True)
class ConsumedToolConsent:
    """Exact private consent evidence; no token or executable authority material."""

    command: InteractiveApprovalRequestCommand
    execution_context: RequestContext
    lease_id: str
    retained_operation_digest: str


class ConsumedConsentVerifier:
    """Recheck consumed native proof through authenticated actual store records."""

    def __init__(self, authority_store: Any, *, clock: Callable[[], float] = time.time) -> None:
        self._store, self._clock = authority_store, clock

    def assert_current(self, proof: ConsumedToolConsent) -> None:
        """Require native approval, committed exact consent and live provenance."""
        command, context = proof.command, proof.execution_context
        request = self._store.get_interactive_approval_request(command.context.request_id)
        decision = self._store.get_interactive_approval_decision(command.context.request_id)
        leased = self._store.get_lease(proof.lease_id)
        if (
            request is None
            or decision is None
            or leased is None
            or decision.decision != "approved"
            or not decision.ui_operator_digest
            or decision.request_snapshot_digest != request.digest
        ):
            raise PermissionError("native consumed consent proof is unavailable")
        approval = self._store.get_approval(decision.approval_id or "")
        grant = self._store.get_grant(decision.grant_id or "")
        lease, state = leased
        scope = AuthorityScope.from_dict(command.base_scope)
        if (
            approval is None
            or grant is None
            or state is not LeaseState.COMMITTED
            or request.expires_at <= self._clock()
            or request.security_epoch != self._store.security_epoch
        ):
            raise PermissionError("native consumed consent is expired or uncommitted")
        if (
            request.request_digest != command.request_digest
            or request.base_scope != scope
            or request.invocation_owner_id != command.invocation_owner_id
            or request.presentation_owner_principal_id != command.presentation_owner_principal_id
            or request.presentation_owner_session_id != command.presentation_owner_session_id
            or request.expires_at != command.expires_at
            or request.redacted_metadata != command.redacted_metadata
        ):
            raise PermissionError("native consumed consent binding changed")
        if (
            grant.revoked
            or grant.lifetime is not GrantLifetime.ONE_SHOT
            or grant.max_uses != 1
            or grant.session_id != context.caller_session_id
            or grant.issued_at != decision.decided_at
            or grant.caller_publisher_lineage != request.caller_publisher_lineage
            or grant.target_publisher_lineage != request.target_publisher_lineage
            or lease.caller_publisher_lineage != request.caller_publisher_lineage
            or lease.target_publisher_lineage != request.target_publisher_lineage
            or self._store.grant_usage(grant.grant_id) != (0, 1)
            or grant.scope != scope
            or grant.approval_id != approval.approval_id
            or grant.expires_at != request.expires_at
            or lease.grant_id != grant.grant_id
            or lease.request_id != context.request_id
            or lease.request_digest != command.request_digest
            or lease.authorized_scope != scope
            or lease.target.principal_id != command.target_principal.value
            or lease.caller.principal_id != context.caller_principal.value
            or request.caller != lease.caller
            or request.target != lease.target
        ):
            raise PermissionError("native consumed consent execution changed")
        if (
            approval.snapshot_digest != request.digest
            or approval.effect_bundle_digest != scope.digest
            or approval.decision != "approved"
            or approval.actor_id != decision.actor_id
            or approval.decided_at != decision.decided_at
            or approval.caller != request.caller
            or approval.target != request.target
            or grant.caller != request.caller
            or grant.target != request.target
        ):
            raise PermissionError("native consumed consent approval changed")
        if (
            scope.dimensions.get("retained_operation_digest") != (proof.retained_operation_digest,)
            or command.redacted_metadata.get("retained_operation_digest")
            != proof.retained_operation_digest
        ):
            raise PermissionError("native consumed consent retained operation changed")
        for field in (
            "profile_id",
            "activation_id",
            "activation_digest",
            "plan_digest",
            "profile_authority_digest",
            "security_epoch",
            "fencing_token",
            "caller_domain_id",
            "caller_boot_epoch",
            "target_domain_id",
            "target_boot_epoch",
        ):
            if getattr(lease, field) != getattr(context, field) or getattr(
                request, field
            ) != getattr(context, field):
                raise PermissionError("native consumed consent capture changed")
        if (
            request.profile_revision != context.profile_revision
            or request.profile_revision != command.context.profile_revision
            or request.caller_session_id != context.caller_session_id
            or request.target_backend_digest != context.target_backend_digest
            or request.handle_namespace != context.handle_namespace
        ):
            raise PermissionError("native consumed consent context changed")
        for field in (
            "profile_id",
            "activation_id",
            "profile_authority_digest",
            "security_epoch",
        ):
            if getattr(grant, field) != getattr(request, field):
                raise PermissionError("native consumed consent grant capture changed")
        if (
            approval.profile_id != request.profile_id
            or approval.security_epoch != request.security_epoch
        ):
            raise PermissionError("native consumed consent approval capture changed")
        provider = self._store.get_provider_authority(lease.provider_authority_id)
        trust = self._store.get_host_extension_trust(lease.host_extension_id)
        now = self._clock()
        if (
            provider is None
            or provider.revoked
            or provider.provider != lease.target
            or provider.digest != lease.provider_authority_digest
            or provider.execution_domain_id != context.target_domain_id
            or provider.host_extension_id != lease.host_extension_id
            or provider.publisher_lineage != lease.target_publisher_lineage
            or provider.security_epoch != lease.security_epoch
            or provider.valid_from > now
            or (provider.expires_at is not None and provider.expires_at <= now)
            or not scope.is_subset_of(provider.scope)
        ):
            raise PermissionError("native consumed consent provider is unavailable")
        if (
            trust is None
            or trust.trust_id != provider.host_extension_id
            or trust.trust_id != lease.host_extension_id
            or trust.revoked
            or trust.security_epoch != lease.security_epoch
            or trust.parent_artifact_digest != lease.target.parent_artifact_digest
            or trust.publisher_lineage != lease.target_publisher_lineage
            or trust.valid_from > now
            or (trust.expires_at is not None and trust.expires_at <= now)
            or lease.target.principal_id not in trust.provider_principal_ids
        ):
            raise PermissionError("native consumed consent trust is unavailable")
        for kind, identifier in (
            ("grant", grant.grant_id),
            ("approval", approval.approval_id),
            ("profile", request.profile_id),
            ("activation", request.activation_id),
            ("function_principal", lease.caller.principal_id),
            ("function_principal", lease.target.principal_id),
            ("execution_domain", lease.caller_domain_id),
            ("execution_domain", lease.target_domain_id),
            ("provider_authority", lease.provider_authority_id),
            ("host_extension", lease.host_extension_id),
            ("pack_artifact", lease.caller.parent_artifact_digest),
            ("pack_artifact", lease.target.parent_artifact_digest),
            ("publisher", lease.caller_publisher_lineage),
            ("publisher", lease.target_publisher_lineage),
        ):
            if self._store.is_revoked(kind, identifier):
                raise PermissionError("native consumed consent was revoked")
        for session, principal, domain_id, boot_epoch in (
            (
                context.caller_session_id,
                context.caller_principal.value,
                context.caller_domain_id,
                context.caller_boot_epoch,
            ),
            (
                request.presentation_owner_session_id,
                request.presentation_owner_principal_id,
                None,
                None,
            ),
        ):
            domain, actual = self._store.resolve_authenticated_session(session)
            if (
                actual != principal
                or domain.state.value != "active"
                or domain.profile_id != context.profile_id
                or domain.activation_id != context.activation_id
                or domain.security_epoch != context.security_epoch
                or domain.fencing_token != context.fencing_token
                or (domain_id is not None and domain.domain_id != domain_id)
                or (boot_epoch is not None and domain.boot_epoch != boot_epoch)
            ):
                raise PermissionError("native consumed consent owner is stale")
        target = self._store.get_domain(context.target_domain_id)
        if (
            target is None
            or target.state.value != "active"
            or target.boot_epoch != context.target_boot_epoch
            or target.identity_digest != provider.execution_domain_identity_digest
            or target.profile_id != context.profile_id
            or target.activation_id != context.activation_id
            or target.security_epoch != context.security_epoch
            or target.fencing_token != context.fencing_token
            or lease.target.principal_id not in target.principal_ids
        ):
            raise PermissionError("native consumed consent target is stale")
        if request.expires_at <= self._clock():
            raise PermissionError("native consumed consent expired during verification")
