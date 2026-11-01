"""Host-private stop authority for an exact Broker-selected Calendar occurrence."""

from typing import Any, Callable, Mapping

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tobkiri_host.operation_cancellation import (
    CancellationObservation,
    OwnedCancellationHandles,
)
from tobkiri_protocol.canonical import canonical_digest

ADAPTER = (
    "tobkiri.action.job.adapter.v2",
    "rumi_turn_runtime_pack.chat-saved-job-adapter",
)
BROKER = ("tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker")
GROUP = ("rumi_turn_runtime_pack", "saved-turn")
FIELDS = frozenset(
    {"profile_id", "action_id", "payload", "idempotency_key", "schedule_id", "lease_id"}
)
CAPTURE = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
)


def _occurrence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PermissionError("scheduled occurrence is unavailable")
    if set(value) - FIELDS - {"operation", "version", "_session_id"}:
        raise PermissionError("scheduled occurrence fields are invalid")
    if not FIELDS <= set(value) or value.get("action_id") != "chat.saved":
        raise PermissionError("scheduled occurrence identity is invalid")
    if (
        value.get("version", "tobkiri.job-dispatch-envelope.v1")
        != "tobkiri.job-dispatch-envelope.v1"
    ):
        raise PermissionError("scheduled occurrence version is invalid")
    result = {key: value[key] for key in FIELDS}
    if any(
        not isinstance(result[key], str) or not result[key] or len(result[key]) > 256
        for key in FIELDS - {"payload"}
    ):
        raise PermissionError("scheduled occurrence identity is invalid")
    if not isinstance(result["payload"], Mapping):
        raise PermissionError("scheduled occurrence payload is invalid")
    # A finite canonical copy prevents mutable nested aliases during the CAS.
    import json

    encoded = json.dumps(result, sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > 131072:
        raise PermissionError("scheduled occurrence is too large")
    return json.loads(encoded)


class ScheduledJobCancellationBinding:
    """Stop only the exact active adapter selected by a granted JobBroker cancel.

    Production supplies the actual captured scope and guard. Target owner session
    remains exclusively in the registry. No wire flag or profile-only authority
    can manufacture this binding.
    """

    def __init__(
        self,
        *,
        registry: OwnedCancellationHandles,
        scope: CapturedInvocationScopeV4,
        guard: Callable[[], None]
    ) -> None:
        if (
            not isinstance(registry, OwnedCancellationHandles)
            or not isinstance(scope, CapturedInvocationScopeV4)
            or not callable(guard)
        ):
            raise PermissionError("scheduled cancellation binding is unavailable")
        self._registry, self._scope, self._guard = registry, scope, guard

    def _authorize(self, occurrence: Mapping[str, Any]) -> None:
        scope = self._scope
        parent = scope.parent
        scope.assert_current()
        self._guard()
        if parent is None:
            raise PermissionError("scheduled cancellation parent is unavailable")
        parent.assert_current()
        child, broker = scope.envelope, parent.envelope
        if (
            (child.contract_id, child.operation_id) != ADAPTER
            or (broker.contract_id, broker.operation_id) != BROKER
            or child.payload.get("operation") != "cancel"
            or broker.payload.get("operation") != "cancel"
            or _occurrence(child.payload) != occurrence
            or broker.payload.get("idempotency_key") != occurrence["idempotency_key"]
            or broker.payload.get("profile_id") != occurrence["profile_id"]
            or child.context.profile_id != occurrence["profile_id"]
            or any(
                getattr(child.context, key) != getattr(broker.context, key)
                for key in CAPTURE
            )
        ):
            raise PermissionError("scheduled cancellation ancestry does not match")

    def request(
        self,
        original_job_envelope: Mapping[str, Any],
        *,
        turn_id: str,
        before_signal: Callable[[], None]
    ) -> CancellationObservation:
        """Persist exact owner cancellation intent before signalling private proof."""
        occurrence = _occurrence(original_job_envelope)
        expected = "calendar:" + canonical_digest(
            [
                occurrence["profile_id"],
                occurrence["schedule_id"],
                occurrence["idempotency_key"],
            ]
        ).removeprefix("sha256:")
        if turn_id != expected or not callable(before_signal):
            raise PermissionError("scheduled cancellation turn does not match")
        registry = self._registry
        with registry._lock:
            self._authorize(occurrence)
            current = self._scope.envelope
            capture = tuple(getattr(current.context, key) for key in CAPTURE)
            matches = []
            for key, handles in registry._active.items():
                proof = registry._records.get(key)
                if (
                    key[:2] != GROUP
                    or key[4:-1] != capture
                    or key[-1] != turn_id
                    or proof is None
                    or proof._scope_exited
                ):
                    continue
                target = proof._envelope
                if (
                    (target.contract_id, target.operation_id) == ADAPTER
                    and target.target_principal == current.target_principal
                    and target.payload.get("operation") == "dispatch"
                    and _occurrence(target.payload) == occurrence
                ):
                    matches.append((key, handles, proof))
            if registry._closed or len(matches) != 1:
                raise PermissionError(
                    "scheduled cancellation exact execution is unavailable"
                )
            key, (signal, completed), proof = matches[0]
            if (
                signal is current.cancellation_requested
                or any(
                    other != key and value[0] is signal
                    for other, value in registry._active.items()
                )
                or any(
                    other != key
                    and record._envelope is not proof._envelope
                    and record._envelope.cancellation_requested is signal
                    for other, record in registry._records.items()
                )
            ):
                raise PermissionError(
                    "scheduled cancellation shared signal is unavailable"
                )
            before_signal()
            self._authorize(occurrence)
            if registry._active.get(key) != (signal, completed):
                raise PermissionError("scheduled cancellation execution changed")
            proof.request()
            signal.set()
            return CancellationObservation(completed=completed, _proof=proof)
