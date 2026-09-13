"""Finite-route regressions for the production Pack v4 HTTP boundary."""

from __future__ import annotations

import pytest

from core_runtime.api.route_handlers import (
    _compile_template_path,
    _is_safe_path_param,
)
from core_runtime.pack_api_server import PackAPIHandler


def test_template_compiler_remains_available_to_offline_validators() -> None:
    compiled = _compile_template_path("/api/panel/flows/{id}")
    assert compiled is not None
    pattern, names = compiled
    assert names == ["id"]
    assert pattern.match("/api/panel/flows/my-flow") is not None
    assert pattern.match("/api/panel/flows/a/b") is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("my-flow-id", True),
        ("", True),
        ("../../../etc/passwd", False),
        ("evil\x00payload", False),
    ],
)
def test_path_parameter_safety(value: str, expected: bool) -> None:
    assert _is_safe_path_param(value) is expected


def test_production_handler_has_no_manifest_route_loader() -> None:
    assert not hasattr(PackAPIHandler, "load_api_routes")
    assert not hasattr(PackAPIHandler, "load_pre_auth_routes")
    assert not hasattr(PackAPIHandler, "load_web_mounts")


def test_production_handler_has_no_registry_route_dispatch() -> None:
    assert not hasattr(PackAPIHandler, "_dispatch_api_route")
    assert not hasattr(PackAPIHandler, "_dispatch_defaultspack_http_route")
    assert not hasattr(PackAPIHandler, "_match_pack_route")


@pytest.mark.parametrize(
    "path",
    [
        "/api/packs",
        "/api/authority/events",
        "/api/runtime/available",
        "/api/packs/scan",
        "/api/routes/reload",
    ],
)
def test_reproduced_legacy_paths_share_one_retirement_boundary(path: str) -> None:
    assert PackAPIHandler._retired_api_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v4/dispatch",
        "/api/setup/status",
        "/api/setup/packs",
        "/api/setup/packs/install",
        "/api/setup/complete",
        "/not-an-api-route",
    ],
)
def test_current_and_unknown_paths_are_not_legacy_root_matches(path: str) -> None:
    assert not PackAPIHandler._retired_api_path(path)
