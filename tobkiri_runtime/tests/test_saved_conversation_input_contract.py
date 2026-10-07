"""External saved-turn input is distinct from the guest-only resume ABI."""

from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
import json
import hashlib

from jsonschema import Draft202012Validator

import pytest

from ecosystem.tobkiri_conversation_orchestration_pack.runtime import saved_conversation as saved
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

pytestmark = pytest.mark.contract

_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII="
)


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
    assert intent["state"]["request"] == {
        key: item for key, item in payload["request"].items() if key != "content"
    }


def test_bounded_inline_image_matches_external_and_guest_contracts() -> None:
    """A saved turn accepts its exact bounded text-plus-raster block shape."""
    payload = _input()
    payload["request"]["content"] = [
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": _TINY_PNG}},
    ]
    checked = validate_document(payload, "saved_conversation_input")
    assert validate_saved_conversation_input(checked) == checked
    intent = saved.tobkiri_packvm_invoke("saved_complete", checked)
    assert intent["state"]["user_content"] == payload["request"]["content"]
    assert "content" not in intent["state"]["request"]


@pytest.mark.parametrize(
    "content",
    [
        "あ" * 21000,
        [{"type": "text", "text": "あ" * 21000}],
    ],
    ids=["unicode_text", "unicode_text_part"],
)
def test_oversized_utf8_text_reports_byte_limit(content: object) -> None:
    payload = _input()
    payload["request"]["content"] = content

    with pytest.raises(ValueError, match="byte limit"):
        validate_saved_conversation_input(payload)


def test_oversized_text_does_not_mask_malformed_content() -> None:
    payload = _input()
    payload["request"]["content"] = [
        {"type": "text", "text": "あ" * 21000, "unexpected": True}
    ]

    with pytest.raises(ValueError, match="content is invalid"):
        validate_saved_conversation_input(payload)


@pytest.mark.parametrize("url", [
    "https://example.test/image.png",
    "data:image/svg+xml;base64,PHN2Zy8+",
    "data:image/png;base64,A===",
])
def test_saved_inline_image_rejects_non_raster_or_invalid_data_url(url: str) -> None:
    payload = _input()
    payload["request"]["content"] = [
        {"type": "text", "text": "Inspect this"},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    with pytest.raises(SchemaValidationError):
        validate_document(payload, "saved_conversation_input")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(payload)


@pytest.mark.parametrize("url", [
    "data:image/png;base64,AAE=",
    "data:image/jpeg;base64,iVBORw0KGgo=",
    "data:image/webp;base64,R0lGODlh",
])
def test_saved_inline_image_rejects_wrong_magic_for_declared_mime(url: str) -> None:
    """Schema syntax is insufficient: both roots verify MIME-matched bytes."""
    payload = _input()
    payload["request"]["content"] = [
        {"type": "text", "text": "Inspect this"},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    assert validate_document(payload, "saved_conversation_input") == payload
    with pytest.raises(ValueError):
        validate_saved_conversation_input(payload)
    with pytest.raises(ValueError):
        saved.tobkiri_packvm_invoke("saved_complete", payload)


def test_saved_inline_image_limit_rejects_third_or_oversized_raster() -> None:
    payload = _input()
    payload["request"]["content"] = [
        {"type": "text", "text": "Inspect these"},
        *[
            {"type": "image_url", "image_url": {"url": _TINY_PNG}}
            for _ in range(3)
        ],
    ]
    with pytest.raises(SchemaValidationError):
        validate_document(payload, "saved_conversation_input")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(payload)

    oversized = _input()
    encoded = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"\0" * (1024 * 1024)
    ).decode()
    oversized["request"]["content"] = [
        {"type": "text", "text": "Inspect this"},
        {"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{encoded}"
        }},
    ]
    with pytest.raises(SchemaValidationError):
        validate_document(oversized, "saved_conversation_input")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(oversized)


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


@pytest.mark.parametrize("level", ["none", "low", "medium", "high", "xhigh"])
def test_saved_turn_accepts_finite_thinking_level(level: str) -> None:
    payload = _input()
    payload["request"]["thinking_level"] = level
    assert validate_document(payload, "saved_conversation_input") == payload
    assert validate_saved_conversation_input(payload) == payload
    state = saved.start(payload["request"])["state"]
    assert state["request"] == {
        key: item for key, item in payload["request"].items() if key != "content"
    }
    assert state["user_content"] == payload["request"]["content"]
    assert state["user_content_digest"] is None


@pytest.mark.parametrize("level", ["ultra", "", None, True, 1, [], {}])
def test_saved_turn_rejects_unbounded_thinking_level(level: object) -> None:
    payload = _input()
    payload["request"]["thinking_level"] = level
    with pytest.raises(SchemaValidationError):
        validate_document(payload, "saved_conversation_input")
    with pytest.raises(ValueError, match="thinking level is invalid"):
        validate_saved_conversation_input(payload)
    with pytest.raises(ValueError, match="thinking level is invalid"):
        saved.start(payload["request"])


def test_saved_function_is_sealed_with_only_the_initial_input_schema() -> None:
    """Registration pins the pure ABI, not guest resume or execution authority."""
    root = Path(__file__).resolve().parents[1]
    variants = json.loads((root / "ecosystem/tobkiri_conversation_orchestration_pack/executables.v4.json").read_text())["variants"]
    selected = [item for item in variants if item["function_id"] == "tobkiri_conversation_orchestration_pack.saved"]
    assert len(selected) == 1
    variant = selected[0]
    assert variant["implementation_path"] == "runtime/saved_conversation.py"
    assert variant["implementation_digest"] == "sha256:" + hashlib.sha256(
        (root / "ecosystem/tobkiri_conversation_orchestration_pack/runtime/saved_conversation.py").read_bytes()
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
        assert saved_edges[0]["target_provider_id"] == "tobkiri_conversation_orchestration_pack.saved"
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
    assert variant["execution_kind"] == "host_extension"
    assert variant["backend"] == "tobkiri.python-host-v4"
    assert variant["implementation_path"] == "runtime/host.py"
    schema = json.loads(
        (root / "tobkiri_protocol/schemas/saved_conversation_input_v1.schema.json").read_text()
    )
    assert variant["operations"][0]["input_schema"] == schema


def test_reconciliation_has_no_execution_edge_and_accepts_only_a_turn_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    pack = root / "ecosystem/rumi_turn_runtime_pack"
    contract = next(item for item in json.loads((pack / "contracts.v4.json").read_text())["contracts"]
                    if item["contract_id"] == "tobkiri.action.turn.reconcile.v1")
    effects = contract["operations"][0]["effect_ceiling"]
    assert "capability:turn.reconcile" in effects
    assert not {"capability:turn.execute", "capability:turn.manage"}.intersection(effects)
    variant = next(item for item in json.loads((pack / "executables.v4.json").read_text())["variants"]
                   if item["function_id"] == "rumi_turn_runtime_pack.turn-runtime.reconcile")
    validator = Draft202012Validator(variant["operations"][0]["input_schema"])
    assert validator.is_valid({"turn_id": "turn-1"})
    for value in (_input(), {"turn_id": "../escape"}, {"turn_id": True},
                  {"turn_id": "turn-1", "approved": True},
                  {"turn_id": "turn-1", "result_reference": {}},
                  {"turn_id": "turn-1", "profile_id": "other"}):
        assert not validator.is_valid(value)
    profile = json.loads((root / "ecosystem/defaultspack/v4/defaults.profile.v5.json").read_text())
    edges = [edge for edge in profile["requested_edges"]
             if edge["caller_function_id"] == variant["function_id"]]
    assert [(edge["contract_id"], edge["operation_id"]) for edge in edges] == [
        ("tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource"),
    ]
