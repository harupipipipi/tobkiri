"""Parity and production-catalog checks for the first migrated Wasm Function."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ecosystem.rumi_scheduler_tool_adapter_pack.runtime.adapter import (
    create_definition_contribution,
)
from scripts.generate_scheduler_definitions_component import build_component
from tobkiri_host.wasm_component import PureComponent


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "ecosystem" / "rumi_scheduler_tool_adapter_pack"
COMPONENT = PACK / "runtime" / "definitions_component.wasm"
OPERATION = "rumi_scheduler_tool_adapter_pack.scheduler-tool-definitions"
FUNCTION = "rumi_scheduler_tool_adapter_pack.tool-definitions.scheduler"


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def test_checked_in_component_is_deterministically_generated() -> None:
    binary = COMPONENT.read_bytes()
    assert binary == build_component()
    assert binary.startswith(b"\x00asm")


@pytest.mark.parametrize(
    "payload",
    ({}, {"ignored": True}, {"nested": {"value": [1, "two", None]}}),
)
def test_component_matches_the_previous_python_catalog(payload: dict) -> None:
    expected = create_definition_contribution(None)("catalog", payload)
    binary = COMPONENT.read_bytes()
    actual = PureComponent(binary, _digest(binary)).invoke(OPERATION, payload)
    assert actual == expected


def test_only_the_pure_definition_function_moves_to_wasm() -> None:
    manifest = json.loads((PACK / "pack.v4.json").read_text(encoding="utf-8"))
    catalog = json.loads((PACK / "executables.v4.json").read_text(encoding="utf-8"))
    functions = {item["id"]: item for item in manifest["functions"]}
    variants = {item["function_id"]: item for item in catalog["variants"]}

    assert functions[FUNCTION]["isolation"] == "wasm_component"
    assert variants[FUNCTION] == {
        **variants[FUNCTION],
        "variant_id": f"{FUNCTION}.wasm",
        "execution_kind": "wasm",
        "backend": "tobkiri.wasmtime-pulley-v1",
        "runtime_abi": "component-v1",
        "execution_domain_profile": "wasm.component.default.v1",
        "implementation_path": "runtime/definitions_component.wasm",
    }
    privileged = "rumi_scheduler_tool_adapter_pack.tool-adapter.scheduler"
    assert functions[privileged]["isolation"] == "pack_vm"
    assert variants[privileged]["execution_kind"] == "pack_vm"
    assert variants[privileged]["backend"] == "tobkiri.python-pack-v4"


def test_wasm_definition_operation_keeps_its_read_only_contract_ceiling() -> None:
    catalog = json.loads((PACK / "executables.v4.json").read_text(encoding="utf-8"))
    variant = next(item for item in catalog["variants"] if item["function_id"] == FUNCTION)
    assert len(variant["operations"]) == 1
    assert variant["operations"][0]["operation_id"] == OPERATION
    assert variant["operations"][0]["effect_class"] == "read"
