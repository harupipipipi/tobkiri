"""Bounded Host-only diagnostics for failed guest capability calls."""

import logging

from tobkiri_host.errors import (
    AuthorizationError,
    BackendUnavailableError,
    ProviderExecutionError,
    RequestTimedOutError,
)

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
    # Broker wraps nested failures. Follow only its known wrapper, with a
    # fixed bound even if an exception's cause chain contains a cycle.
    for _ in range(8):
        if type(error) is not ProviderExecutionError:
            break
        cause = error.__cause__
        if not isinstance(cause, Exception):
            break
        error = cause
    reason = "internal_error"
    if isinstance(error, (AuthorityDenied, AuthorizationError)):
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
    elif isinstance(error, (TimeoutError, RequestTimedOutError)):
        reason = "timeout"
    _LOGGER.warning("PackVM capability bridge failed: %s", reason)
