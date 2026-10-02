"""Finite, identity-bound presentation of durable saved-turn events."""

from __future__ import annotations

from typing import Mapping


TURN_EVENT_TARGET = (
    "defaults.conversations.turn.events",
    "tobkiri.event.turn.v1",
    "rumi_turn_runtime_pack.turn-events",
    "rumi_turn_runtime_pack.turn-runtime.events",
    "rumi_turn_runtime_pack.turn-runtime.events",
)

_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def normalize_turn_event_read(
    payload: Mapping[str, object], *, profile_id: str
) -> dict[str, object]:
    """Require both durable identities before reading event state."""

    if set(payload) != {"turn_id", "conversation_id"} or not profile_id:
        raise ValueError("turn events require captured and durable identities")
    turn_id = _identifier(payload.get("turn_id"))
    conversation_id = _identifier(payload.get("conversation_id"))
    return {
        "profile_id": profile_id,
        "operation": "get",
        "turn_id": turn_id,
        "conversation_id": conversation_id,
    }


def present_turn_events(result: Mapping[str, object]) -> dict[str, object]:
    """Project a finite poll response without pretending it is live SSE."""

    turn_id = _identifier(result.get("id"))
    conversation_id = _identifier(result.get("conversation_id"))
    request_id = _identifier(result.get("request_id"))
    revision = result.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("turn event owner returned an invalid revision")
    status = str(result.get("status") or "")
    if status not in {"queued", "running", "waiting", *_TERMINAL}:
        raise ValueError("turn event owner returned an invalid status")
    raw_events = result.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("turn event owner returned invalid events")
    identity = {
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "operation_id": turn_id,
        "request_id": request_id,
    }
    events: list[dict[str, object]] = []
    for expected_sequence, raw in enumerate(raw_events):
        if not isinstance(raw, Mapping) or raw.get("sequence") != expected_sequence:
            raise ValueError("turn event owner returned a non-contiguous sequence")
        name = raw.get("name")
        at = raw.get("at")
        details = raw.get("details")
        if (
            not isinstance(name, str)
            or not name.startswith("turn.")
            or type(at) is not int
            or not isinstance(details, Mapping)
        ):
            raise ValueError("turn event owner returned a malformed event")
        events.append({
            **identity,
            "sequence": expected_sequence,
            "turn_revision": revision,
            "name": name,
            "at": at,
            "details": dict(details),
        })
    terminal = None
    if status in _TERMINAL:
        terminal = {
            **identity,
            "status": status,
            "turn_revision": revision,
            "result_reference": result.get("result_reference"),
            "error": result.get("error"),
        }
    return {
        **identity,
        "status": status,
        "turn_revision": revision,
        "events": events,
        "terminal": terminal,
        "turn": dict(result),
    }


def _identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value.strip() != value
    ):
        raise ValueError("turn events require stable identities")
    return value
