"""Host-private ancestry of actual dispatched invocations, never a wire claim."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from core_runtime.authority.v4 import LeaseState
from tobkiri_host.broker import RequestEnvelope


@dataclass(frozen=True)
class CapturedInvocationScopeV4:
    """Retain authenticated execution and its live guard across nested dispatch."""

    envelope: RequestEnvelope
    assert_current: Callable[[], None]
    parent: CapturedInvocationScopeV4 | None = None


def assert_dispatched_invocation(
    envelope: RequestEnvelope, authority_store: Any,
) -> None:
    """Check the actual sealed lease and all revocations without minting authority."""

    context = envelope.context
    if (
        envelope.cancellation_requested.is_set()
        or envelope.deadline_monotonic <= time.monotonic()
        or authority_store.security_epoch != context.security_epoch
    ):
        raise PermissionError("captured invocation is no longer active")
    durable, state = authority_store.inspect_lease_token(
        envelope.lease.token.decode("ascii")
    )
    if state is not LeaseState.DISPATCHED or (
        durable.caller.principal_id != context.caller_principal.value
        or durable.target.principal_id != envelope.target_principal.value
        or durable.target.operation_id != envelope.operation_id
        or durable.profile_id != context.profile_id
        or durable.activation_id != context.activation_id
        or durable.security_epoch != context.security_epoch
        or durable.target_domain_id != context.target_domain_id
        or durable.target_boot_epoch != context.target_boot_epoch
        or durable.request_id != context.request_id
        or durable.request_digest != envelope.request_digest
    ):
        raise PermissionError("captured invocation lease does not match")
    if any(authority_store.is_revoked(kind, identity) for kind, identity in (
        ("function_principal", durable.caller.principal_id),
        ("function_principal", durable.target.principal_id),
        ("execution_domain", durable.caller_domain_id),
        ("execution_domain", durable.target_domain_id),
        ("profile", durable.profile_id),
        ("activation", durable.activation_id),
        ("grant", durable.grant_id),
        ("provider_authority", durable.provider_authority_id),
    )):
        raise PermissionError("captured invocation authority was revoked")
