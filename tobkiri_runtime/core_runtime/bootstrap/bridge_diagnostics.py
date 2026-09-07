"""Bounded Host-only diagnostics for failed guest capability calls."""

import logging

from tobkiri_host.errors import BackendUnavailableError

from ..authority.v4 import AuthorityDenied
from ..global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    HostCredentialTransportError,
)

_LOGGER = logging.getLogger(__name__)
_PROVIDER_CODES = frozenset(
    {
        "missing_provider",
        "unresolved_profile",
        "capability_mismatch",
        "provider_unavailable",
        "invalid_response",
        "deadline_exceeded",
    }
)


def record_bridge_failure(error: Exception) -> None:
    """Log only a fixed classification, never exception text or request data."""
    reason = "internal_error"
    if isinstance(error, AuthorityDenied):
        reason = "authority_denied"
    elif isinstance(error, BackendUnavailableError):
        reason = "backend_unavailable"
    elif isinstance(error, GlobalContractUnavailable):
        reason = "contract_unavailable"
    elif isinstance(error, HostCredentialTransportError):
        reason = "credential_transport_failed"
    elif isinstance(error, GlobalContractInvocationError):
        reason = "provider_error"
        if type(error.code) is str and error.code in _PROVIDER_CODES:
            reason = error.code
    elif isinstance(error, TimeoutError):
        reason = "timeout"
    _LOGGER.warning("PackVM capability bridge failed: %s", reason)
