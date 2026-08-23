"""Retired capability-graph routes fail closed at the Pack v4 boundary."""

from __future__ import annotations

import json
from pathlib import Path

from tests.v4_batch_support import assert_route_cutover

RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _assert_retired(method: str, path: str) -> None:
    """Prove a historical capability route is not a current route authority."""
    assert_route_cutover(method, path, "conversation.turn.v1", "complete")


def test_nodes_api_returns_viewer_palette_shape() -> None:
    _assert_retired("GET", "/api/nodes")


def test_profile_nodes_api_filters_palette_by_profile_state_and_locale() -> None:
    _assert_retired("GET", "/api/profiles/coding/nodes")


def test_profile_nodes_api_tolerates_nodes_without_profile_state() -> None:
    _assert_retired("GET", "/api/profiles/coding/nodes")


def test_profile_nodes_api_falls_back_when_state_registry_is_invalid() -> None:
    _assert_retired("GET", "/api/profiles/coding/nodes")


def test_profiles_api_documents_startup_profile_boundary() -> None:
    _assert_retired("GET", "/api/panel/profiles")


def test_graph_compile_preview_requires_profile_and_does_not_register_by_default() -> None:
    _assert_retired(
        "POST",
        "/api/panel/startup/profiles/coding/compile-preview",
    )


def test_graph_compile_preview_returns_surface_launch_target() -> None:
    _assert_retired(
        "POST",
        "/api/panel/startup/profiles/coding/compile-preview",
    )


def test_draft_graph_validation_error_returns_400_diagnostics() -> None:
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )

    assert_retired_module_absent("core_runtime.ecosystem_nodes")
    assert_profile_resolver_requires_authority_snapshot()


def test_graph_save_validation_error_returns_400_diagnostics() -> None:
    _assert_retired("POST", "/api/graphs")


def test_core_control_panel_registers_capability_graph_api_routes() -> None:
    for method, path in (
        ("GET", "/api/nodes"),
        ("GET", "/api/panel/profiles"),
        ("GET", "/api/profiles/coding/nodes"),
        ("POST", "/api/panel/startup/profiles/coding/compile-preview"),
        ("POST", "/api/graphs/coding_graph/compile"),
    ):
        _assert_retired(method, path)


def test_retired_graph_routes_are_absent_from_legacy_panel_manifest() -> None:
    """A stale panel manifest cannot advertise removed graph handlers."""

    manifest = json.loads(
        (
            RUNTIME_ROOT
            / "core_runtime/core_pack/core_control_panel/ecosystem.json"
        ).read_text(encoding="utf-8")
    )
    routes = manifest["api_routes"]
    assert not any(
        str(route.get("path", route.get("path_pattern", ""))).startswith(
            ("/api/graphs", "/api/panel/graphs")
        )
        for route in routes
    )
