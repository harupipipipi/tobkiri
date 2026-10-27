"""Narrow Host-envelope views shared by Pack service implementations."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Mapping

from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import OpaqueInvocationLease


_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


@dataclass(frozen=True)
class HostWorkspaceScope:
    """Authenticated Host workspace binding for one currently-live invocation."""

    workspace_id: str


def require_host_workspace_scope(value: object) -> HostWorkspaceScope:
    """Extract a non-payload workspace identity from a live Host envelope.

    Pack code must not depend on the authority-bridge Pack merely to validate
    its Host context.  This deliberately exposes only the binding required by
    workspace services and leaves authority issuance/redemption in its own
    provider implementation.
    """

    envelope = value if isinstance(value, RequestEnvelope) else getattr(value, "envelope", None)
    if not isinstance(envelope, RequestEnvelope):
        raise PermissionError("Host-authenticated request envelope is required")
    context = envelope.context
    if not isinstance(context, RequestContext):
        raise PermissionError("Host-authenticated request context is invalid")
    if not isinstance(context.caller_principal, OpaqueAuthorityRef):
        raise PermissionError("Host caller principal is invalid")
    if not isinstance(envelope.target_principal, OpaqueAuthorityRef):
        raise PermissionError("Host target principal is invalid")
    if not isinstance(envelope.target_domain, OpaqueAuthorityRef):
        raise PermissionError("Host target domain is invalid")
    if not isinstance(envelope.lease, OpaqueInvocationLease):
        raise PermissionError("Host invocation lease is invalid")
    if not isinstance(envelope.payload, Mapping):
        raise PermissionError("Host envelope payload is invalid")
    if not _DIGEST.fullmatch(str(envelope.request_digest or "")):
        raise PermissionError("Host request digest is invalid")
    for field_name in (
        "activation_digest",
        "plan_digest",
        "profile_authority_digest",
        "target_backend_digest",
    ):
        if not _DIGEST.fullmatch(str(getattr(context, field_name) or "")):
            raise PermissionError(f"Host {field_name} is invalid")
    if not isinstance(envelope.deadline_monotonic, (int, float)) or isinstance(
        envelope.deadline_monotonic, bool
    ):
        raise PermissionError("Host request context is invalid")
    if not math.isfinite(envelope.deadline_monotonic) or envelope.deadline_monotonic <= time.monotonic():
        raise PermissionError("Host request context is expired")
    required_fields = (
        "request_id",
        "trace_id",
        "profile_id",
        "activation_id",
        "caller_session_id",
        "caller_domain_id",
        "handle_namespace",
    )
    if any(not str(getattr(context, field_name, "") or "").strip() for field_name in required_fields):
        raise PermissionError("Host request context is incomplete")
    workspace_id = str(
        getattr(value, "workspace_id", "") or getattr(context, "workspace_id", "") or ""
    ).strip()
    if not workspace_id:
        raise PermissionError("Host workspace binding is unavailable")
    return HostWorkspaceScope(workspace_id=workspace_id)


__all__ = ["HostWorkspaceScope", "require_host_workspace_scope"]
