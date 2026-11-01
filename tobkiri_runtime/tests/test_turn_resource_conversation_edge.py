"""Normative producer regressions; these do not simulate native authority."""

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.generate_defaultspack_v4_bundle import _validate_catalog
from tobkiri_protocol.bundle_catalog import BundledCatalog

RUNTIME = Path(__file__).resolve().parents[1]
SOURCE = "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
CALLER = "rumi_turn_runtime_pack.turn-runtime.resource"
CONTRACT = "tobkiri.resource.conversation.v1"
OPERATION = "rumi_conversation_store_pack.conversation-resource"


def _catalog() -> tuple[BundledCatalog, dict[str, Any]]:
    catalog = BundledCatalog.load(RUNTIME / "ecosystem/defaultspack/v4")
    intent = json.loads((RUNTIME / SOURCE).read_text(encoding="utf-8"))
    return catalog, intent


def test_actual_intent_has_exactly_one_valid_resource_read_edge() -> None:
    catalog, intent = _catalog()
    _validate_catalog(catalog)
    matches = [
        edge
        for edge in intent["requested_edges"]
        if edge["caller_function_id"] == CALLER
        and edge["contract_id"] == CONTRACT
        and edge["operation_id"] == OPERATION
    ]
    assert len(matches) == 1
    edge = matches[0]
    generated_matches = [
        item
        for item in catalog.profiles[intent["profile_id"]]["requested_edges"]
        if item["caller_function_id"] == CALLER
        and item["contract_id"] == CONTRACT
        and item["operation_id"] == OPERATION
    ]
    contract_digests = [
        contract["revision_digest"]
        for pack in catalog.packs.values()
        if any(
            function["id"] == edge["target_provider_id"]
            for function in pack["functions"]
        )
        for contract in pack["contracts"]
        if contract["contract_id"] == CONTRACT and OPERATION in contract["operations"]
    ]
    assert len(contract_digests) == 1
    expected = copy.deepcopy(edge)
    expected["requested_scope_template"]["semantics_digest"] = contract_digests[0]
    assert generated_matches == [expected]
    assert edge["caller_function_id"] == CALLER
    assert edge["target_provider_id"] == (
        "rumi_conversation_store_pack.conversation-store.resource"
    )
    assert edge["requested_scope_template"] == {
        "capability": "operation.invoke",
        "dimensions": {"contract": [CONTRACT], "operation": [OPERATION]},
        "quotas": {},
        "exact_request_digest": None,
        "opaque": False,
    }
    assert edge["authority_mode"] == "profile_grant"


def test_duplicate_resource_edge_is_rejected_by_canonical_generator() -> None:
    catalog, intent = _catalog()
    catalog = replace(catalog, profiles=copy.deepcopy(dict(catalog.profiles)))
    catalog.profiles[intent["profile_id"]]["requested_edges"].append(
        copy.deepcopy(
            next(
                edge
                for edge in catalog.profiles[intent["profile_id"]]["requested_edges"]
                if edge["caller_function_id"] == CALLER
                and edge["contract_id"] == CONTRACT
                and edge["operation_id"] == OPERATION
            )
        )
    )
    with pytest.raises(ValueError, match="duplicate requested edges"):
        _validate_catalog(catalog)
