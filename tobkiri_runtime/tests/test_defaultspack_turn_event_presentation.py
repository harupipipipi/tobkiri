"""Saved-turn event reads remain finite and bound to durable identities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecosystem.defaultspack.defaultspack.turn_event_presentation import (
    normalize_turn_event_read,
    present_turn_events,
)


def _turn(status: str = "running") -> dict[str, object]:
    return {
        "id": "turn-1",
        "request_id": "saved-turn.request-1",
        "conversation_id": "conversation-1",
        "status": status,
        "revision": 3,
        "events": [
            {"sequence": 0, "name": "turn.queued", "at": 1, "details": {}},
            {"sequence": 1, "name": f"turn.{status}", "at": 2, "details": {}},
        ],
        "result_reference": None,
        "error": None,
    }


def test_turn_event_read_binds_profile_turn_and_conversation() -> None:
    assert normalize_turn_event_read(
        {"turn_id": "turn-1", "conversation_id": "conversation-1"},
        profile_id="defaults",
    ) == {
        "profile_id": "defaults",
        "operation": "get",
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
    }
    with pytest.raises(ValueError, match="identities"):
        normalize_turn_event_read({"turn_id": "turn-1"}, profile_id="defaults")


def test_turn_event_projection_uses_one_identity_for_every_event() -> None:
    projected = present_turn_events(_turn())
    assert projected["terminal"] is None
    assert projected["turn"] == _turn()
    for event in projected["events"]:
        assert event["turn_id"] == "turn-1"
        assert event["conversation_id"] == "conversation-1"
        assert event["operation_id"] == "turn-1"
        assert event["request_id"] == "saved-turn.request-1"


def test_turn_event_projection_preserves_terminal_receipt_and_error() -> None:
    completed = _turn("completed")
    completed["result_reference"] = {
        "conversation_id": "conversation-1",
        "conversation_revision": 4,
        "user_message_id": "message:user",
        "assistant_message_id": "message:assistant",
        "outcome_digest": "sha256:" + "a" * 64,
    }
    projected = present_turn_events(completed)
    assert projected["terminal"] == {
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "operation_id": "turn-1",
        "request_id": "saved-turn.request-1",
        "turn_revision": 3,
        "status": "completed",
        "result_reference": completed["result_reference"],
        "error": None,
    }

    failed = _turn("failed")
    failed["error"] = {"code": "MODEL_FAILED", "message": "failed"}
    assert present_turn_events(failed)["terminal"]["error"] == failed["error"]


def test_turn_event_projection_keeps_requested_nonterminal_until_confirmed() -> None:
    requested = _turn()
    requested["events"].append({
        "sequence": 2,
        "name": "turn.cancellation_requested",
        "at": 3,
        "details": {"phase": "nested_cancellation_requested"},
    })
    requested["revision"] = 4
    assert present_turn_events(requested)["terminal"] is None

    confirmed = {**requested, "status": "cancelled", "revision": 5}
    confirmed["events"] = [
        *requested["events"],
        {
            "sequence": 3,
            "name": "turn.cancelled",
            "at": 4,
            "details": {"phase": "nested_cancellation_confirmed"},
        },
    ]
    terminal = present_turn_events(confirmed)["terminal"]
    assert terminal is not None
    assert terminal["status"] == "cancelled"
    assert terminal["turn_id"] == "turn-1"
    assert terminal["operation_id"] == "turn-1"


def test_turn_event_projection_rejects_non_contiguous_owner_events() -> None:
    turn = _turn()
    turn["events"][1]["sequence"] = 4
    with pytest.raises(ValueError, match="non-contiguous"):
        present_turn_events(turn)


def test_fixed_frontend_map_and_profile_select_the_event_owner() -> None:
    root = Path(__file__).resolve().parents[1]
    contract_map = json.loads((
        root / "ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json"
    ).read_text(encoding="utf-8"))
    routes = [
        route for route in contract_map["routes"]
        if (route["method"], route["path"])
        == ("GET", "/api/chat/turn/events")
    ]
    assert len(routes) == 1
    assert routes[0]["presentation"] == "turn_events"
    assert routes[0]["targets"] == [{
        "contribution_id": "defaults.conversations.turn.events",
        "contract_id": "tobkiri.event.turn.v1",
        "operation_id": "rumi_turn_runtime_pack.turn-events",
        "provider_id": "rumi_turn_runtime_pack.turn-runtime.events",
        "function_id": "rumi_turn_runtime_pack.turn-runtime.events",
        "allowed_payload_keys": ["turn_id", "conversation_id"],
    }]

    profile = json.loads((
        root / "ecosystem/defaultspack/v4/defaults.profile.v5.json"
    ).read_text(encoding="utf-8"))
    assert any(
        edge["caller_function_id"] == "shell.tauri.default"
        and edge["contract_id"] == "tobkiri.event.turn.v1"
        and edge["operation_id"] == "rumi_turn_runtime_pack.turn-events"
        and edge["target_provider_id"]
        == "rumi_turn_runtime_pack.turn-runtime.events"
        for edge in profile["requested_edges"]
    )
