"""Narrow integration ports owned by the authority and audit security core.

The host execution package intentionally does not define Grant, Lease, principal,
or execution-domain semantics. Implementations adapt these opaque DTOs to the
canonical types in ``core_runtime.authority``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .contracts import ResolvedOperationBinding
from .models import OpaqueAuthorityRef, RequestContext, RuntimeEvidence


@dataclass(frozen=True)
class StaticAuthorityQuery:
    """Data-only query used before queueing or materialization."""

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    request_digest: str
    effect_scope: Mapping[str, Any]


@dataclass(frozen=True)
class FinalAuthorizationQuery:
    """Runtime-evidence-bound query used after materialization."""

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    request_digest: str
    effect_scope: Mapping[str, Any]
    evidence: RuntimeEvidence


@dataclass(frozen=True)
class OpaqueInvocationLease:
    """Non-inspectable, request-bound lease transport DTO."""

    token: bytes

    def __post_init__(self) -> None:
        if not self.token or len(self.token) > 4096:
            raise ValueError("InvocationLease token must be non-empty and bounded")


class AuthorityPort(Protocol):
    """Adapter contract expected from ``core_runtime.authority``.

    Exact required methods:

    * ``check_static_path`` checks epoch/revocation and the existence of a
      potentially matching authority path without issuing authority.
    * ``authorize_and_issue_lease`` validates runtime evidence and returns one
      request-bound opaque lease, or raises on any unknown/mismatch/failure.
    * ``recheck_effect_boundary`` rechecks epoch, revocation, domain, and lease
      immediately before an effect boundary.
    * ``fence_request`` revokes request leases/handles after cancel or timeout.
    * ``issue_trigger_lease`` returns a trigger-occurrence-specific one-shot
      lease after checking epoch and revocation.
    """

    def check_static_path(self, query: StaticAuthorityQuery) -> None:
        """Fail closed unless a potential authority path exists."""

    def authorize_and_issue_lease(
        self,
        query: FinalAuthorizationQuery,
    ) -> OpaqueInvocationLease:
        """Return a request-bound lease after complete final authorization."""

    def recheck_effect_boundary(
        self,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        lease: OpaqueInvocationLease,
    ) -> None:
        """Fail closed on epoch, revocation, domain, or lease mismatch."""

    def fence_request(self, request_id: str) -> None:
        """Revoke all request-bound authority after cancel or timeout."""

    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        """Issue one trigger-specific, one-shot lease."""


@dataclass(frozen=True)
class OpaqueAuditReservation:
    """Opaque authoritative audit reservation reference."""

    value: str


class AuditPort(Protocol):
    """Fail-closed authoritative audit interface expected by the broker."""

    def reserve_effect(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        request_digest: str,
    ) -> OpaqueAuditReservation:
        """Durably reserve an effect event before dispatch."""

    def mark_dispatched(self, reservation: OpaqueAuditReservation) -> None:
        """Durably record provider dispatch."""

    def commit_effect(
        self,
        reservation: OpaqueAuditReservation,
        outcome_digest: str,
    ) -> None:
        """Durably commit a completed effect."""

    def fail_effect(
        self,
        reservation: OpaqueAuditReservation,
        stable_code: str,
        ambiguous: bool,
    ) -> None:
        """Durably record failure or uncertainty without provider strings."""
