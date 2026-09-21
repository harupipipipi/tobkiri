from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
for path in (str(ROOT), str(DEFAULTSPACK_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


CHANNEL_ROUTES = {
    ("GET", "/api/chat/channels"): "chat_channel_list",
    ("POST", "/api/chat/channels"): "chat_channel_create",
    ("GET", "/api/chat/channels/{id}"): "chat_channel_get",
    ("POST", "/api/chat/channels/{id}/join"): "chat_channel_join",
    ("POST", "/api/chat/channels/{id}/leave"): "chat_channel_leave",
    ("POST", "/api/chat/channels/{id}/messages"): "chat_channel_send_message",
    ("GET", "/api/chat/channels/{id}/messages"): "chat_channel_get_messages",
    (
        "POST",
        "/api/chat/channels/{id}/messages/{msg_id}/reply",
    ): "chat_channel_reply",
}


def _manifest_routes() -> list[dict]:
    manifest = json.loads((DEFAULTSPACK_ROOT / "ecosystem.json").read_text(encoding="utf-8"))
    return manifest["api_routes"]


def test_chat_channel_family_is_manifest_declared_with_security_metadata():
    routes = {
        (route["method"], route.get("path") or route.get("path_pattern")): route
        for route in _manifest_routes()
    }

    for key, function_id in CHANNEL_ROUTES.items():
        route = routes[key]
        assert route["function_id"] == function_id
        assert route["auth_mode"] == "panel_or_bearer"
        assert route["principal"] == "authenticated"
        assert isinstance(route["csrf_origin_required"], bool)
        assert route["rate_limit"] == "default_per_path"
        assert route["audit_category"].startswith("chat.channel.")
        assert route["legacy_until"] is None


def test_chat_channel_transport_routes_dispatch_without_legacy_block_fallback():
    from transport.registry import canonical_http_route_specs

    specs = {
        (spec.method, spec.pattern): spec
        for spec in canonical_http_route_specs(include_always_available=False)
    }
    for key, function_id in CHANNEL_ROUTES.items():
        spec = specs[key]
        assert spec.function_id == function_id
        assert spec.legacy_block_module == ""
        assert spec.fallback_block_module == ""
        assert spec.block_module == ""


def test_chat_channel_function_route_does_not_fall_back_to_block():
    from transport.registry import HttpRouteSpec, build_http_routes_from_specs

    calls = []

    class Server:
        def _invoke_function_route(
            self,
            function_name,
            request_data,
            path_params,
            inject,
            *,
            fallback_block_module,
        ):
            calls.append((function_name, request_data, path_params, inject, fallback_block_module))
            return {"status": "ok"}

        def _invoke_fallback_block(self, *_args, **_kwargs):
            raise AssertionError("legacy block fallback must not run")

    routes = build_http_routes_from_specs(
        Server(),
        [
            HttpRouteSpec(
                "POST",
                "/api/chat/channels/{id}/join",
                function_id="chat_channel_join",
                path_inject={"id": "id"},
            )
        ],
    )
    result = routes[0][2]({"member_id": "user-1"}, {"id": "channel-1"})

    assert result == {"status": "ok"}
    assert calls[0][0] == "chat_channel_join"
    assert calls[0][-1] == ""


def test_manifest_security_metadata_survives_pack_api_route_registration(monkeypatch):
    from core_runtime.api.router_table import APIRouteTableMixin

    class RouteTable(APIRouteTableMixin):
        _api_route_exact = {}
        _api_route_patterns = []

    monkeypatch.setattr(
        RouteTable,
        "_pack_allows_in_process_api_metadata",
        classmethod(lambda cls, pack_id, pack_info=None: True),
    )
    manifest = json.loads((DEFAULTSPACK_ROOT / "ecosystem.json").read_text(encoding="utf-8"))

    RouteTable._register_api_routes_from_manifest("defaultspack", manifest)

    entry = RouteTable._api_route_exact[("POST", "/api/chat/channels")]
    assert entry["function_id"] == "chat_channel_create"
    assert entry["auth_mode"] == "panel_or_bearer"
    assert entry["principal"] == "authenticated"
    assert entry["csrf_origin_required"] is True
    assert entry["rate_limit"] == "default_per_path"
    assert entry["audit_category"] == "chat.channel.write"
    assert entry["legacy_until"] is None


def test_all_defaultspack_legacy_routes_have_complete_allowlist_metadata():
    from transport.registry import canonical_http_route_specs, legacy_http_route_metadata

    validated = 0
    for spec in canonical_http_route_specs(include_always_available=False):
        if not spec.legacy_block_module or spec.pattern.startswith("/api/mobile/v1/"):
            continue
        metadata = legacy_http_route_metadata(spec)
        assert metadata["owner"]
        assert metadata["auth_mode"]
        assert metadata["principal"]
        assert metadata["csrf_origin"]
        assert metadata["rate_limit"]
        assert metadata["audit_category"]
        assert metadata["function_id"]
        assert metadata["legacy_until"]
        validated += 1
    assert validated > 0


def test_legacy_allowlist_guard_rejects_incomplete_security_metadata(monkeypatch):
    import transport.registry as registry

    spec = registry.HttpRouteSpec(
        "POST",
        "/api/example",
        fallback_block_module="blocks.example",
    )
    key = ("POST", "/api/example", "blocks.example")
    monkeypatch.setattr(
        registry,
        "load_legacy_http_route_allowlist",
        lambda: {key: {"owner": "example", "reason": "migration"}},
    )

    with pytest.raises(ValueError, match="metadata is incomplete"):
        registry.require_legacy_route_allowlisted(spec)
