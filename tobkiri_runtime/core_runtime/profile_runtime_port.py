"""Application-owned Profile runtime primitives required by Host capture.

The Host owns the authority, pointer, and filesystem checks around Profile
activation.  The concrete Profile catalog and activation envelope format are
application-pack concerns, so they enter the Host only through this explicit
composition port.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


class ProfileRuntimeUnavailable(RuntimeError):
    """Raised when an application did not provide Profile runtime composition."""


class ProfileRuntimeAlreadyConfigured(RuntimeError):
    """Raised when a second application tries to replace Profile composition."""


@dataclass(frozen=True)
class SetupActivationDecision:
    """An application-owned setup request either accepts or rejects activation.

    The Host never interprets a Pack's operation identifier, request fields, or
    presentation payload.  It receives only a confirmation to commit through
    its existing atomic activation ceremony, or an already-sanitized response
    to return without writing state.
    """

    confirmation: Mapping[str, Any] | None = None
    response: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Require exactly one of an accepted confirmation or a rejection."""

        if (self.confirmation is None) == (self.response is None):
            raise ValueError("setup activation decision must contain exactly one outcome")


class ProfileRuntimePort(Protocol):
    """Concrete Profile catalog and activation record operations."""

    def bundled_profile_root(self) -> Path:
        """Return this application's sealed Profile bundle root."""

    def host_resource_root(self) -> Path:
        """Return the Host resource root that binds the sealed bundle."""

    def bootstrap_profile_id(self) -> str:
        """Return the application-owned Profile used by first-run setup."""

    def bootstrap_confirmation(
        self,
        *,
        resolved: Any,
        profile_id: str,
        authority_snapshot_digest: str,
        security_epoch: int,
    ) -> Mapping[str, Any]:
        """Project the application bootstrap confirmation from Host facts."""

    def profile_confirmation(
        self,
        *,
        resolved: Any,
        profile_id: str,
        authority_snapshot_digest: str,
        security_epoch: int,
    ) -> Mapping[str, Any]:
        """Project an application-selected Profile confirmation from Host facts."""

    def setup_listing(
        self,
        catalog: Any,
        confirmation: Mapping[str, Any],
        *,
        active: bool,
        activation_denied: bool,
        denial_diagnostic: str | None,
    ) -> Mapping[str, Any]:
        """Build and validate this application's complete setup presentation."""

    def setup_preview(self, listing: Mapping[str, Any]) -> Mapping[str, Any]:
        """Extract this application's preview from its setup presentation."""

    def setup_activation_decision(
        self,
        body: Mapping[str, Any],
        listing: Mapping[str, Any] | None,
    ) -> SetupActivationDecision:
        """Accept one Pack-owned setup request or return its no-write response."""

    def setup_activation_success(
        self,
        active: Any,
        audit_receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Project an application-owned successful setup response."""

    def setup_activation_failure(self) -> Mapping[str, Any]:
        """Return this application's no-write activation failure response."""

    def retired_setup_response(
        self,
        *,
        route: str | None = None,
    ) -> Mapping[str, Any]:
        """Return this application's retired setup operation response."""

    def load_catalog(self, root: Path) -> Any:
        """Load the sealed application Profile catalog."""

    def catalog_with_packs(self, catalog: Any, packs: Mapping[str, Any]) -> Any:
        """Return a catalog retaining its concrete immutable record type."""

    def catalog_with_profiles(
        self,
        catalog: Any,
        profiles: Mapping[str, Any],
    ) -> Any:
        """Return a catalog with Host-selected Profile definitions only."""

    def resolve_profile(self, catalog: Any, profile_id: str, **kwargs: Any) -> Any:
        """Resolve an application Profile after Host authority preparation."""

    def dynamic_profile_edges(
        self, catalog: Any, profile_id: str, pack_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        """Return application-defined optional edges for the selected closure."""

    def activation_store(self, **kwargs: Any) -> Any:
        """Construct the application activation-envelope store."""

    def denied(self, message: str) -> Exception:
        """Create the application's canonical Profile denial error."""

    def is_reconfirmation_required(self, error: BaseException) -> bool:
        """Identify a retryable application Profile reconfirmation error."""

    def is_resolution_denied(self, error: BaseException) -> bool:
        """Identify an application Profile resolution denial."""

    def active_profile(self, resolved: Any, activation: Mapping[str, Any]) -> Any:
        """Reconstruct one concrete active Profile envelope for cache binding."""


_LOCK = threading.RLock()
_PROFILE_RUNTIME: ProfileRuntimePort | None = None


def register_profile_runtime(runtime: ProfileRuntimePort) -> ProfileRuntimePort:
    """Install one immutable application Profile implementation for this process.

    Repeating the exact same application composition instance is harmless and
    returns that instance.  Any other object is rejected instead of replacing
    already-captured Profile authority.
    """

    with _LOCK:
        global _PROFILE_RUNTIME
        existing = _PROFILE_RUNTIME
        if existing is None:
            _PROFILE_RUNTIME = runtime
            return runtime
        if existing is runtime:
            return existing
        raise ProfileRuntimeAlreadyConfigured(
            "application Profile runtime composition is already configured"
        )


def require_profile_runtime() -> ProfileRuntimePort:
    """Return the configured Profile runtime or fail closed."""

    with _LOCK:
        runtime = _PROFILE_RUNTIME
    if runtime is None:
        raise ProfileRuntimeUnavailable("application Profile runtime composition is unavailable")
    return runtime


__all__ = [
    "ProfileRuntimePort",
    "ProfileRuntimeAlreadyConfigured",
    "ProfileRuntimeUnavailable",
    "SetupActivationDecision",
    "register_profile_runtime",
    "require_profile_runtime",
]
