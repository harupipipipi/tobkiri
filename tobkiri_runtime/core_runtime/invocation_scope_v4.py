"""Host-private ancestry of actual dispatched invocations, never a wire claim."""

from __future__ import annotations

from dataclasses import dataclass
import time
import hashlib
from threading import RLock
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import LeaseState
from tobkiri_host.broker import RequestEnvelope
from tobkiri_protocol.canonical import canonical_digest


@dataclass(frozen=True)
class CapturedInvocationScopeV4:
    """Retain authenticated execution and its live guard across nested dispatch."""

    envelope: RequestEnvelope
    assert_current: Callable[[], None]
    parent: CapturedInvocationScopeV4 | None = None

    def public_payload(self) -> dict[str, Any]:
        """Copy normalized input without Host session metadata or mutable aliases."""

        def copy(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: copy(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [copy(item) for item in value]
            return value

        return {
            key: copy(item)
            for key, item in self.envelope.payload.items()
            if key != "_session_id"
        }


def execution_session_id(envelope: RequestEnvelope) -> str:
    """Separate concurrent executions while leaving their presentation owner stable."""
    context = envelope.context
    return "session.host-execution." + canonical_digest(
        {
            "request_id": context.request_id,
            "request_digest": envelope.request_digest,
            "caller_session_id": context.caller_session_id,
            "target_principal": envelope.target_principal.value,
            "profile_id": context.profile_id,
            "activation_id": context.activation_id,
            "plan_digest": context.plan_digest,
            # This is Host-private scope identity, never a wire token or Grant.
            "lease_identity": hashlib.sha256(envelope.lease.token).hexdigest(),
        }
    ).removeprefix("sha256:")


class ParentInvocationScopesV4:
    """Retain request-local parents across Broker threads until their final user drains."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, tuple[CapturedInvocationScopeV4, int]] = {}

    def lookup(self, session_id: str) -> CapturedInvocationScopeV4 | None:
        """Return only the exact Host-retained scope for this execution session."""
        with self._lock:
            entry = self._entries.get(session_id)
            return entry[0] if entry else None

    def retain(self, session_id: str, scope: CapturedInvocationScopeV4) -> None:
        """Share concurrent calls from one parent without accepting replacements."""
        with self._lock:
            prior = self._entries.get(session_id)
            if prior is not None and prior[0].envelope is not scope.envelope:
                raise PermissionError("nested captured parent changed")
            self._entries[session_id] = (scope, prior[1] + 1 if prior else 1)

    def release(self, session_id: str) -> None:
        """Remove the parent's live reference at its final verified dispatch return."""
        with self._lock:
            prior = self._entries.get(session_id)
            if prior is None:
                return
            if prior[1] == 1:
                del self._entries[session_id]
            else:
                self._entries[session_id] = (prior[0], prior[1] - 1)


def assert_dispatched_invocation(
    envelope: RequestEnvelope,
    authority_store: Any,
) -> None:
    """Check the actual sealed lease and all revocations without minting authority."""

    context = envelope.context
    if (
        envelope.cancellation_requested.is_set()
        or envelope.deadline_monotonic <= time.monotonic()
    ):
        raise PermissionError("captured invocation is no longer active")
    durable, state, epoch, revoked = authority_store.inspect_dispatch_authority(
        envelope.lease.token.decode("ascii")
    )
    if (
        envelope.cancellation_requested.is_set()
        or envelope.deadline_monotonic <= time.monotonic()
        or epoch != context.security_epoch
    ):
        raise PermissionError("captured invocation is no longer active")
    if state is not LeaseState.DISPATCHED or (
        durable.caller.principal_id != context.caller_principal.value
        or durable.target.principal_id != envelope.target_principal.value
        or durable.target.operation_id != envelope.operation_id
        or durable.profile_id != context.profile_id
        or durable.activation_id != context.activation_id
        or durable.activation_digest != context.activation_digest
        or durable.plan_digest != context.plan_digest
        or durable.profile_authority_digest != context.profile_authority_digest
        or durable.fencing_token != context.fencing_token
        or durable.caller_domain_id != context.caller_domain_id
        or durable.caller_boot_epoch != context.caller_boot_epoch
        or durable.security_epoch != context.security_epoch
        or durable.target_domain_id != context.target_domain_id
        or durable.target_boot_epoch != context.target_boot_epoch
        or durable.request_id != context.request_id
        or durable.request_digest != envelope.request_digest
    ):
        raise PermissionError("captured invocation lease does not match")
    if revoked:
        raise PermissionError("captured invocation authority was revoked")
