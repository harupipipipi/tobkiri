"""Authenticate committed independent reviewer provenance in the native store."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityStore,
    LeaseState,
    GrantLifetime,
    authority_digest,
)
from .canonical_reviewer_port import CapturedReviewerBoundary, GENERATE_TARGET


class CommittedReviewerTransportAuthenticator:
    """Read encrypted native records, never trust a callback's proof dictionaries.

    The root captures the exact independent signed reviewer edge, domains and
    Grant before native approval. Its digest is the approved reviewer boundary's
    `authority_capture_digest`. None of this private capture goes to the model.
    The production binder must supply the actual committed generate lease ID.
    """

    def __init__(
        self,
        store: AuthorityStore,
        boundary: CapturedReviewerBoundary,
        independent_capture: Mapping[str, Any],
        assert_current: Callable[[], None],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(store, AuthorityStore):
            raise TypeError("reviewer transport requires the actual native AuthorityStore")
        self._store = store
        self._boundary = boundary
        self._capture = dict(independent_capture)
        self._guard = assert_current
        self._clock = clock

    def __call__(self, provenance: Mapping[str, Any]) -> None:
        """Verify exact committed generate authority under the approved capture."""
        self._guard()
        facts = self._boundary.snapshot()
        if authority_digest(self._capture) != facts["authority_capture_digest"]:
            raise AuthorityDenied("reviewer independent edge capture changed")
        proof = provenance.get("transport_proof")
        if not isinstance(proof, Mapping):
            raise AuthorityDenied("reviewer native transport proof missing")
        stored = self._store.get_lease(str(proof.get("lease_id") or ""))
        if stored is None or stored[1] is not LeaseState.COMMITTED:
            raise AuthorityDenied("reviewer generate lease is not committed")
        lease = stored[0]
        if (
            lease.request_digest != proof.get("request_digest")
            or provenance.get("transport_request_digest") != lease.request_digest
            or provenance.get("reviewer_boundary_digest") != self._boundary.boundary_digest
            or lease.caller.principal_id != facts["caller_principal_id"]
            or lease.profile_id != facts["profile_id"]
            or lease.expires_at <= self._clock()
        ):
            raise AuthorityDenied("reviewer committed lease is expired or rebound")
        snapshot = proof.get("reviewer_prepared_snapshot")
        if snapshot is not None:
            payload = snapshot.get("normalized_payload") if isinstance(snapshot, Mapping) else None
            if (
                not isinstance(payload, Mapping)
                or snapshot.get("request_digest") != lease.request_digest
                or snapshot.get("contract_id") != GENERATE_TARGET[0]
                or snapshot.get("operation_id") != GENERATE_TARGET[1]
                or payload.get("model_profile_id") != facts["model_reference"]
                or payload.get("route_binding") != facts["route_binding"]
                or payload.get("tools") != []
                or payload.get("allow_failover") is not False
            ):
                raise AuthorityDenied("reviewer prepared transport snapshot rebound")
        fields = {
            "caller_principal_id": lease.caller.principal_id,
            "target_principal_id": lease.target.principal_id,
            **{
                key: getattr(lease, key)
                for key in (
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
        stable = {
            "caller_principal_id",
            "target_principal_id",
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
        }
        if (
            not stable <= set(self._capture)
            or set(self._capture) - set(fields)
            or any(fields[k] != v for k, v in self._capture.items())
        ):
            raise AuthorityDenied("reviewer lease differs from independent signed edge capture")
        dimensions = lease.authorized_scope.dimensions
        required = {
            "contract": (GENERATE_TARGET[0],),
            "operation": (GENERATE_TARGET[1],),
            "request_surface": ("approval-review",),
            "tool_calling": ("false",),
            "allow_failover": ("false",),
        }
        if any(tuple(dimensions.get(k, ())) != v for k, v in required.items()):
            raise AuthorityDenied("reviewer lease scope does not enforce isolated review")
        grant = self._store.get_grant(lease.grant_id)
        if (
            grant is None
            or grant.revoked
            or self._store.is_revoked("grant", lease.grant_id)
            or self._store.is_revoked("provider_authority", lease.provider_authority_id)
            or grant.caller != lease.caller
            or grant.target != lease.target
            or grant.security_epoch != lease.security_epoch
            or grant.issued_at > self._clock()
            or grant.profile_id != lease.profile_id
            or grant.activation_id != lease.activation_id
            or grant.profile_authority_digest != lease.profile_authority_digest
            or not lease.authorized_scope.is_subset_of(grant.scope)
            or (grant.expires_at is not None and grant.expires_at <= self._clock())
        ):
            raise AuthorityDenied("reviewer independent Grant is missing or revoked")
        if grant.lifetime is GrantLifetime.ONE_SHOT and self._store.grant_usage(grant.grant_id) != (
            0,
            1,
        ):
            raise AuthorityDenied("reviewer one-shot Grant usage is not exactly committed")
        provider = self._store.get_provider_authority(lease.provider_authority_id)
        if (
            provider is None
            or provider.revoked
            or provider.digest != lease.provider_authority_digest
            or provider.security_epoch != lease.security_epoch
            or provider.provider != lease.target
            or provider.execution_domain_id != lease.target_domain_id
            or provider.publisher_lineage != lease.target_publisher_lineage
            or provider.host_extension_id != lease.host_extension_id
            or provider.valid_from > self._clock()
            or (provider.expires_at is not None and provider.expires_at <= self._clock())
            or not lease.authorized_scope.is_subset_of(provider.scope)
        ):
            raise AuthorityDenied("reviewer current Provider authority is unavailable")
        if provider.host_extension_id != "runtime-tcb":
            trust = self._store.get_host_extension_trust(provider.host_extension_id)
            if (
                trust is None
                or trust.revoked
                or trust.security_epoch != lease.security_epoch
                or trust.parent_artifact_digest != lease.target.parent_artifact_digest
                or trust.publisher_lineage != lease.target_publisher_lineage
                or lease.target.principal_id not in trust.provider_principal_ids
                or trust.valid_from > self._clock()
                or (trust.expires_at is not None and trust.expires_at <= self._clock())
                or self._store.is_revoked("host_extension", trust.trust_id)
            ):
                raise AuthorityDenied("reviewer Host Extension trust is unavailable")
        revocation_targets = (
            ("profile", lease.profile_id),
            ("activation", lease.activation_id),
            ("function_principal", lease.caller.principal_id),
            ("function_principal", lease.target.principal_id),
            ("pack_artifact", lease.caller.parent_artifact_digest),
            ("pack_artifact", lease.target.parent_artifact_digest),
            ("publisher", lease.caller_publisher_lineage),
            ("publisher", lease.target_publisher_lineage),
            ("execution_domain", lease.caller_domain_id),
            ("execution_domain", lease.target_domain_id),
        )
        if any(self._store.is_revoked(kind, value) for kind, value in revocation_targets):
            raise AuthorityDenied("reviewer authority was revoked")
        activation = self._store.active_activation_reservation(lease.activation_id)
        if (
            self._store.security_epoch != lease.security_epoch
            or activation is None
            or any(
                activation.get(k) != getattr(lease, k)
                for k in (
                    "profile_id",
                    "plan_digest",
                    "profile_authority_digest",
                    "security_epoch",
                    "fencing_token",
                )
            )
        ):
            raise AuthorityDenied("reviewer active profile ledger changed or missing")
        for kind, identifier in (
            ("caller", lease.caller_domain_id),
            ("target", lease.target_domain_id),
        ):
            domain = self._store.get_domain(identifier)
            principal = lease.caller if kind == "caller" else lease.target
            boot = lease.caller_boot_epoch if kind == "caller" else lease.target_boot_epoch
            if (
                domain is None
                or domain.state.value != "active"
                or domain.boot_epoch != boot
                or principal not in domain.principals
                or domain.profile_id != lease.profile_id
                or domain.activation_id != lease.activation_id
                or domain.security_epoch != lease.security_epoch
                or domain.fencing_token != lease.fencing_token
            ):
                raise AuthorityDenied("reviewer authenticated execution domain changed")
        target_domain = self._store.get_domain(lease.target_domain_id)
        if (
            target_domain is None
            or target_domain.identity_digest != provider.execution_domain_identity_digest
        ):
            raise AuthorityDenied("reviewer Provider domain identity changed")
        self._guard()
        self._boundary.snapshot()
