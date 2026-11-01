"""Host-private cancellation of one natively approved, exact saved target.

This port deliberately leaves ordinary owner/session stop bindings untouched.
Only production composition may supply its authenticated authorization callback.
No token, copied owner session, or JSON approved flag grants access to this port.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.operation_cancellation import (
    CancellationObservation,
    OwnedCancellationHandles,
)
from tobkiri_protocol.canonical import canonical_digest


CAPTURE_FIELDS = (
    "profile_id", "profile_revision", "activation_id", "activation_digest",
    "plan_digest", "profile_authority_digest", "security_epoch", "fencing_token",
)
TARGET_FIELDS = frozenset({
    "conversation_id", "conversation_revision", "turn_id", "request_id",
    "revision", "status",
})
SAVED = ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
DELIVERY = (
    "tobkiri.action.chat.message.delivery.v1",
    "rumi_turn_runtime_pack.chat-message-deliver",
)
GROUP = ("rumi_turn_runtime_pack", "saved-turn")


class ApprovedInterruptBinding:
    """A narrow Host capability tied to an approved source invocation.

    ``authorize`` is a Host-composed guard proving the exact prepared interrupt
    plan and approved execute ancestry. It is never supplied by contract payload.
    ``before_signal`` belongs to the trusted turn owner and must atomically CAS
    the target run version and persist its interruption intent before returning.
    """

    def __init__(
        self,
        *,
        registry: OwnedCancellationHandles,
        envelope: RequestEnvelope,
        presentation_owner_principal_id: str,
        authorize: Callable[[Mapping[str, Any]], None],
        guard: Callable[[], None],
    ) -> None:
        if (
            not isinstance(registry, OwnedCancellationHandles)
            or not isinstance(envelope, RequestEnvelope)
            or (envelope.contract_id, envelope.operation_id) != DELIVERY
            or not isinstance(presentation_owner_principal_id, str)
            or not presentation_owner_principal_id
            or not callable(authorize)
            or not callable(guard)
        ):
            raise PermissionError("approved interrupt binding is unavailable")
        self._registry = registry
        self._envelope = envelope
        self._owner = presentation_owner_principal_id
        self._authorize = authorize
        self._guard = guard

    def request(
        self,
        target_state: Mapping[str, Any],
        *,
        before_signal: Callable[[], None],
    ) -> CancellationObservation:
        """CAS through the owner before signalling the exact private handle."""
        state = _validate_state(target_state, self._envelope.context.profile_id)
        if not callable(before_signal):
            raise PermissionError("approved interrupt owner CAS is unavailable")
        registry = self._registry
        capture = tuple(
            getattr(self._envelope.context, field) for field in CAPTURE_FIELDS
        )
        with registry._lock:
            self._guard()
            self._authorize(state)
            if registry._closed:
                raise PermissionError("approved interrupt activation is unavailable")
            matches = [
                key for key in registry._active
                if key[:2] == GROUP
                and key[2] == self._owner
                and key[4:-1] == capture
                and key[-1] == state["turn_id"]
            ]
            if len(matches) != 1:
                raise PermissionError("approved interrupt exact target is unavailable")
            key = matches[0]
            signal, completed = registry._active[key]
            proof = registry._records.get(key)
            if proof is None or proof._scope_exited:
                raise PermissionError("approved interrupt target has left execution")
            target = proof._envelope
            request = target.payload.get("request")
            if (
                (target.contract_id, target.operation_id) != SAVED
                or not isinstance(request, Mapping)
                or request.get("turn_id") != state["turn_id"]
                or request.get("conversation_id") != state["conversation_id"]
                or tuple(getattr(target.context, field) for field in CAPTURE_FIELDS)
                != capture
                or signal is self._envelope.cancellation_requested
                or any(
                    other != key and value[0] is signal
                    for other, value in registry._active.items()
                )
                or any(
                    other != key
                    and record._envelope is not target
                    and record._envelope.cancellation_requested is signal
                    for other, record in registry._records.items()
                )
            ):
                raise PermissionError("approved interrupt target scope is unavailable")
            # The exact key retains the target's private authenticated session.
            # Cross-session access exists only through this approved capability.
            before_signal()
            self._guard()
            self._authorize(state)
            if registry._active.get(key) != (signal, completed):
                raise PermissionError("approved interrupt target changed before signal")
            proof.request()
            signal.set()
            return CancellationObservation(completed=completed, _proof=proof)


def _validate_state(value: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != TARGET_FIELDS:
        raise ValueError("approved interrupt target state is invalid")
    state = dict(value)
    if (
        state["status"] not in {"running", "waiting"}
        or any(
            not isinstance(state[field], str) or not state[field]
            or len(state[field]) > 256
            for field in ("conversation_id", "turn_id", "request_id")
        )
        or type(state["revision"]) is not int or state["revision"] < 1
        or type(state["conversation_revision"]) is not int
        or state["conversation_revision"] < 1
        or state["request_id"] != "saved-turn." + canonical_digest({
            "profile_id": profile_id, "turn_id": state["turn_id"],
        }).removeprefix("sha256:")
    ):
        raise ValueError("approved interrupt target identity is invalid")
    return state
