"""Register UI contract HTTP routes for defaultspack."""

from __future__ import annotations

import os
import sys


def _lazy(module_path: str, func_name: str = "run"):
    def handler(request_data, context):
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, func_name)(request_data, context)

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
        ("GET", "/api/ui/settings", _lazy("blocks.ui.settings"), {}),
        ("PUT", "/api/ui/settings", _lazy("blocks.ui.settings"), {}),
        ("GET", "/api/ui/provider-health", _lazy("blocks.ui.provider_health"), {}),
        ("GET", "/api/connections/codex", _lazy("blocks.connections.codex"), {}),
        ("POST", "/api/connections/codex", _lazy("blocks.connections.codex"), {}),
        (
            "GET",
            "/api/connections/openai-compatible",
            _lazy("blocks.connections.openai_compatible"),
            {},
        ),
        (
            "POST",
            "/api/connections/openai-compatible",
            _lazy("blocks.connections.openai_compatible"),
            {},
        ),
        ("GET", "/api/ui/commands", _lazy("blocks.ui.commands"), {}),
        ("POST", "/api/ui/commands/execute", _lazy("blocks.ui.commands"), {}),
        ("POST", "/api/ui/clipboard", _lazy("blocks.ui.clipboard"), {}),
        ("POST", "/api/ui/client-events", _lazy("blocks.ui.client_events"), {}),
        ("POST", "/api/ui/compile-plan", _lazy("blocks.ui.compile_plan"), {}),
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
