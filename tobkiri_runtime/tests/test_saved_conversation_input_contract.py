"""External saved-turn input is distinct from the guest-only resume ABI."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import hashlib

from jsonschema import Draft202012Validator

import pytest

from ecosystem.defaultspack.runtime import saved_conversation as saved
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

pytestmark = pytest.mark.contract


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


def test_saved_function_is_sealed_with_only_the_initial_input_schema() -> None:
    """Registration pins the pure ABI, not guest resume or execution authority."""
    root = Path(__file__).resolve().parents[1]
    variants = json.loads((root / "ecosystem/defaultspack/executables.v4.json").read_text())["variants"]
    selected = [item for item in variants if item["function_id"] == "defaultspack.conversation.saved"]
    assert len(selected) == 1
    variant = selected[0]
    assert variant["implementation_path"] == "runtime/saved_conversation.py"
    assert variant["implementation_digest"] == "sha256:" + hashlib.sha256(
        (root / "ecosystem/defaultspack/runtime/saved_conversation.py").read_bytes()
    ).hexdigest()
    assert variant["execution_kind"] == "pack_vm"
    assert variant["backend"] == "tobkiri.python-pack-v4"
    assert len(variant["operations"]) == 1
    operation = variant["operations"][0]
    assert operation["contract_id"] == "conversation.saved-turn.v1"
    assert operation["operation_id"] == "saved_complete"
    schema = json.loads(
        (root / "tobkiri_protocol/schemas/saved_conversation_input_v1.schema.json").read_text()
    )
    assert operation["input_schema"] == schema
    validator = Draft202012Validator(operation["input_schema"])
    validator.validate(_input())
    assert not validator.is_valid({"state": {}, "outcome": {}})
    assert not validator.is_valid({**_input(), "approved": True})


def test_defaults_saved_edge_requires_coordinator_and_ui_remains_separate() -> None:
    """Defaults declares the coordinator chain, never direct Shell to guest."""
    root = Path(__file__).resolve().parents[1]
    bundle = root / "ecosystem/defaultspack/v4"
    for path in bundle.glob("*.profile.*.json"):
        profile = json.loads(path.read_text())
        edges = profile.get("requested_edges", [])
        if not edges:
            continue
        saved_edges = [edge for edge in edges
                       if edge["contract_id"] == "conversation.saved-turn.v1"]
        assert len(saved_edges) == 1
        assert saved_edges[0]["caller_function_id"] == "rumi_turn_runtime_pack.turn-runtime.saved"
        assert saved_edges[0]["target_provider_id"] == "defaultspack.conversation.saved"
        assert saved_edges[0]["operation_id"] == "saved_complete"
        assert any(edge["caller_function_id"] == "shell.tauri.default"
                   and edge["contract_id"] == "tobkiri.action.turn.saved.v1" for edge in edges)
    routes = json.loads(
        (root / "ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json").read_text()
    )
    assert "conversation.saved-turn.v1" not in json.dumps(routes)


def test_saved_host_registration_has_separate_execution_capability() -> None:
    root = Path(__file__).resolve().parents[1]
    pack_root = root / "ecosystem/rumi_turn_runtime_pack"
    contracts = json.loads((pack_root / "contracts.v4.json").read_text())["contracts"]
    contract = next(item for item in contracts
                    if item["contract_id"] == "tobkiri.action.turn.saved.v1")
    assert contract["owner"] == "rumi_turn_runtime_pack"
    assert contract["operations"][0]["operation_id"] == "rumi_turn_runtime_pack.turn-saved"
    assert "capability:turn.execute" in contract["operations"][0]["effect_ceiling"]
    assert "capability:turn.manage" not in contract["operations"][0]["effect_ceiling"]
    variants = json.loads((pack_root / "executables.v4.json").read_text())["variants"]
    variant = next(item for item in variants
                   if item["function_id"] == "rumi_turn_runtime_pack.turn-runtime.saved")
    assert variant["execution_kind"] == "host_brokered"
    assert variant["implementation_path"] == "runtime/host.py"
    schema = json.loads(
        (root / "tobkiri_protocol/schemas/saved_conversation_input_v1.schema.json").read_text()
    )
    assert variant["operations"][0]["input_schema"] == schema
