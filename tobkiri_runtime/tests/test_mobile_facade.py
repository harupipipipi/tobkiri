from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_mobile_bootstrap_returns_server_and_capability_flags():
    from blocks.mobile.bootstrap import run

    result = run({}, None)
    assert result["status"] == "ok"
    data = result["data"]
    assert "server" in data
    assert {"device_id", "label", "version"} <= set(data["server"])
    caps = data["capabilities"]
    assert caps["chat"] is True
    assert caps["tools"] is True
    assert caps["credential_transfer"] is False
    assert "cursor" in data


def test_mobile_manifest_exposes_facade_without_authority_routes():
    from blocks.mobile.manifest import run

    result = run({}, None)
    assert result["status"] == "ok"
    data = result["data"]
    assert data["kind"] == "tobkiri_mobile_manifest_v1"
    routes = data["routes"]
    assert routes
    assert all(route["path"].startswith("/api/mobile/v1/") for route in routes)
    assert all(not route["path"].startswith("/api/authority/") for route in routes)
    assert all(not str(route.get("feature", "")).endswith("_admin") for route in routes)
    assert data["authority_routes"] == []
    assert "mobile_client" in data["token_roles"]
    assert "mobile_approver" in data["token_roles"]
    assert data["token_roles"]["mobile_client"]["scopes"] == [
        "chat.read",
        "chat.write",
        "tools.observe",
        "tools.invoke.basic",
        "tools.invoke.cloud",
    ]
    assert data["capabilities"]["tool_invoke"] is True
    assert data["capabilities"]["cloud_delegation"] is True
    routes_by_path = {route["path"]: route for route in routes}
    assert routes_by_path["/api/mobile/v1/tools/invoke"]["device_scope"] == "tools.invoke.basic"
    cloud_route = routes_by_path["/api/mobile/v1/cloud/tools/invoke"]
    assert cloud_route["device_scope"] == "tools.invoke.cloud"
    assert cloud_route["defaults"]["execution_route"] == "cloud"
    assert cloud_route["defaults"]["execution_provider"] == "cloudflare_sandbox_bridge"


def test_mobile_capabilities_returns_provider_and_model_catalogs():
    from blocks.mobile.capabilities import run

    result = run({}, None)
    assert result["status"] == "ok"
    data = result["data"]
    assert len(data["providers"]) > 0
    assert len(data["models"]) > 0

    provider = data["providers"][0]
    assert "provider_id" in provider
    assert "display_name" in provider
    assert "configured" in provider
    # Secrets must not leak: only env var names, never values.
    assert all(isinstance(name, str) for name in provider.get("env_vars", []))
    assert "api_key" not in str(provider).lower() or provider.get("configured_api_count") is not None

    model = data["models"][0]
    assert "id" in model
    assert "provider_id" in model
    assert "model_id" in model
    assert "max_context" in model

    assert "runtime" in data
    assert "preferred_model" in data["runtime"]
    assert "commands" in data
    assert any(command["name"] == "model" for command in data["commands"])
    assert data["agent_template"]["template_id"] == "rumi.composer.default"
    assert "tools" in data
    assert "tool_summary" in data
    if data["tools"]:
        assert "mobile_compatible" in data["tools"][0]
        assert "mobile" in data["tools"][0]


def test_mobile_tools_endpoint_tags_compatible_tools():
    from blocks.mobile.tools import run

    result = run({}, None)
    assert result["status"] == "ok"
    data = result["data"]
    assert data["agent_template"]["template_id"] == "rumi.composer.default"
    assert "tools" in data
    compatible = [tool for tool in data["tools"] if tool.get("mobile_compatible")]
    assert data["summary"]["compatible_count"] == len(compatible)
    if compatible:
        assert "mobile-compatible" in compatible[0]["tags"]


def test_mobile_tools_invoke_dispatches_defaultspack_tool_function(monkeypatch):
    import blocks.mobile.tools as mobile_tools

    calls = {}

    def fake_invoke(function_id, args, context, principal_id="defaultspack"):
        calls["function_id"] = function_id
        calls["args"] = args
        calls["context"] = context
        calls["principal_id"] = principal_id
        return {"status": "ok", "data": {"answer": 42}}

    monkeypatch.setattr(
        mobile_tools,
        "invoke_defaultspack_function",
        fake_invoke,
    )

    result = mobile_tools.run(
        {
            "action": "invoke",
            "tool_name": "tool_calculator",
            "arguments": {"expression": "6 * 7"},
        },
        {"conversation_id": "c1"},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["tool_name"] == "tool_calculator"
    assert data["function_id"] == "tool_calculator"
    assert data["result"] == {"answer": 42}
    assert data["execution_location"] == "pc"
    assert calls["function_id"] == "tool_calculator"
    assert calls["args"] == {"expression": "6 * 7"}
    assert calls["context"]["_mobile_tool_delegate"] is True
    assert calls["principal_id"] == "defaultspack"


def test_mobile_tools_invoke_dispatches_tool_registry_id(monkeypatch):
    import blocks.mobile.tools as mobile_tools

    calls = {}

    def fake_invoke_tool(payload, context):
        calls["payload"] = payload
        calls["context"] = context
        return {
            "status": "ok",
            "data": {
                "tool_name": "python_exec",
                "result": "1",
                "is_error": False,
            },
        }

    monkeypatch.setattr(mobile_tools, "invoke_tool", fake_invoke_tool)

    result = mobile_tools.run(
        {
            "action": "invoke",
            "tool_name": "python_exec",
            "arguments": {"code": "print(1)"},
        },
        {},
    )

    assert result["status"] == "ok"
    assert calls["payload"]["tool_name"] == "python_exec"
    assert calls["payload"]["arguments"] == {"code": "print(1)"}
    assert calls["context"]["_mobile_tool_delegate"] is True


def test_mobile_cloud_tools_invoke_rejects_pc_local_tool():
    import blocks.mobile.tools as mobile_tools

    result = mobile_tools.run(
        {
            "action": "invoke",
            "execution_route": "cloud",
            "tool_name": "desktop_input",
            "arguments": {"action": "click"},
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "PC_BRIDGE_REQUIRED"
    assert result["error"]["details"]["cloudflare"]["route"] == "pc_bridge_required"
    assert result["error"]["details"]["cloudflare"]["reason"] == "pc_local_surface"


def test_mobile_cloud_tools_invoke_injects_cloudflare_provider(monkeypatch):
    import blocks.mobile.tools as mobile_tools

    calls = {}

    def fake_invoke_tool(payload, context):
        calls["payload"] = payload
        calls["context"] = context
        return {
            "status": "ok",
            "data": {
                "tool_name": "sandbox_exec",
                "result": "ok",
                "is_error": False,
            },
        }

    monkeypatch.setattr(mobile_tools, "invoke_tool", fake_invoke_tool)

    result = mobile_tools.run(
        {
            "action": "invoke",
            "execution_route": "cloud",
            "tool_name": "sandbox_exec",
            "arguments": {"argv": ["python", "-V"]},
        },
        {},
    )

    assert result["status"] == "ok"
    assert calls["payload"]["tool_name"] == "sandbox_exec"
    assert calls["payload"]["arguments"]["argv"] == ["python", "-V"]
    assert calls["payload"]["arguments"]["provider_id"] == "cloudflare_sandbox_bridge"
    assert calls["context"]["_mobile_tool_delegate"] is True
    assert calls["context"]["_mobile_cloud_tool_delegate"] is True


def test_mobile_capabilities_provider_filter_narrows_models():
    from blocks.mobile.capabilities import run

    all_models = run({}, None)["data"]["models"]
    if not all_models:
        return
    provider_id = all_models[0]["provider_id"]
    filtered = run({"provider": provider_id}, None)["data"]["models"]
    assert filtered
    assert all(m["provider_id"] == provider_id for m in filtered)


def test_mobile_capabilities_query_param_provider_filter():
    from blocks.mobile.capabilities import run

    all_models = run({}, None)["data"]["models"]
    if not all_models:
        return
    provider_id = all_models[0]["provider_id"]
    filtered = run({"query_params": {"provider": provider_id}}, None)["data"]["models"]
    assert filtered
    assert all(m["provider_id"] == provider_id for m in filtered)


def test_mobile_capabilities_include_templates_flag():
    from blocks.mobile.capabilities import run

    with_templates = run({"include_templates": True}, None)["data"]
    without = run({"include_templates": False}, None)["data"]
    assert "templates" in with_templates
    assert "templates" in without
    assert without["templates"] == []


def test_mobile_capabilities_no_secret_values_in_provider_entries():
    from blocks.mobile.capabilities import run

    providers = run({}, None)["data"]["providers"]
    blob = str(providers)
    # Active HMAC keys / API key values must never appear in catalog payloads.
    forbidden = ["sk-", "Bearer ", "hmac_secret"]
    for token in forbidden:
        assert token not in blob, f"unexpected secret token in catalog: {token}"


def test_mobile_capabilities_provider_summary_has_openai_compatible_flag():
    from blocks.mobile.capabilities import run

    providers = run({}, None)["data"]["providers"]
    assert any("openai_compatible" in p for p in providers)


def test_mobile_route_contract_is_reflected_in_registry():
    from domain.mobile.contract import iter_mobile_route_contracts
    from transport.registry import canonical_http_route_specs

    specs = {
        (spec.method, spec.pattern): spec
        for spec in canonical_http_route_specs(include_always_available=False)
    }

    for route in iter_mobile_route_contracts():
        spec = specs.get((route.method, route.pattern))
        assert spec is not None, f"missing mobile route spec: {route.method} {route.pattern}"
        assert spec.block_module == route.block_module
        assert spec.flow_id == route.flow_id
        assert spec.fallback_block_module == (
            route.fallback_block_module or route.block_module
        )
        if route.feature.endswith("_admin"):
            assert spec.sensitive is True
            assert spec.local_only is True


def test_mobile_route_contract_legacy_routes_build_fallback_table():
    from domain.mobile.contract import iter_mobile_route_contracts
    from transport.registry import build_fallback_http_routes

    class _Server:
        def __getattr__(self, name):
            if name.startswith("_handle_"):
                return lambda _request_data, _path_params: {"status": "ok"}
            raise AttributeError(name)

        def _invoke_fallback_block(self, *_args, **_kwargs):
            return {"status": "ok"}

        def _invoke_flow_route(self, *_args, **_kwargs):
            return {"status": "ok"}

        def _invoke_function_route(self, *_args, **_kwargs):
            return {"status": "ok"}

    routes = build_fallback_http_routes(_Server())
    compiled_routes = {(method, pattern.pattern) for method, pattern, *_ in routes}

    for route in iter_mobile_route_contracts():
        assert (
            route.method,
            _compiled_pattern_for(route.pattern),
        ) in compiled_routes, (
            "mobile fallback route is not buildable: "
            f"{route.method} {route.pattern}"
        )


def test_mobile_pc_equivalent_routes_exist_for_parity_guard():
    from domain.mobile.contract import iter_mobile_route_contracts
    from transport.registry import canonical_http_route_specs

    specs = {
        f"{spec.method} {spec.pattern}"
        for spec in canonical_http_route_specs(include_always_available=False)
    }
    equivalents = [
        route.pc_equivalent
        for route in iter_mobile_route_contracts()
        if route.pc_equivalent
    ]

    assert equivalents
    for equivalent in equivalents:
        assert equivalent in specs


def test_mobile_contract_covers_pc_conversation_routes():
    from domain.mobile.contract import iter_mobile_route_contracts
    from transport.registry import canonical_http_route_specs

    pc_conversation_routes = {
        f"{spec.method} {spec.pattern}"
        for spec in canonical_http_route_specs(include_always_available=False)
        if spec.pattern.startswith("/api/chat/conversations")
    }
    mobile_equivalents = {
        route.pc_equivalent
        for route in iter_mobile_route_contracts()
        if route.pc_equivalent
    }

    assert pc_conversation_routes <= mobile_equivalents


def test_mobile_device_scope_contract_blocks_unknown_routes():
    from domain.mobile.contract import required_device_scope

    assert required_device_scope("GET", "/api/mobile/v1/conversations") == "chat.read"
    assert (
        required_device_scope(
            "POST",
            "/api/mobile/v1/conversations/convo-1/stream",
        )
        == "chat.write"
    )
    assert (
        required_device_scope("POST", "/api/mobile/v1/commands/execute")
        == "chat.write"
    )
    assert required_device_scope("GET", "/api/mobile/v1/tools") == "tools.observe"
    assert required_device_scope("POST", "/api/mobile/v1/tools/invoke") == "tools.invoke.basic"
    assert (
        required_device_scope("POST", "/api/mobile/v1/cloud/tools/invoke")
        == "tools.invoke.cloud"
    )
    assert required_device_scope("GET", "/api/packs") == ""
    assert required_device_scope("GET", "/api/mobile/v1/approvals") == ""
    assert required_device_scope("POST", "/api/mobile/v1/approvals/auth_1/approve") == ""
    assert required_device_scope("GET", "/api/mobile/v1/pairings/pair-1/review") == ""
    assert required_device_scope("POST", "/api/mobile/v1/pairings/pair-1/approve") == ""
    assert required_device_scope("POST", "/api/mobile/v1/pairings/pair-1/reject") == ""


def test_mobile_manifest_does_not_expose_legacy_approval_facade():
    from blocks.mobile.manifest import run
    from domain.mobile.contract import iter_mobile_route_contracts

    contract_paths = {route.pattern for route in iter_mobile_route_contracts()}
    assert "/api/mobile/v1/approvals" not in contract_paths
    assert "/api/mobile/v1/approvals/{id}/approve" not in contract_paths
    assert "/api/mobile/v1/approvals/{id}/deny" not in contract_paths

    manifest = run({}, None)["data"]
    route_paths = {route["path"] for route in manifest["routes"]}
    assert "/api/mobile/v1/approvals" not in route_paths
    assert not manifest["authority_routes"]


def test_mobile_commands_execute_uses_pc_slash_registry():
    from blocks.mobile.commands import run

    result = run({"command": "model", "args": {}, "mode": "chat"}, None)
    assert result["status"] == "ok"
    data = result["data"]
    assert data["command"]["name"] == "model"
    assert data["action"] == "open_model_picker"


def _compiled_pattern_for(pattern: str) -> str:
    import re

    compiled = re.sub(
        r"\{(\w+)\}",
        lambda match: rf"(?P<{match.group(1)}>[^/]+)",
        pattern,
    )
    return f"^{compiled}$"
