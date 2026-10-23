"""Register Kanban HTTP routes for defaultspack."""

from __future__ import annotations

import os
import sys
from typing import Any


def run(context: dict[str, Any]):
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pack_root not in sys.path:
        sys.path.insert(0, pack_root)

    interface_registry = context["interface_registry"]
    source_component = context.get("_source_component", "defaultspack:kanban:kanban")

    routes = [
        ("GET", "/api/kanban/boards", "list_boards", {}),
        ("POST", "/api/kanban/boards/bootstrap", "bootstrap_board", {}),
        (
            "POST",
            "/api/kanban/boards/{board_id}/migrate",
            "migrate_board",
            {"board_id": "board_id"},
        ),
        ("GET", "/api/kanban/boards/{board_id}", "get_board", {"board_id": "board_id"}),
        ("PUT", "/api/kanban/boards/{board_id}", "update_board", {"board_id": "board_id"}),
        ("POST", "/api/kanban/boards/{board_id}/cards", "create_card", {"board_id": "board_id"}),
        ("POST", "/api/kanban/boards/{board_id}/columns", "create_column", {"board_id": "board_id"}),
        ("POST", "/api/kanban/boards/{board_id}/sync-runs", "sync_runs", {"board_id": "board_id"}),
        (
            "POST",
            "/api/kanban/boards/{board_id}/import-conversation",
            "import_conversation",
            {"board_id": "board_id"},
        ),
        ("PUT", "/api/kanban/cards/{card_id}", "update_card", {"card_id": "card_id"}),
        ("DELETE", "/api/kanban/cards/{card_id}", "delete_card", {"card_id": "card_id"}),
        ("POST", "/api/kanban/cards/{card_id}/move", "move_card", {"card_id": "card_id"}),
        ("POST", "/api/kanban/cards/{card_id}/agent/start", "agent_start", {"card_id": "card_id"}),
        ("GET", "/api/kanban/cards/{card_id}/agent/status", "agent_status", {"card_id": "card_id"}),
        ("POST", "/api/kanban/cards/{card_id}/agent/ready", "agent_ready", {"card_id": "card_id"}),
        ("POST", "/api/kanban/cards/{card_id}/agent/apply", "agent_apply", {"card_id": "card_id"}),
        ("POST", "/api/kanban/cards/{card_id}/agent/dismiss", "agent_dismiss", {"card_id": "card_id"}),
        ("PUT", "/api/kanban/columns/{column_id}", "update_column", {"column_id": "column_id"}),
        ("DELETE", "/api/kanban/columns/{column_id}", "delete_column", {"column_id": "column_id"}),
    ]

    for method, pattern, action, path_inject in routes:
        interface_registry.register(
            "io.http.route",
            {
                "method": method,
                "pattern": pattern,
                "handler": _kanban_handler(action),
                "path_inject": path_inject,
            },
            meta={"_source_component": source_component},
        )

    return {"status": "ok", "registered": [route[1] for route in routes]}


def _kanban_handler(action: str):
    def handler(request_data, context):
        import importlib

        payload = dict(request_data or {})
        payload["action"] = action
        mod = importlib.import_module("blocks.kanban.api")
        return mod.run(payload, context)

    return handler
