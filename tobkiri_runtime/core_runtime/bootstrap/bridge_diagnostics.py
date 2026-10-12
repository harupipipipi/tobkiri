"""Bounded Host-only diagnostics for failed guest capability calls."""

import logging
import re

from tobkiri_protocol.errors import CanonicalizationError

from tobkiri_host.errors import (
    AdmissionError,
    AmbiguousEffectError,
    AuditUnavailableError,
    AuthorizationError,
    BackendUnavailableError,
    ProviderExecutionError,
    QueueFullError,
    RequestCancellationRequestedError,
    RequestTimedOutError,
    ResolutionError,
    ResourceExhaustedError,
    ResourceHandleError,
)

from ..authority.v4 import AuthorityDenied
from ..global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    HostCredentialTransportError,
)

_LOGGER = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,191}\Z")
_REQUEST_ID = re.compile(r"request\.[0-9a-f]{32}\Z")
_PHASES = frozenset(
    {
        "finite_source",
        "provider_dispatch",
        "result_shape",
        "result_projection",
        "result_encoding",
    }
)
# Exact class identities prevent custom exception class names from becoming
# a channel for provider-controlled text. Unknown classes get a fixed label.
_ERROR_TYPES = {
    Exception: "Exception",
    RuntimeError: "RuntimeError",
    ValueError: "ValueError",
    TypeError: "TypeError",
    PermissionError: "PermissionError",
    OSError: "OSError",
    FileNotFoundError: "FileNotFoundError",
    FileExistsError: "FileExistsError",
    IsADirectoryError: "IsADirectoryError",
    NotADirectoryError: "NotADirectoryError",
    TimeoutError: "TimeoutError",
    ConnectionError: "ConnectionError",
    ConnectionResetError: "ConnectionResetError",
    ConnectionRefusedError: "ConnectionRefusedError",
    BrokenPipeError: "BrokenPipeError",
    KeyError: "KeyError",
    AttributeError: "AttributeError",
    AssertionError: "AssertionError",
    ImportError: "ImportError",
    ModuleNotFoundError: "ModuleNotFoundError",
    LookupError: "LookupError",
    UnicodeError: "UnicodeError",
    UnicodeDecodeError: "UnicodeDecodeError",
    UnicodeEncodeError: "UnicodeEncodeError",
    OverflowError: "OverflowError",
    MemoryError: "MemoryError",
    NotImplementedError: "NotImplementedError",
    RecursionError: "RecursionError",
    AuthorityDenied: "AuthorityDenied",
    AdmissionError: "AdmissionError",
    AmbiguousEffectError: "AmbiguousEffectError",
    AuditUnavailableError: "AuditUnavailableError",
    AuthorizationError: "AuthorizationError",
    BackendUnavailableError: "BackendUnavailableError",
    ProviderExecutionError: "ProviderExecutionError",
    QueueFullError: "QueueFullError",
    RequestCancellationRequestedError: "RequestCancellationRequestedError",
    RequestTimedOutError: "RequestTimedOutError",
    ResolutionError: "ResolutionError",
    ResourceExhaustedError: "ResourceExhaustedError",
    ResourceHandleError: "ResourceHandleError",
    GlobalContractUnavailable: "GlobalContractUnavailable",
    GlobalContractInvocationError: "GlobalContractInvocationError",
    HostCredentialTransportError: "HostCredentialTransportError",
    CanonicalizationError: "CanonicalizationError",
}
_PROVIDER_CODES = frozenset(
    {
        "missing_provider",
        "unresolved_profile",
        "capability_mismatch",
        "provider_unavailable",
        "invalid_response",
        "deadline_exceeded",
        "denied",
        "incompatible",
        "invalid_request",
        "not_configured",
        "route_binding_invalid",
        "route_binding_stale",
        "route_binding_conflict",
    }
)


def _identifier(value: object) -> str:
    return value if type(value) is str and _IDENTIFIER.fullmatch(value) else "unknown"


def _error_types(error: Exception) -> str:
    types: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(types) < 4 and id(current) not in seen:
        seen.add(id(current))
        types.append(_ERROR_TYPES.get(type(current), "OtherError"))
        cause = current.__cause__
        current = (
            cause
            if cause is not None
            else (None if current.__suppress_context__ else current.__context__)
        )
    return ">".join(types)


def _reason(error: Exception) -> str:
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
    return reason


def record_bridge_failure(
    error: Exception,
    *,
    request_id: str = "unknown",
    phase: str = "unknown",
    contract_id: str = "unknown",
    operation_id: str = "unknown",
) -> None:
    """Log Host correlation and fixed labels without exception or request text."""
    try:
        _LOGGER.warning(
            "PackVM capability bridge failed: %s request=%s phase=%s "
            "contract=%s operation=%s error_types=%s",
            _reason(error),
            request_id
            if type(request_id) is str and _REQUEST_ID.fullmatch(request_id)
            else "unknown",
            phase if type(phase) is str and phase in _PHASES else "unknown",
            _identifier(contract_id),
            _identifier(operation_id),
            _error_types(error),
        )
    except Exception:  # noqa: BLE001 - diagnostics cannot replace bridge outcomes
        return
