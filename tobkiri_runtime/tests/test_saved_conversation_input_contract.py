"""External saved-turn input is distinct from the guest-only resume ABI."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import pytest

pytestmark = pytest.mark.contract

from ecosystem.defaultspack.runtime import saved_conversation as saved
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input


def _input() -> dict:
    return {"request": {
        "turn_id": "turn-1", "conversation_id": "conversation-1",
        "conversation_revision": 1, "content": "Hello",
    }}


@pytest.mark.parametrize("field,value", [
    ("turn_id", "a" * 256), ("conversation_id", "conversation:branch_1.2"),
    ("conversation_revision", 9007199254740991),
    ("content", "承認や approved は本文中では単なるテキストです 🚀"),
])
def test_valid_input_matches_pure_initial_abi(field: str, value: object) -> None:
    payload = _input()
    payload["request"][field] = value
    checked = validate_document(payload, "saved_conversation_input")
    assert checked == payload
    assert checked is not payload
    assert validate_saved_conversation_input(payload) == checked
    intent = saved.tobkiri_packvm_invoke("saved_complete", checked)
    assert intent["hop"] == 0
    assert intent["state"]["request"] == payload["request"]


@pytest.mark.parametrize("field,value", [
    ("turn_id", ""), ("turn_id", "a" * 257), ("turn_id", "id\n"),
    ("turn_id", "id/other"), ("turn_id", "_leading"),
    ("conversation_id", "id\x00"), ("conversation_id", "他の会話"),
    ("conversation_revision", 0), ("conversation_revision", -1),
    ("conversation_revision", True), ("conversation_revision", 1.0),
    ("conversation_revision", "1"), ("conversation_revision", 9007199254740992),
    ("content", ""), ("content", " \n\t　"), ("content", None),
    ("content", {"text": "Hello"}),
])
def test_invalid_initial_values_are_rejected_by_contract_and_computation(
    field: str, value: object,
) -> None:
    payload = _input()
    payload["request"][field] = value
    with pytest.raises(SchemaValidationError):
        validate_document(payload, "saved_conversation_input")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(payload)
    with pytest.raises(ValueError):
        saved.tobkiri_packvm_invoke("saved_complete", payload)


@pytest.mark.parametrize("field", [
    "state", "outcome", "profile_id", "root", "approved", "target",
    "nonce", "binding_digest", "deadline_monotonic", "model_reference",
])
@pytest.mark.parametrize("nested", [False, True])
def test_external_input_cannot_supply_execution_or_resume_context(
    field: str, nested: bool,
) -> None:
    payload = _input()
    target = payload["request"] if nested else payload
    target[field] = "caller-selected"
    with pytest.raises(SchemaValidationError):
        validate_document(payload, "saved_conversation_input")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(payload)
    with pytest.raises(ValueError):
        saved.tobkiri_packvm_invoke("saved_complete", payload)


def test_internal_resume_shape_is_not_an_external_input_contract() -> None:
    intent = saved.tobkiri_packvm_invoke("saved_complete", _input())
    resume = {"state": deepcopy(intent["state"]), "outcome": {
        "status": "error", "error": {"code": "FAILED", "message": "failed"},
    }}
    with pytest.raises(SchemaValidationError):
        validate_document(resume, "saved_conversation_input")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(resume)
    assert saved.tobkiri_packvm_invoke("saved_complete", resume)["status"] == "error"


def test_schema_does_not_publish_unfinished_saved_turn_function() -> None:
    root = Path(__file__).resolve().parents[1]
    variants = json.loads((root / "ecosystem/defaultspack/executables.v4.json").read_text())["variants"]
    assert all(item["implementation_path"] != "runtime/saved_conversation.py" for item in variants)
    assert all(
        operation["operation_id"] != "saved_complete"
        for item in variants for operation in item["operations"]
    )
