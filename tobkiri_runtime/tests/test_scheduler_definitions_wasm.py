"""Parity and production-catalog checks for the first migrated Wasm Function."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from core_runtime.authority.v4 import DomainBoundary
from ecosystem.rumi_scheduler_tool_adapter_pack.runtime.adapter import (
    create_definition_contribution,
)
from ecosystem.rumi_scheduler_tool_adapter_pack.runtime.generate_definitions_component import (
    build_component,
)
from tests.conformance_support.host_profile import captured_host_profile
from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.wasm_component import PureComponent
from tobkiri_host.wasm_backend import production_wasm_backend

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "ecosystem" / "rumi_scheduler_tool_adapter_pack"
COMPONENT = PACK / "runtime" / "definitions_component.wasm"
OPERATION = "rumi_scheduler_tool_adapter_pack.scheduler-tool-definitions"
FUNCTION = "rumi_scheduler_tool_adapter_pack.tool-definitions.scheduler"
LOCAL_CONTRACT = "tobkiri.service.tool.local.operation.v1"
LOCAL_PROVIDER = "rumi_scheduler_tool_adapter_pack.tool-adapter.scheduler"
LOCAL_OPERATION = "rumi_scheduler_tool_adapter_pack.scheduler-tool-operation"
# Keep in sync with the local executor's identifier gate in
# ``rumi_tool_local_executor_pack/runtime/executor.py``.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def test_checked_in_component_is_deterministically_generated() -> None:
    binary = COMPONENT.read_bytes()
    assert binary == build_component()
    assert binary.startswith(b"\x00asm")


@pytest.mark.parametrize(
    "payload",
    ({}, {"approved": True}, {"nested": {"value": [1, "two", None]}}),
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
    wasm_variant = variants[FUNCTION]
    assert wasm_variant["variant_id"] == f"{FUNCTION}.wasm"
    assert wasm_variant["execution_kind"] == "wasm"
    assert wasm_variant["backend"] == "tobkiri.wasmtime-pulley-v1"
    assert wasm_variant["runtime_abi"] == "component-v1"
    assert wasm_variant["execution_domain_profile"] == "wasm.component.default.v1"
    assert wasm_variant["implementation_path"] == "runtime/definitions_component.wasm"
    privileged = "rumi_scheduler_tool_adapter_pack.tool-adapter.scheduler"
    assert functions[privileged]["isolation"] == "pack_vm"
    assert variants[privileged]["execution_kind"] == "pack_vm"
    assert variants[privileged]["backend"] == "tobkiri.python-pack-v4"


def test_component_and_previous_python_catalog_both_reject_unknown_operation() -> None:
    with pytest.raises(ValueError, match="unknown scheduler tool catalog operation"):
        create_definition_contribution(None)("unknown", {})
    binary = COMPONENT.read_bytes()
    with pytest.raises(ProviderExecutionError, match="component rejected"):
        PureComponent(binary, _digest(binary)).invoke("unknown", {})


def test_wasm_definition_operation_keeps_its_read_only_contract_ceiling() -> None:
    catalog = json.loads((PACK / "executables.v4.json").read_text(encoding="utf-8"))
    variant = next(
        item for item in catalog["variants"] if item["function_id"] == FUNCTION
    )
    assert len(variant["operations"]) == 1
    assert variant["operations"][0]["operation_id"] == OPERATION
    assert variant["operations"][0]["effect_class"] == "read"


def test_production_profile_authority_and_broker_invoke_scheduler_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the migrated production Pack through its captured Profile edge."""

    contract = "tobkiri.resource.tool.definition.contribution.v1"
    caller = "rumi_tool_registry_pack.tool-registry.definition"
    edge = {
        "caller_function_id": caller,
        "target_provider_id": FUNCTION,
        "contract_id": contract,
        "operation_id": OPERATION,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {
                "contract": [contract],
                "operation": [OPERATION],
            },
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }
    backend = production_wasm_backend()
    if not backend.status.ready_for_production:
        pytest.skip(backend.status.unavailable_reason or "hard controller unavailable")
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=("rumi_scheduler_tool_adapter_pack",),
        edges=(edge,),
        backends=(backend,),
    ) as (session, store):
        session.assert_operation_ready(contract, OPERATION)
        domain = next(
            item
            for item in store.list_domains()
            if any(principal.function_id == FUNCTION for principal in item.principals)
        )
        assert domain.boundary is DomainBoundary.WASM_COMPONENT
        assert len(domain.principals) == 1
        assert domain.principals[0].function_id == FUNCTION

        actual = session.invoke(
            "tobkiri.resource.tool.definition.v1",
            "rumi_tool_registry_pack.tool-definition-resource",
            {
                "operation": "list",
                "_session_id": "scheduler-wasm-candidate",
            },
        )

    expected = create_definition_contribution(None)("catalog", {})
    expected_ids = {item["tool_id"] for item in expected["definitions"]}
    actual_ids = {item["tool_id"] for item in actual["definitions"]}
    assert expected_ids <= actual_ids
    assert {item["provider_instance_id"] for item in actual["contributions"]} == {
        "tool-definitions.scheduler"
    }


def test_contributed_definitions_resolve_to_a_declared_local_operation() -> None:
    """Every contributed local definition must name a dispatchable operation.

    The local executor fails closed unless ``execution.operation`` is a valid
    identifier that matches exactly one captured provider route, so a missing
    or invented operation id would leave catalog-visible tools undispatchable.
    """

    catalog = json.loads((PACK / "executables.v4.json").read_text(encoding="utf-8"))
    routes = {
        (variant["function_id"], operation["operation_id"])
        for variant in catalog["variants"]
        if variant.get("backend")
        for operation in variant.get("operations", ())
        if operation.get("contract_id") == LOCAL_CONTRACT
    }
    assert (LOCAL_PROVIDER, LOCAL_OPERATION) in routes

    for source in (
        create_definition_contribution(None)("catalog", {})["definitions"],
        PureComponent(COMPONENT.read_bytes(), _digest(COMPONENT.read_bytes()))
        .invoke(OPERATION, {})["definitions"],
    ):
        assert source, "scheduler contribution must not be empty"
        for definition in source:
            execution = definition.get("execution")
            assert isinstance(execution, dict)
            assert execution.get("kind") == "local"
            assert execution.get("contract_id") == LOCAL_CONTRACT
            for key in ("provider_instance_id", "operation"):
                value = execution.get(key)
                assert isinstance(value, str)
                assert _IDENTIFIER.fullmatch(value), (
                    f"{definition.get('tool_id')}: {key} is not a valid identifier"
                )
            assert execution["provider_instance_id"] == LOCAL_PROVIDER
            assert execution["operation"] == LOCAL_OPERATION
            assert (execution["provider_instance_id"], execution["operation"]) in routes
