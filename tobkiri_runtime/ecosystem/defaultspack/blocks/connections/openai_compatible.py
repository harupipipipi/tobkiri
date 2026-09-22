"""Authenticated Settings actions for saved OpenAI-compatible endpoints."""

from __future__ import annotations

from typing import Any

from blocks._common import error, ok
from domain.ai_client.openai_compatible_connections import (
    connection_status,
    delete_connection,
    get_connection,
    save_connection,
    save_connection_auth,
    select_connection,
)


def _refresh_runtime() -> None:
    """Refresh the selected endpoint without trusting any client-side state."""
    from domain.ai_client.client import AIClient

    AIClient().refresh_openai_compatible_connections()


def _saved_credential_exists(connection_id: str) -> bool:
    """Return whether a settings-safe status carries a stored credential."""
    return any(
        item.get("connection_id") == connection_id
        and bool(item.get("credential_configured"))
        for item in connection_status().get("connections", [])
        if isinstance(item, dict)
    )


def _save(payload: dict[str, Any]) -> dict[str, Any]:
    definition = payload.get("connection")
    if not isinstance(definition, dict):
        raise ValueError("connection definition is required")
    raw_id = str(definition.get("connection_id") or "").strip().lower()
    existing = get_connection(raw_id) if raw_id else None
    api_key = str(payload.get("api_key") or "")
    auth_mode = str(definition.get("auth_mode") or "none").strip().lower()
    existing_credential = bool(raw_id and _saved_credential_exists(raw_id))
    env_reference = str(definition.get("api_key_env") or "").strip()
    if auth_mode != "none" and not (api_key or existing_credential or env_reference):
        raise ValueError("an API key or existing credential is required")

    saved = save_connection(definition)
    connection_id = str(saved["connection_id"])
    if api_key:
        save_connection_auth(
            connection_id,
            api_key,
            username=str(payload.get("username") or ""),
        )
    elif auth_mode == "none" and existing is not None:
        from domain.ai_client.openai_compatible_connections import delete_connection_auth

        delete_connection_auth(connection_id)
    select = bool(payload.get("select", existing is None))
    if select:
        select_connection(connection_id)
    _refresh_runtime()
    return connection_status()


def run(input_data: Any, context: Any) -> dict[str, Any]:
    """List and mutate secret-safe connection definitions after route auth."""
    del context
    payload = input_data if isinstance(input_data, dict) else {}
    method = str(payload.get("_method") or "GET").upper()
    if method == "GET":
        return ok(connection_status())
    if method != "POST":
        return error("unsupported method", "METHOD_NOT_ALLOWED")

    action = str(payload.get("action") or "save").strip().lower()
    try:
        if action == "save":
            return ok(_save(payload))
        if action == "select":
            select_connection(str(payload.get("connection_id") or ""))
            _refresh_runtime()
            return ok(connection_status())
        if action == "delete":
            delete_connection(str(payload.get("connection_id") or ""))
            _refresh_runtime()
            return ok(connection_status())
    except (KeyError, RuntimeError, TypeError, ValueError):
        return error("OpenAI-compatible connection could not be saved", "CONNECTION_SAVE_FAILED")
    return error("unsupported connection action", "INVALID_CONNECTION_ACTION")
