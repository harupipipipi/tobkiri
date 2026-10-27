"""Validate Calendar completion against actual ancestry and owner-bound source."""

from typing import Any, Mapping
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tobkiri_protocol.canonical import canonical_digest

CALENDAR_FUNCTION = "rumi_turn_runtime_pack.chat-saved-job-adapter"
ADAPTER = ("tobkiri.action.job.adapter.v2", CALENDAR_FUNCTION)
BROKER = ("tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker")
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


def assert_calendar_completion_source(
    invocation: Any, turn: Mapping[str, Any], profile_id: str
) -> None:
    """Match exact captured adapter ancestry to the durable preparation fingerprint."""
    invocation.assert_current()
    parent = invocation.parent_invocation
    if not isinstance(parent, CapturedInvocationScopeV4):
        raise PermissionError("Calendar completion ancestry is unavailable")
    parent.assert_current()
    grandparent = parent.parent
    if not isinstance(grandparent, CapturedInvocationScopeV4):
        raise PermissionError("Calendar completion broker ancestry is unavailable")
    grandparent.assert_current()
    actual = invocation.envelope
    envelope = parent.envelope
    broker = grandparent.envelope
    payload = parent.public_payload()
    if (
        (envelope.contract_id, envelope.operation_id) != ADAPTER
        or envelope.contract_version != "2.0.0"
        or (broker.contract_id, broker.operation_id) != BROKER
        or broker.contract_version != "1.0.0"
        or envelope.target_principal.value != actual.context.caller_principal.value
        or payload.get("operation") not in {"dispatch", "status"}
        or payload.get("action_id") != "chat.saved"
        or payload.get("profile_id") != profile_id
        or broker.payload.get("operation") not in {"dispatch", "status"}
        or broker.payload.get("profile_id") != profile_id
        or broker.payload.get("idempotency_key") != payload.get("idempotency_key")
        or any(
            getattr(actual.context, key) != getattr(envelope.context, key)
            or getattr(envelope.context, key) != getattr(broker.context, key)
            for key in CAPTURE
        )
    ):
        raise PermissionError("Calendar completion captured source differs")
    raw = payload.get("payload")
    if not isinstance(raw, Mapping) or raw.get("profile_id") != profile_id:
        raise PermissionError("Calendar completion task is unavailable")
    for key in ("schedule_id", "idempotency_key", "lease_id"):
        if (
            not isinstance(payload.get(key), str)
            or not payload[key]
            or len(payload[key]) > 256
        ):
            raise PermissionError("Calendar completion occurrence is invalid")
    expected_turn = "calendar:" + canonical_digest(
        [
            profile_id,
            payload["schedule_id"],
            payload["idempotency_key"],
        ]
    ).removeprefix("sha256:")
    expected_target = raw.get("conversation_id") or "calendar:" + canonical_digest(
        [
            profile_id,
            payload["idempotency_key"],
        ]
    ).removeprefix("sha256:")
    fingerprint = canonical_digest(
        {
            "profile_id": profile_id,
            "occurrence_key": payload["idempotency_key"],
            "source": dict(raw),
            "turn_id": expected_turn,
            "conversation_id": expected_target,
        }
    )
    if (
        turn.get("id") != expected_turn
        or turn.get("conversation_id") != expected_target
        or turn.get("calendar_preparation") != fingerprint
    ):
        raise PermissionError("Calendar completion immutable source differs")
    invocation.assert_current()
    parent.assert_current()
    grandparent.assert_current()
