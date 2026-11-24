"""Finite optional live-progress grammar shares the protected thread boundary."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ecosystem.defaultspack.defaultspack.v4_view_contract import validate_catalog_view

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "ecosystem/tobkiri_side_chat_pack"
DESCRIPTOR = "frontend/contributions/side-chat.json"


def _descriptor() -> dict[str, Any]:
    return json.loads((PACK / DESCRIPTOR).read_text(encoding="utf-8"))


def test_shipped_progress_uses_both_strict_schemas_and_host_validator() -> None:
    """The shipped optional read passes outer admission and the Host grammar."""
    descriptor = _descriptor()
    schema = json.loads((ROOT / "schemas/frontend_contribution.schema.json").read_text())
    Draft202012Validator(schema).validate(descriptor)
    validate_catalog_view(descriptor["view"])
    protocol = json.loads(
        (ROOT / "tobkiri_protocol/schemas/ui_view_v1.schema.json").read_text()
    )
    assert schema["$defs"]["thread_progress_request"] == (
        protocol["$defs"]["thread_progress_request"]
    )
    proposal = json.loads((PACK / "integration-input.v1.json").read_text())
    digest = "sha256:" + hashlib.sha256((PACK / DESCRIPTOR).read_bytes()).hexdigest()
    artifact = next(
        item for item in proposal["pack_catalog_record"]["runtime_artifacts"] if item["path"] == DESCRIPTOR
    )
    assert artifact["digest"] == digest
    assert artifact["index_role"] == "sidecar"


@pytest.mark.parametrize("change", [
    "content_key", "turn_key", "cursor_key", "cursor_constant", "cursor_binding",
    "missing_conversation", "different_contract", "different_operation", "scope",
    "nested_model", "prototype", "url", "constant_binding_collision", "version",
])
def test_progress_descriptor_cannot_acquire_execution_or_overwrite_cursor(
    change: str,
) -> None:
    """Only the exact protected read can consume the retained ticket and cursor."""
    view = copy.deepcopy(_descriptor()["view"])
    request = view["conversation_thread"]["progress"]
    if change == "content_key":
        request["content_key"] = "content"
    elif change == "turn_key":
        request["turn_id_key"] = "other_turn"
    elif change == "cursor_key":
        request["cursor_key"] = "offset"
    elif change == "cursor_constant":
        request["input"] = {"cursor": 0}
    elif change == "cursor_binding":
        request["source_bindings"]["cursor"] = "revision"
    elif change == "missing_conversation":
        request["source_bindings"] = {}
    elif change == "different_contract":
        request["operation"]["contract_id"] = "tobkiri.action.side-chat.v1"
    elif change == "different_operation":
        request["operation"]["operation_id"] = "rumi_turn_runtime_pack.turn-manage"
    elif change == "scope":
        request["input"] = {"approved": True, "profile_id": "foreign"}
    elif change == "nested_model":
        request["input"] = {"nested": {"model_reference": "foreign"}}
    elif change == "prototype":
        request["source_bindings"]["conversation_id"] = "constructor.id"
    elif change == "url":
        request["url"] = "https://example.invalid/progress"
    elif change == "constant_binding_collision":
        request["input"] = {"conversation_id": "foreign"}
    else:
        view["version"] = "tobkiri.ui.view.v2"
    with pytest.raises((ValidationError, ValueError)):
        validate_catalog_view(view)


def test_absent_progress_keeps_canonical_thread_and_explicit_recovery() -> None:
    """No optional resource means no live read requirement or implicit retry."""
    view = _descriptor()["view"]
    del view["conversation_thread"]["progress"]
    validate_catalog_view(view)
    assert view["conversation_thread"]["reconcile"]["input"] == {
        "operation": "reconcile"
    }
