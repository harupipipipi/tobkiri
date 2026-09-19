from __future__ import annotations

import os
import sys
from typing import Any


def run(context: dict[str, Any]):
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pack_root not in sys.path:
        sys.path.insert(0, pack_root)

    interface_registry = context["interface_registry"]
    source_component = context.get("_source_component", "defaultspack:ambient:ambient")

    routes = [
        ("GET", "/api/ambient/status", "blocks.ambient.status", {}),
        ("POST", "/api/ambient/monitor/start", "blocks.ambient.monitor", {"action": "start"}),
        ("POST", "/api/ambient/monitor/stop", "blocks.ambient.monitor", {"action": "stop"}),
        ("POST", "/api/ambient/config", "blocks.ambient.config", {}),
        ("POST", "/api/ambient/events", "blocks.ambient.event_submit", {}, False),
        ("POST", "/api/ambient/transcriptions", "blocks.ambient.transcription", {}, True),
        ("POST", "/api/ambient/permissions/grant", "blocks.ambient.permissions", {"action": "grant"}),
        ("POST", "/api/ambient/permissions/revoke", "blocks.ambient.permissions", {"action": "revoke"}),
        ("POST", "/api/ambient/permissions/check", "blocks.ambient.permissions", {"action": "check_os"}),
    ]

    for route in routes:
        if len(route) == 4:
            method, pattern, module_path, defaults = route
            local_only = False
        else:
            method, pattern, module_path, defaults, local_only = route
        interface_registry.register(
            "io.http.route",
            {
                "method": method,
                "pattern": pattern,
                "handler": _ambient_handler(module_path, defaults),
                "path_inject": {},
                "local_only": local_only,
            },
            meta={"_source_component": source_component},
        )

    return {"status": "ok", "registered": [route[1] for route in routes]}


def _ambient_handler(module_path: str, defaults: dict[str, Any]):
    def handler(request_data, context):
        import importlib

        payload = dict(request_data or {})
        payload.update({key: value for key, value in defaults.items() if key not in payload})
        mod = importlib.import_module(module_path)
        return mod.run(payload, context)

    return handler
