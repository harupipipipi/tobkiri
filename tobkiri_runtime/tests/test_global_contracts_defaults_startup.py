"""Focused startup retention tests for Defaults compatibility metadata."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

from core_runtime.global_contracts.defaults_compat import (
    DefaultsCompatibilityHandle,
    DefaultsLegacyInventoryItem,
    defaults_compatibility_api_inventory,
)
from core_runtime.kernel import Kernel
from core_runtime.paths import BASE_DIR


def _approved_inventory() -> tuple[DefaultsLegacyInventoryItem, ...]:
    return tuple(
        DefaultsLegacyInventoryItem(
            legacy_id=entry.legacy_id,
            component_id=entry.component_id,
            component_version=entry.contract_version,
        )
        for entry in defaults_compatibility_api_inventory()
    )


def _kernel() -> tuple[Kernel, MagicMock]:
    diagnostics = MagicMock()
    install_journal = MagicMock()
    interface_registry = MagicMock()
    event_bus = MagicMock()
    lifecycle = MagicMock()
    kernel = Kernel(
        diagnostics=diagnostics,
        install_journal=install_journal,
        interface_registry=interface_registry,
        event_bus=event_bus,
        lifecycle=lifecycle,
    )
    interface_registry.reset_mock()
    return kernel, interface_registry


def test_startup_flow_places_static_handler_at_priority_55() -> None:
    """The static build runs after component setup and before lib processing."""
    flow_path = BASE_DIR / "flows" / "00_startup.flow.yaml"
    flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
    steps = {step["id"]: step for step in flow["steps"]}
    compat = steps["defaults_compat_build"]
    assert compat == {
        "id": "defaults_compat_build",
        "phase": "ecosystem",
        "priority": 55,
        "type": "handler",
        "input": {"handler": "kernel:defaults.compat.build"},
    }
    assert steps["component_setup"]["priority"] < compat["priority"]
    assert compat["priority"] < steps["lib_process_all"]["priority"]


def test_smallest_profile_retains_handle_and_cleans_shutdown() -> None:
    """Injected metadata builds 91 bindings without registry or provider use."""
    kernel, interface_registry = _kernel()
    handler = kernel._kernel_handlers["kernel:defaults.compat.build"]
    args = {
        "legacy_inventory": _approved_inventory(),
        "api_inventory": defaults_compatibility_api_inventory(),
    }

    first_result = handler(args, {})
    first = first_result["output"]
    assert isinstance(first, DefaultsCompatibilityHandle)
    assert len(first.entries) == 91
    assert kernel._defaults_compat_handles == (first,)
    assert kernel._defaults_compat_handles[0] is first
    assert len(kernel._shutdown_handlers) == 1

    second_result = handler(args, {})
    second = second_result["output"]
    assert second is not first
    assert kernel._defaults_compat_handles == (second,)
    assert kernel._defaults_compat_handles[0] is second
    assert len(kernel._shutdown_handlers) == 1
    assert interface_registry.mock_calls == []

    first_shutdown = kernel.shutdown()
    assert kernel._defaults_compat_handles == ()
    assert not any(
        item.get("status") == "failed"
        for item in first_shutdown["results"]
    )

    third_result = handler(args, {})
    third = third_result["output"]
    assert third is not second
    assert kernel._defaults_compat_handles == (third,)
    assert len(kernel._shutdown_handlers) == 1
    second_shutdown = kernel.shutdown()
    assert kernel._defaults_compat_handles == ()
    assert not any(
        item.get("status") == "failed"
        for item in second_shutdown["results"]
    )
    assert interface_registry.mock_calls == []


def test_component_inventory_extraction_stays_in_memory() -> None:
    """Approved component metadata can be injected without filesystem access."""
    entries = defaults_compatibility_api_inventory()
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        grouped.setdefault(entry.component_id, []).append(entry.legacy_id)
    components = tuple(
        SimpleNamespace(
            pack_id="defaults",
            id=component_id,
            version="1.0.0",
            manifest={
                "id": component_id,
                "version": "1.0.0",
                "connectivity": {"provides": legacy_ids},
            },
        )
        for component_id, legacy_ids in grouped.items()
    )
    kernel, interface_registry = _kernel()
    result = kernel._kernel_handlers["kernel:defaults.compat.build"](
        {},
        {"_discovered_component_objects": components},
    )
    handle = result["output"]
    assert len(handle.entries) == 91
    assert kernel._defaults_compat_handles[0] is handle
    assert interface_registry.mock_calls == []
    shutdown = kernel.shutdown()
    assert not any(
        item.get("status") == "failed"
        for item in shutdown["results"]
    )
