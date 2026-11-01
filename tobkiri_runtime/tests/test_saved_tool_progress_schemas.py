"""Sealed schemas admit captured tool lifecycle and reject client extra fields."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "a" * 64
START = {
    "type": "tool_started",
    "tool_id": "calculator",
    "tool_call_id": "captured-call",
    "arguments": {"expression": "2+2"},
}
COMPLETE = {
    "type": "tool_completed",
    "tool_id": "calculator",
    "tool_call_id": "captured-call",
    "status": "success",
    "content": '{"status":"success","result":4,"error":null}',
}


def _schema(contract_id: str, kind: str) -> dict:
    catalog = json.loads((ROOT / "ecosystem/rumi_turn_runtime_pack/contracts.v4.json").read_text())
    contract = next(item for item in catalog["contracts"] if item["contract_id"] == contract_id)
    return contract["schema_catalog"][contract["operations"][0][f"{kind}_schema_digest"]]


@pytest.mark.parametrize(
    "payload",
    [
        {"phase": "ready"},
        {"phase": "tool_bind", "progress_id": IDENTITY},
        {"phase": "tool_publish", "progress_id": IDENTITY, "cursor": 1, "event": START},
        {"phase": "tool_publish", "progress_id": IDENTITY, "cursor": 2, "event": COMPLETE},
    ],
)
def test_signed_action_admits_captured_tool_lifecycle(payload: dict) -> None:
    """The signed action schema accepts exactly the host producer frames."""
    schema = _schema("tobkiri.action.turn.progress.v1", "input")
    Draft202012Validator(schema).validate(payload)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({**payload, "approved": True})


def test_signed_resource_accepts_authenticated_stage_cursor() -> None:
    """Resource cursors permit stage identity and reject malformed identities."""
    validator = Draft202012Validator(_schema("tobkiri.resource.turn.progress.v1", "input"))
    payload = {"turn_id": "turn", "conversation_id": "conversation", "cursor": 0}
    validator.validate(payload)
    validator.validate({**payload, "progress_id": IDENTITY})
    with pytest.raises(ValidationError):
        validator.validate({**payload, "progress_id": "foreign"})


def test_signed_resource_output_includes_real_tool_events() -> None:
    """The protected page admits start/result events with a required stage ID."""
    schema = _schema("tobkiri.resource.turn.progress.v1", "output")
    assert "progress_id" in schema["required"]
    validator = Draft202012Validator(schema["properties"]["events"])
    validator.validate([{"cursor": 1, "event": START}, {"cursor": 2, "event": COMPLETE}])
    with pytest.raises(ValidationError):
        validator.validate([{"cursor": 2, "event": {**COMPLETE, "status": "pending"}}])
