"""Register UI contract HTTP routes for defaultspack."""

from __future__ import annotations

import os
import sys


def _lazy(
    module_path: str,
    func_name: str = "run",
    *,
    sensitive: bool = False,
    local_only: bool = False,
):
    def handler(request_data, context):
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, func_name)(request_data, context)

    handler.__rumi_route_sensitive__ = sensitive
    handler.__rumi_route_local_only__ = local_only
    return handler


def _static_shell(request_data, context):
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    shell_path = os.path.join(pack_root, "ui", "shell.html")
    if os.path.isfile(shell_path):
        with open(shell_path, "r", encoding="utf-8") as f:
            body = f.read()
        ui_dir = os.path.dirname(shell_path)
        for asset_name in ("shell-app.css", "shell-app.js"):
            asset_path = os.path.join(ui_dir, asset_name)
            if os.path.isfile(asset_path):
                version = str(int(os.path.getmtime(asset_path)))
                body = body.replace(f"/static/{asset_name}", f"/static/{asset_name}?v={version}")
        return {"_static": True, "content_type": "text/html; charset=utf-8", "body": body}
    return {
        "_static": True,
        "content_type": "text/html; charset=utf-8",
        "body": "<!DOCTYPE html><html><body><h1>defaults pack</h1><p>shell.html not found</p></body></html>",
    }


def _authority_browser_ui_operator(request_data, context):
    del context
    from transport.http import DefaultsHttpServer

    return DefaultsHttpServer(None)._handle_authority_browser_ui_operator(request_data, {})


def run(context):
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pack_root not in sys.path:
        sys.path.insert(0, pack_root)

    interface_registry = context["interface_registry"]
    source_component = context.get("_source_component", "defaultspack:frontend:ui")
    routes = [
        ("GET", "/api/ui/catalog", _lazy("blocks.ui.catalog"), {}),
        (
            "POST",
            "/api/ui/capability/invoke",
            _lazy(
                "blocks.ui.frontend_capability",
                sensitive=True,
                local_only=True,
            ),
            {},
        ),
        ("GET", "/api/ui/settings", _lazy("blocks.ui.settings"), {}),
        ("PUT", "/api/ui/settings", _lazy("blocks.ui.settings"), {}),
        ("GET", "/api/ui/provider-health", _lazy("blocks.ui.provider_health"), {}),
        ("GET", "/api/connections/codex", _lazy("blocks.connections.codex"), {}),
        ("POST", "/api/connections/codex", _lazy("blocks.connections.codex"), {}),
        ("GET", "/api/ui/commands", _lazy("blocks.ui.commands"), {}),
        ("POST", "/api/ui/commands/execute", _lazy("blocks.ui.commands"), {}),
        ("GET", "/api/command-protocol/v1/catalog", _lazy("blocks.ui.command_protocol_catalog"), {}),
        ("POST", "/api/command-protocol/v1/invoke", _lazy("blocks.ui.command_protocol_invoke"), {}),
        (
            "POST",
            "/api/command-protocol/v1/invocations/events/query",
            _lazy("blocks.ui.command_protocol_events"),
            {},
        ),
        (
            "GET",
            "/api/command-protocol/v1/invocations/{invocation_id}/events",
            _lazy("blocks.ui.command_protocol_stream", sensitive=True),
            {"invocation_id": "invocation_id"},
        ),
        (
            "POST",
            "/api/command-protocol/v1/offline",
            _lazy("blocks.ui.command_protocol_offline"),
            {},
        ),
        (
            "POST",
            "/api/command-protocol/v1/resume",
            _lazy(
                "blocks.ui.command_protocol_resume",
                sensitive=True,
            ),
            {},
        ),
        ("POST", "/api/command-protocol/v1/states/query", _lazy("blocks.ui.command_protocol_states"), {}),
        ("POST", "/api/command-protocol/v1/datasources/query", _lazy("blocks.ui.command_protocol_datasources"), {}),
        ("POST", "/api/ui/clipboard", _lazy("blocks.ui.clipboard"), {}),
        ("POST", "/api/ui/client-events", _lazy("blocks.ui.client_events"), {}),
        ("POST", "/api/ui/compile-plan", _lazy("blocks.ui.compile_plan"), {}),
        (
            "GET",
            "/isolated/packs/{pack_id}/{path}",
            _lazy("blocks.ui.isolated_pack_asset", local_only=True),
            {"pack_id": "pack_id", "path": "asset_path"},
        ),
        (
            "GET",
            "/api/ui/conversations/{id}/preview",
            _lazy("blocks.ui.conversation_preview"),
            {"id": "conversation_id"},
        ),
        ("POST", "/api/ui/select-directory", _lazy("blocks.ui.select_directory"), {}),
        ("GET", "/chat", _static_shell, {}),
        ("GET", "/defaultspack", _static_shell, {}),
        ("GET", "/pack/defaultspack", _static_shell, {}),
        ("GET", "/desktops", _static_shell, {}),
        ("GET", "/approval", _static_shell, {}),
        ("POST", "/api/authority/browser-ui-operator", _authority_browser_ui_operator, {}),
        ("GET", "/ambient", _static_shell, {}),
        ("GET", "/ambient-debug", _static_shell, {}),
        ("GET", "/finger-recording", _static_shell, {}),
        ("GET", "/console", _static_shell, {}),
        ("GET", "/host-permissions", _static_shell, {}),
    ]
    try:
        from ecosystem.defaultspack.domain.frontend.host import build_frontend_catalog
        from core_runtime.resolved_profile_scope import active_resolved_profile

        plan = active_resolved_profile()
        if plan is not None and any(
            item.kind == "route" and item.route == "/prompts"
            for item in build_frontend_catalog(plan).contributions
        ):
            routes.append(("GET", "/prompts", _static_shell, {}))
    except Exception:
        pass

    for method, pattern, handler, path_inject in routes:
        interface_registry.register(
            "io.http.route",
            {
                "method": method,
                "pattern": pattern,
                "handler": handler,
                "path_inject": path_inject,
            },
            meta={"_source_component": source_component},
        )

    return {"status": "ok", "registered": [route[1] for route in routes]}
