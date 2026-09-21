"""Focused tests for the 91-ID Defaults compatibility projection."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core_runtime.global_contracts.defaults_compat import (
    DefaultsCompatibilityHandle,
    DefaultsLegacyInventoryItem,
    build_defaults_compatibility_handle,
    defaults_compatibility_api_inventory,
)
from core_runtime.global_contracts.legacy_projection import (
    LegacyProjectionRule,
    LegacyRegistryProjection,
)
from core_runtime.global_contracts.models import Cardinality


def _approved_inventory() -> tuple[DefaultsLegacyInventoryItem, ...]:
    return tuple(
        DefaultsLegacyInventoryItem(
            legacy_id=entry.legacy_id,
            component_id=entry.component_id,
            component_version=entry.contract_version,
        )
        for entry in defaults_compatibility_api_inventory()
    )


def _handle() -> DefaultsCompatibilityHandle:
    return build_defaults_compatibility_handle(
        _approved_inventory(),
        defaults_compatibility_api_inventory(),
    )


def test_reviewed_compatibility_table_is_complete_and_schema_valid() -> None:
    """All reviewed IDs map uniquely to their schema-valid action contract."""
    entries = defaults_compatibility_api_inventory()
    assert len(entries) == 91
    assert len({entry.legacy_id for entry in entries}) == 91
    assert sum(len(entry.targets) for entry in entries) == 92
    assert sum(len(entry.targets) == 1 for entry in entries) == 90
    assert sum(len(entry.targets) == 2 for entry in entries) == 1
    for entry in entries:
        suffix = entry.legacy_id.removeprefix("defaults.")
        assert entry.contract_id == f"rumi.action.legacy.{suffix}.v1"
        assert entry.contract_version == "1.0.0"
        assert entry.projection_rule.exact_key is True
        assert entry.projection_rule.cardinality is Cardinality.ONE
        for target in entry.targets:
            assert target.function_id
            assert target.risk in {"low", "medium", "high"}
            assert target.failure_semantics == "fail_closed"


def test_prompt_system_selection_is_operation_and_capability_aware() -> None:
    """Prompt get and set retain distinct security requirements."""
    handle = _handle()
    assert handle.select("defaults.prompt.system") is None
    prompt_get = handle.select(
        "defaults.prompt.system",
        operation="get",
    )
    assert prompt_get is not None
    assert prompt_get.function_id == "prompt_system_get"
    assert prompt_get.risk == "low"
    assert prompt_get.requires == ()
    assert handle.select(
        "defaults.prompt.system",
        operation="set",
    ) is None
    prompt_set = handle.select(
        "defaults.prompt.system",
        operation="set",
        granted_capabilities={"prompt.system.set"},
    )
    assert prompt_set is not None
    assert prompt_set.function_id == "prompt_system_set"
    assert prompt_set.risk == "medium"
    assert prompt_set.requires == ("prompt.system.set",)


def test_selector_fails_closed_without_prefix_collapse_or_invocation() -> None:
    """Unknown IDs, operations, and authorization never select metadata."""
    handle = _handle()
    assert handle.select("defaults.unknown") is None
    assert handle.select("defaults.agent.execute.extra") is None
    direct = handle.entries["defaults.agent.execute"]
    target = direct.targets[0]
    assert handle.select(
        direct.legacy_id,
        operation="get",
        granted_capabilities=target.requires,
        caller_capabilities=target.caller_requires,
    ) is None
    protected = next(
        entry
        for entry in handle.entries.values()
        if entry.targets[0].caller_requires
    )
    protected_target = protected.targets[0]
    assert handle.select(
        protected.legacy_id,
        granted_capabilities=protected_target.requires,
    ) is None
    assert handle.select(
        protected.legacy_id,
        granted_capabilities=protected_target.requires,
        caller_capabilities=protected_target.caller_requires,
    ) is not None


def test_handle_and_nested_metadata_are_immutable() -> None:
    """The compatibility handle cannot be mutated after construction."""
    handle = _handle()
    with pytest.raises(TypeError):
        handle.entries["defaults.unknown"] = handle.entries[
            "defaults.agent.execute"
        ]
    with pytest.raises(FrozenInstanceError):
        handle.entries["defaults.agent.execute"].contract_version = "2.0.0"


def test_legacy_projection_exact_key_preserves_read_only_semantics() -> None:
    """Exact-key projection ignores longer prefix matches without mutation."""

    class FakeRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def list(
            self,
            prefix: str | None = None,
            include_meta: bool = False,
        ) -> dict[str, object]:
            self.calls += 1
            assert prefix == "defaults.prompt.system"
            assert include_meta is True
            metadata = {
                "count": 1,
                "last_meta": {
                    "_source_pack_id": "legacy.pack",
                    "_source_pack_version": "1.0.0",
                },
            }
            return {
                "defaults.prompt.system": metadata,
                "defaults.prompt.system.extra": metadata,
            }

    registry = FakeRegistry()
    projection = LegacyRegistryProjection(
        registry,
        (
            LegacyProjectionRule(
                "defaults.prompt.system",
                "rumi.action.legacy.prompt.system.v1",
                cardinality=Cardinality.ONE,
                exact_key=True,
            ),
        ),
    )
    snapshot = projection.snapshot()
    assert len(snapshot) == 1
    assert registry.calls == 1
    assert snapshot[0].contract.contract_id == (
        "rumi.action.legacy.prompt.system.v1"
    )
