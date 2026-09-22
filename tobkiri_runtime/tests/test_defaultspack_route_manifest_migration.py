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
    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert not (DEFAULTSPACK_ROOT / "routes.json").exists()
    assert_profile_resolver_requires_authority_snapshot()


def test_chat_channel_requires_captured_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/chat/channels",
        "tobkiri.chat-channel.v1",
        "defaultspack.chat-channel.list",
    )


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
    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert_profile_resolver_requires_authority_snapshot()


def test_all_defaultspack_legacy_routes_are_explicitly_rejected():
    from ecosystem.defaultspack.transport.registry import (
        _FALLBACK_HTTP_ROUTE_SPECS,
        load_legacy_http_route_allowlist,
    )

    assert _FALLBACK_HTTP_ROUTE_SPECS == []
    assert load_legacy_http_route_allowlist() == {}


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
