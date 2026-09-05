"""Narrow integration protocols for Workflow v4."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .models import AuthorityReservation, DispatchAuthority, InvocationOutcome


class ContractCatalogProvider(Protocol):
    """Supply one already-resolved active Contract catalog snapshot."""

    def snapshot(self) -> Mapping[str, Any]:
        """Return catalog digest, SecurityEpoch, and exact operation bindings."""


class AuthorityProvider(Protocol):
    """Reserve and commit atomic attempt-scoped authority."""

    def reserve(self, request: Mapping[str, Any]) -> AuthorityReservation:
        """Reserve authority without beginning a Provider effect."""

    def inspect(self, reservation_id: str) -> AuthorityReservation:
        """Re-evaluate approval, timeout, revocation, and SecurityEpoch."""

    def commit(
        self, reservation_id: str, *, request_digest: str, security_epoch: int
    ) -> DispatchAuthority:
        """Commit a fresh one-dispatch token at the effect boundary."""

    def finish(self, reservation_id: str, *, outcome_digest: str, state: str) -> None:
        """Commit the authoritative outcome or ambiguity."""

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        """Fence any unused authority."""


class ContractInvocationProvider(Protocol):
    """Invoke only an exact Contract Request through the Host broker."""

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
    ) -> InvocationOutcome:
        """Invoke the pinned Function principal with ephemeral authority."""

    def cancel(self, request_id: str) -> None:
        """Propagate cancellation to the in-flight Provider request."""


class InputValidator(Protocol):
    """Validate payloads against catalog-owned schemas without network I/O."""

    def validate(self, schema_digest: str, value: Mapping[str, Any]) -> Sequence[str]:
        """Return deterministic validation errors, or an empty sequence."""
