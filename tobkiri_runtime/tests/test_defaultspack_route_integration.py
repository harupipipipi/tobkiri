from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class _CapturedV4Broker:
    """Small Host-side catalog double for the one pinned chat operation."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Mapping[str, Any]]] = []
        self.catalog = frozenset({("conversation.turn.v1", "complete")})

    def invoke(
        self,
        frame: Any,
        context: Any,
        *,
        effect_scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((frame, context, effect_scope))
        if (frame.contract_id, frame.operation_id) not in self.catalog:
            raise RuntimeError("operation is not pinned by the active v4 plan")
        return {
            "status": "ok",
            "data": {
                "id": "assistant-1",
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            },
        }


def _install_captured_chat_session(monkeypatch: pytest.MonkeyPatch) -> _CapturedV4Broker:
    """Install only a captured Host dispatch session for transport tests."""
    from core_runtime.di_container import DIContainer
    from tobkiri_host.runtime import V4DispatchSession
    import core_runtime.di_container as di_container

    broker = _CapturedV4Broker()
    session = V4DispatchSession(
        broker=broker,  # type: ignore[arg-type]
        context_for=lambda _contract, _operation: {"source": "captured-host"},
        effect_scope_for=lambda _contract, _operation, _payload: {
            "effect": "conversation.complete"
        },
        providers={
            "conversation.turn.v1": (
                {"provider_instance_id": "defaultspack.conversation"},
            )
        },
        profile_id="profile:captured",
        plan_digest="sha256:" + "1" * 64,
    )
    broker.session = session
    container = DIContainer()
    container.set_instance("v4_dispatch_session", session)
    monkeypatch.setattr(di_container, "get_container", lambda: container)
    return broker


def test_retired_company_routes_fail_closed_before_generic_status():
    from ecosystem.defaultspack.transport.registry import (
        HttpRouteSpec,
        _FALLBACK_HTTP_ROUTE_SPECS,
        build_http_routes_from_specs,
        canonical_http_route_specs,
    )

    class Server:
        pass

    assert _FALLBACK_HTTP_ROUTE_SPECS == []
    assert canonical_http_route_specs(include_always_available=False) == []
    with pytest.raises(ValueError, match="legacy HTTP route is not allowlisted"):
        build_http_routes_from_specs(
            Server(),
            [
                HttpRouteSpec(
                    "GET",
                    "/api/company/{company_id}/status",
                    block_module="blocks.company.status",
                )
            ],
        )


def test_http_route_spec_preserves_authority_metadata_on_handler():
    from ecosystem.defaultspack.transport.registry import (
        HttpRouteSpec,
        build_http_routes_from_specs,
    )

    class Server:
        def _handle_mobile_chat(self, request_data, path_params):
            return {"status": "ok"}

    routes = build_http_routes_from_specs(
        Server(),
        [
            HttpRouteSpec(
                "POST",
                "/api/mobile/v1/chat",
                handler_name="_handle_mobile_chat",
                permission_id="mobile.chat.send",
                owner_pack_id="defaultspack",
                provider_id="rumi",
                frontend_id="mobile",
                audience="kernel_api",
                resource_template={"surface_id": "mobile", "device_id": "{body.device_id}"},
                core_only=False,
            ),
        ],
    )

    _method, _compiled, handler, _source, _path_inject = routes[0]
    assert getattr(handler, "__rumi_route_authority__") == {
        "permission_id": "mobile.chat.send",
        "owner_pack_id": "defaultspack",
        "provider_id": "rumi",
        "frontend_id": "mobile",
        "audience": "kernel_api",
        "resource_template": {"surface_id": "mobile", "device_id": "{body.device_id}"},
    }


def test_chat_http_routes_require_a_captured_v4_operation():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer
    from ecosystem.defaultspack.transport.registry import (
        _FALLBACK_HTTP_ROUTE_SPECS,
        canonical_http_route_specs,
    )

    assert _FALLBACK_HTTP_ROUTE_SPECS == []
    assert canonical_http_route_specs(include_always_available=False) == []
    server = DefaultsHttpServer(facade=None)
    for method, path in (
        ("POST", "/v1/chat/completions"),
        ("POST", "/api/chat/conversations/c1/messages"),
        ("POST", "/api/chat/conversations/c1/stream"),
    ):
        assert server._match_route(method, path) == (None, None, None, None, None)


def test_tool_selection_resource_routes_fail_closed_without_v4_catalog_entries():
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs

    specs = {(spec.method, spec.pattern): spec for spec in canonical_http_route_specs()}

    assert ("PUT", "/api/chat/conversations/{id}/tool-preferences") not in specs
    assert ("GET", "/api/tools/selection/traces/{trace_id}") not in specs


def test_browser_companion_session_route_is_not_host_registered_without_v4_operation():
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs

    specs = {(spec.method, spec.pattern): spec for spec in canonical_http_route_specs()}
    assert ("GET", "/api/tools/browser-companion/session") not in specs


def test_desktops_spa_route_is_registered_as_defaultspack_shell():
    from ecosystem.defaultspack.transport.registry import build_always_available_http_routes

    class Server:
        def __getattr__(self, name):
            if name.startswith("_handle_"):
                return self._handle_noop
            raise AttributeError(name)

        def _handle_noop(self, request_data, path_params):
            return {"status": "ok"}

        def _handle_static(self, request_data, path_params):
            return {"_static": True}

    routes = build_always_available_http_routes(Server())
    by_pattern = {
        (method, compiled.pattern): handler
        for method, compiled, handler, _source, _path_inject in routes
    }

    assert ("GET", "^/desktops$") in by_pattern
    assert by_pattern[("GET", "^/desktops$")].__name__ == "_handle_static"


def test_tool_setup_registers_browser_companion_session_route_metadata():
    from blocks.tool.setup import run

    class Registry:
        def __init__(self):
            self.entries = []

        def register(self, key, value, meta=None):
            self.entries.append((key, value, meta))

    registry = Registry()
    run({"interface_registry": registry})

    routes = {
        (entry["method"], entry["pattern"]): entry
        for key, entry, _meta in registry.entries
        if key == "io.http.route"
    }
    route = routes[("GET", "/api/tools/browser-companion/session")]

    assert route["sensitive"] is True
    assert route["local_only"] is True
    assert getattr(route["handler"], "__rumi_route_sensitive__") is True
    assert getattr(route["handler"], "__rumi_route_local_only__") is True


def test_flow_yaml_routes_do_not_bypass_the_captured_v4_catalog():
    from ecosystem.defaultspack.transport.registry import (
        canonical_http_route_specs,
        build_http_routes_from_specs,
        flow_http_route_specs,
    )

    assert canonical_http_route_specs(include_always_available=False) == []

    class Server:
        pass

    with pytest.raises(ValueError, match="legacy HTTP route is not allowlisted"):
        build_http_routes_from_specs(Server(), flow_http_route_specs())


def test_template_function_routes_require_captured_v4_catalog_entries():
    from ecosystem.defaultspack.transport.registry import (
        canonical_http_route_specs,
        template_http_route_specs,
    )

    assert canonical_http_route_specs(include_always_available=False) == []
    template_keys = {
        (spec.method, spec.pattern) for spec in template_http_route_specs()
    }
    canonical_keys = {
        (spec.method, spec.pattern) for spec in canonical_http_route_specs()
    }
    assert template_keys
    assert template_keys.isdisjoint(canonical_keys)


def test_adaptive_function_routes_are_absent_until_pinned_by_v4_plan():
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs

    canonical = {(spec.method, spec.pattern): spec for spec in canonical_http_route_specs()}
    assert not any(str(spec.function_id or "").startswith("adaptive_") for spec in canonical.values())
    assert not any("/api/onboarding" in spec.pattern for spec in canonical.values())


def test_inactive_template_function_routes_are_not_registered(tmp_path):
    import domain.function_runtime.template_specs as template_specs
    from ecosystem.defaultspack.transport.registry import template_http_route_specs

    for status in ("active", "draft", "deprecated", "disabled"):
        template_path = tmp_path / "templates" / status / "template.json"
        template_path.parent.mkdir(parents=True)
        template_path.write_text(
            json.dumps(
                {
                    "id": f"route.{status}",
                    "kind": "backend",
                    "version": "1.0.0",
                    "status": status,
                    "trust_level": "builtin",
                    "pieces": [
                        {
                            "id": "route_action",
                            "kind": "function",
                            "role": "action",
                            "action_id": f"{status}_route_action",
                            "block_module": "blocks.context.token_estimate",
                            "method": "POST",
                            "route_path": f"/api/{status}",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    template_specs._template_catalog.cache_clear()
    try:
        routes = {
            (spec.method, spec.pattern): spec
            for spec in template_http_route_specs(defaultspack_root=tmp_path)
        }
    finally:
        template_specs._template_catalog.cache_clear()

    assert ("POST", "/api/active") in routes
    assert ("POST", "/api/draft") not in routes
    assert ("POST", "/api/deprecated") not in routes
    assert ("POST", "/api/disabled") not in routes


def test_always_available_routes_include_ambient_shell():
    from ecosystem.defaultspack.transport.registry import _ALWAYS_AVAILABLE_HTTP_ROUTE_SPECS

    routes = {
        (spec.method, spec.pattern, spec.handler_name)
        for spec in _ALWAYS_AVAILABLE_HTTP_ROUTE_SPECS
    }

    assert ("GET", "/chat", "_handle_static") in routes
    assert ("GET", "/defaultspack", "_handle_static") in routes
    assert ("GET", "/pack/defaultspack", "_handle_static") in routes
    assert ("GET", "/coding", "_handle_static") in routes
    assert ("GET", "/calendar", "_handle_static") in routes
    assert ("GET", "/approval", "_handle_static") in routes
    assert ("POST", "/api/authority/browser-ui-operator", "_handle_authority_browser_ui_operator") in routes
    assert ("GET", "/ambient", "_handle_static") in routes
    assert ("GET", "/ambient-debug", "_handle_static") in routes
    assert ("GET", "/finger-recording", "_handle_static") in routes
    assert ("GET", "/console", "_handle_static") in routes
    assert ("GET", "/host-permissions", "_handle_static") in routes
    assert ("GET", "/adaptive", "_handle_static") in routes
    assert ("GET", "/operating-profile", "_handle_static") in routes


def test_calendar_route_is_spa_shell_fallback():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    assert DefaultsHttpServer._is_spa_shell_fallback_route("GET", "/calendar")
    assert DefaultsHttpServer._is_spa_shell_fallback_route("GET", "/calendar/")


def test_routes_json_transport_direct_entries_match_canonical_registry():
    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert not (DEFAULTSPACK_ROOT / "routes.json").exists()
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


def test_static_mediapipe_assets_fall_back_to_webapp_public_canonical(monkeypatch):
    from ecosystem.defaultspack.transport import http as http_transport
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    canonical_dir = DEFAULTSPACK_ROOT / "webapp" / "public" / "mediapipe" / "wasm"
    canonical_files = sorted(path.name for path in canonical_dir.iterdir() if path.is_file())
    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    real_isfile = http_transport.os.path.isfile

    def isfile_without_generated_ui(path):
        if f"{http_transport.os.sep}ui{http_transport.os.sep}" in str(path):
            return False
        return real_isfile(path)

    monkeypatch.setattr(http_transport.os.path, "isfile", isfile_without_generated_ui)

    for name in canonical_files:
        result = server._handle_static_file(
            {},
            {"path": f"mediapipe/wasm/{name}"},
        )
        assert result["_static"] is True, name
        expected_body = (
            (canonical_dir / name).read_bytes()
            if name.endswith(".wasm")
            else (canonical_dir / name).read_text(encoding="utf-8")
        )
        assert result["body"] == expected_body

    model_result = server._handle_static_file({}, {"path": "models/hand_landmarker.task"})
    assert model_result["_static"] is True
    assert model_result["content_type"] == "application/octet-stream"
    assert model_result["body"] == (
        DEFAULTSPACK_ROOT / "webapp" / "public" / "models" / "hand_landmarker.task"
    ).read_bytes()


def test_static_mediapipe_wasm_uses_browser_wasm_mime():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)

    result = server._handle_static_file(
        {},
        {"path": "mediapipe/wasm/vision_wasm_internal.wasm"},
    )

    assert result["_static"] is True
    assert result["content_type"] == "application/wasm"
    assert isinstance(result["body"], bytes)


def test_mediapipe_wasm_mirror_matches_webapp_public_canonical():
    canonical_dir = DEFAULTSPACK_ROOT / "webapp" / "public" / "mediapipe" / "wasm"
    mirror_dir = DEFAULTSPACK_ROOT / "ui" / "mediapipe" / "wasm"

    canonical_files = sorted(path.name for path in canonical_dir.iterdir() if path.is_file())
    mirror_files = sorted(path.name for path in mirror_dir.iterdir() if path.is_file())

    assert mirror_files == canonical_files
    for name in canonical_files:
        assert (mirror_dir / name).read_bytes() == (canonical_dir / name).read_bytes(), name

    canonical_model = DEFAULTSPACK_ROOT / "webapp" / "public" / "models" / "hand_landmarker.task"
    mirror_model = DEFAULTSPACK_ROOT / "ui" / "models" / "hand_landmarker.task"
    assert mirror_model.read_bytes() == canonical_model.read_bytes()


def test_pack_api_does_not_dispatch_unpinned_interface_routes():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/ui/conversations/c1/preview",
        "conversation.turn.v1",
        "complete",
    )


def test_pack_api_does_not_use_kernelless_defaultspack_block_fallback():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/chat/conversations",
        "conversation.turn.v1",
        "complete",
    )


def test_pack_api_rejects_adaptive_route_without_captured_v4_plan():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/onboarding/status",
        "conversation.turn.v1",
        "complete",
    )


def test_chat_send_transport_dispatches_through_captured_v4_operation(monkeypatch):
    from core_runtime.global_contract_dispatch import (
        captured_profile_id,
        selected_global_providers,
    )
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    broker = _install_captured_chat_session(monkeypatch)
    assert captured_profile_id(broker.session) == "profile:captured"
    assert selected_global_providers(broker.session, "conversation.turn.v1") == (
        {"provider_instance_id": "defaultspack.conversation"},
    )
    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    server._build_context = lambda: {"request_id": "req-1"}

    result = server._invoke_fallback_block(
        "blocks.chat.send",
        {"content": "hi", "model": "captured-model"},
        {"id": "c1"},
        {"id": "conversation_id"},
    )

    assert result["status"] == "ok"
    assert len(broker.calls) == 1
    frame, context, effect_scope = broker.calls[0]
    assert frame.contract_id == "conversation.turn.v1"
    assert frame.operation_id == "complete"
    assert frame.payload == {
        "model": "captured-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert context == {"source": "captured-host"}
    assert effect_scope == {"effect": "conversation.complete"}


def test_http_flow_route_requires_captured_v4_session():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    server._build_context = lambda: {"request_id": "req-1"}

    result = server._invoke_flow_route(
        "defaultspack.chat_turn",
        {"message": {"content": "hi"}},
        {"id": "c1"},
        {"id": "conversation_id"},
        fallback_block_module="blocks.chat.send",
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "V4_SESSION_UNAVAILABLE"


def test_authority_test_request_endpoint_is_disabled_by_default(monkeypatch):
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    monkeypatch.delenv("RUMI_AUTHORITY_TEST_ENDPOINT", raising=False)
    server = DefaultsHttpServer(facade=None)

    result = server._handle_authority_test_request({}, {})

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUTHORITY_TEST_DISABLED"
    assert result["_http_status"] == 404


def test_authority_test_request_endpoint_rejects_legacy_authority_probe(monkeypatch):
    import core_runtime.host_contract as host_contract
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    monkeypatch.setattr(
        host_contract,
        "host_contract_value",
        lambda key: "1" if key == "authority_test_endpoint" else "",
    )
    server = DefaultsHttpServer(facade=None)

    result = server._handle_authority_test_request(
        {
            "provider_id": "anthropic",
            "api_id": "manual-smoke",
            "model_id": "claude-test",
            "conversation_id": "conv-1",
            "stream": False,
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUTHORITY_UNAVAILABLE"
    assert "legacy authority workflow is unavailable" in result["error"]["message"]


@pytest.mark.parametrize(
    ("handler_name", "request_data", "path_params"),
    [
        ("_handle_authority_requests", {"status": "pending"}, {}),
        ("_handle_authority_request", {}, {"request_id": "auth-1"}),
        (
            "_handle_authority_approve",
            {"scope": "once"},
            {"request_id": "auth-1"},
        ),
        (
            "_handle_authority_challenge",
            {"decision": "approve"},
            {"request_id": "auth-1"},
        ),
        (
            "_handle_authority_deny",
            {"reason": "denied"},
            {"request_id": "auth-1"},
        ),
    ],
)
def test_retired_authority_compatibility_handlers_fail_closed(
    handler_name: str,
    request_data: dict[str, Any],
    path_params: dict[str, str],
) -> None:
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer(facade=None)
    handler = getattr(server, handler_name)

    result = handler(request_data, path_params)

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUTHORITY_UNAVAILABLE"
    assert "legacy authority workflow is unavailable" in result["error"]["message"]


def test_authority_test_request_route_is_not_registered():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer(facade=None)
    assert server._match_route(
        "POST",
        "/api/authority/test/request",
    ) == (None, None, None, None, None)


def test_http_chat_flow_output_requires_a_compatible_v4_message_shape():
    from ecosystem.defaultspack.transport.registry import flow_http_output_is_compatible

    assert not flow_http_output_is_compatible(
        "defaultspack.chat_turn",
        {"status": "ok", "data": {"outputs": {"ai_response": {}}}},
        fallback_block_module="blocks.chat.send",
    )
    assert flow_http_output_is_compatible(
        "defaultspack.chat_turn",
        {
            "status": "ok",
            "data": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
        fallback_block_module="blocks.chat.send",
    )


def test_http_chat_message_output_is_supplied_by_captured_v4_dispatch(monkeypatch):
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    broker = _install_captured_chat_session(monkeypatch)
    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    server._build_context = lambda: {"request_id": "req-1"}

    result = server._invoke_flow_route(
        "defaultspack.chat_turn",
        {"content": "hi"},
        {"id": "c1"},
        {"id": "conversation_id"},
        fallback_block_module="blocks.chat.send",
    )

    assert result["status"] == "ok"
    assert len(broker.calls) == 1
    frame, _context, _effect_scope = broker.calls[0]
    assert (frame.contract_id, frame.operation_id) == (
        "conversation.turn.v1",
        "complete",
    )


def test_http_chat_stream_flow_output_rejects_non_sse_shape():
    from ecosystem.defaultspack.transport.registry import flow_http_output_is_compatible

    assert not flow_http_output_is_compatible(
        "defaultspack.chat_stream_turn",
        {"status": "ok", "data": {"outputs": {"stream_result": {}}}},
        fallback_block_module="blocks.chat.stream",
    )


def test_http_chat_stream_flow_output_rejects_stringified_sse_events():
    from ecosystem.defaultspack.transport.registry import flow_http_output_is_compatible

    assert not flow_http_output_is_compatible(
        "defaultspack.chat_stream_turn",
        {
            "status": "ok",
            "data": {
                "_sse": True,
                "events": "<generator object _engine_events>",
            },
        },
        fallback_block_module="blocks.chat.stream",
    )


def test_http_chat_stream_route_fails_closed_without_captured_v4_operation():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer(facade=None)
    server._build_context = lambda: {"request_id": "req-1"}
    result = server._invoke_fallback_block(
        "blocks.chat.stream",
        {"message": {"content": "hi"}},
        {"id": "c1"},
        {"id": "conversation_id"},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "V4_OPERATION_UNAVAILABLE"


def test_stdio_chat_message_route_fails_closed_without_captured_v4_operation():
    from ecosystem.defaultspack.transport.stdio import DefaultsStdioTransport

    result = DefaultsStdioTransport()._handle_request(
        {
            "method": "POST",
            "path": "/api/chat/conversations/c1/messages",
            "data": {"message": {"content": "hi"}},
        }
    )

    assert result["status"] == "error"
    assert result["error"]["message"] == (
        "not found: POST /api/chat/conversations/c1/messages"
    )


def test_cli_direct_send_message_uses_captured_v4_dispatch(monkeypatch):
    from ecosystem.defaultspack.transport.cli import DirectBackend

    broker = _install_captured_chat_session(monkeypatch)
    result = DirectBackend().send_message({"content": "hi"})

    assert result["status"] == "ok"
    assert len(broker.calls) == 1
    frame, _context, _effect_scope = broker.calls[0]
    assert (frame.contract_id, frame.operation_id) == (
        "conversation.turn.v1",
        "complete",
    )


def test_live_registry_company_routes_are_ignored_by_host_transport():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    class Facade:
        def get_interface(self, key, strategy=None):
            raise AssertionError("Host transport must not read a live route registry")

    server = DefaultsHttpServer(Facade())
    assert server._match_route("GET", "/api/agent/company/status") == (
        None,
        None,
        None,
        None,
        None,
    )


def test_live_registry_chat_send_route_is_not_a_v4_dispatch_source():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    class Facade:
        def get_interface(self, key, strategy=None):
            raise AssertionError("Host transport must not read a live route registry")

    server = DefaultsHttpServer(Facade())
    assert server._match_route("POST", "/api/chat/conversations/c1/messages") == (
        None,
        None,
        None,
        None,
        None,
    )


def test_registry_chat_flow_handler_keeps_path_params_through_http_dispatch_shape():
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )

    assert_retired_module_absent("core_runtime.interface_registry")
    assert_profile_resolver_requires_authority_snapshot()


def test_live_registry_chat_stream_route_is_not_a_v4_dispatch_source():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    class Facade:
        def get_interface(self, key, strategy=None):
            raise AssertionError("Host transport must not read a live route registry")

    server = DefaultsHttpServer(Facade())
    assert server._match_route("POST", "/api/chat/conversations/c1/stream") == (
        None,
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/agent/companies/acme/status"),
        ("GET", "/api/company/acme/status"),
        ("PUT", "/api/agent/companies/acme/agents/bot"),
        ("POST", "/api/integrations/p2p/events"),
        ("POST", "/api/p2p/messages/send"),
        ("POST", "/api/connections/import"),
        ("DELETE", "/api/p2p/peers/peer-a"),
        ("POST", "/api/chat/conversations/c1/compact"),
        ("GET", "/api/coding/workspaces/ws1"),
        ("POST", "/api/coding/workspaces/ws1/trust"),
    ],
)
def test_retired_noncore_routes_fail_closed(
    method: str,
    path: str,
):
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer(facade=None)
    assert server._match_route(method, path) == (None, None, None, None, None)


def test_fallback_specs_are_empty_after_v4_cutover():
    from ecosystem.defaultspack.transport.registry import _FALLBACK_HTTP_ROUTE_SPECS

    assert _FALLBACK_HTTP_ROUTE_SPECS == []


def test_p2p_pre_auth_only_exposes_signed_integration_event():
    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


def test_routes_json_documents_new_route_groups():
    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert not (DEFAULTSPACK_ROOT / "routes.json").exists()
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()
