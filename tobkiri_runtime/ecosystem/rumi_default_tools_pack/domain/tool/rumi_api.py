from __future__ import annotations

from typing import Any


def ok(data: Any = None) -> dict[str, Any]:
    """Encode this adapter's successful wire response without another Pack."""
    return {"status": "ok", "data": data}


def error(message: str, code: str = "ERROR") -> dict[str, Any]:
    """Encode a failure without changing the adapter's public envelope."""
    return {"status": "error", "error": {"code": code, "message": message}}


def run(arguments: dict[str, Any], context: dict[str, Any] | None = None):
    del context
    action = str(arguments.get("action") or "list_routes").strip()
    if action == "list_routes":
        return ok(
            {
                "routes": [],
                "count": 0,
                "dispatch": "disabled",
            }
        )
    if action == "request":
        return error(
            "Legacy HTTP routes are disabled; use a declared Host operation",
            "LEGACY_HTTP_DISABLED",
        )
    return error("unsupported action: " + action, "INVALID_ACTION")
