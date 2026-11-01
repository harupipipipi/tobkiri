from __future__ import annotations

import os
from pathlib import Path
from typing import Any


_CODEX_TOKEN_KEY = "RUMICODEX_ACCESS_TOKEN"
_CODEX_TOKEN_ENV_KEYS = ("RUMI_CODEX_ACCESS_TOKEN", "CODEX_ACCESS_TOKEN")
_CODEX_ACCESS_MATERIAL_TYPE = "access_token"
_DEFAULT_CONNECTION_ID = "default"


def _codex_auth_methods(*, access_token_configured: bool = False, app_server_configured: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "id": "chatgpt_account",
            "label": "ChatGPT account via Codex App Server",
            "credential_kind": "chatgpt_account",
            "configured": app_server_configured,
            "secret_material": False,
        },
        {
            "id": "codex_access_token",
            "label": "Codex access token",
            "credential_kind": "codex_access_token",
            "configured": access_token_configured,
            "secret_material": True,
        },
        {
            "id": "app_server_secret",
            "label": "Codex App Server secret",
            "credential_kind": "codex_app_server_secret",
            "configured": app_server_configured,
            "secret_material": True,
        },
    ]


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _secrets_dir(pack_root: Path | None = None) -> Path:
    if pack_root is None:
        configured_override = os.getenv("RUMI_DEFAULTSPACK_SECRETS_DIR", "").strip()
        if configured_override:
            return Path(configured_override).expanduser()
        configured_user_data = os.getenv("RUMI_USER_DATA", "").strip()
        if configured_user_data:
            return Path(configured_user_data).expanduser() / "secrets"
    return (pack_root or _pack_root()) / "user_data" / "secrets"


def _get_store(pack_root: Path | None = None):
    from core_runtime.secrets_store import SecretsStore

    return SecretsStore(str(_secrets_dir(pack_root)))


def _read_secret_value(pack_root: Path | None = None) -> str:
    bundle = _read_codex_token_bundle(pack_root=pack_root)
    credentials = bundle.get("credentials") if isinstance(bundle.get("credentials"), dict) else {}
    bundle_value = str(credentials.get("access_token") or credentials.get("token") or "").strip()
    if bundle_value:
        return bundle_value
    try:
        legacy_value = str(
            _get_store(pack_root)._internal_read_value(
                _CODEX_TOKEN_KEY,
                caller_id="defaultspack.codex:access_token",
            )
            or ""
        ).strip()
    except Exception:
        legacy_value = ""
    if legacy_value:
        return legacy_value
    return ""


def _read_codex_token_bundle(*, pack_root: Path | None = None) -> dict[str, Any]:
    from domain.connections.store import read_connection_credential

    return read_connection_credential(
        "codex",
        _CODEX_ACCESS_MATERIAL_TYPE,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _save_codex_token_bundle(value: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    from domain.connections.store import save_connection_credential

    return save_connection_credential(
        "codex",
        _CODEX_ACCESS_MATERIAL_TYPE,
        {"credentials": {"access_token": value}},
        connection_id=_DEFAULT_CONNECTION_ID,
        token_metadata={
            "credential_kind": "codex_access_token",
            "status": "connected",
            "account_label": "Codex",
        },
        pack_root=pack_root,
    )


def _delete_codex_token_bundle(*, pack_root: Path | None = None) -> None:
    from domain.connections.store import delete_connection_credential

    delete_connection_credential(
        "codex",
        _CODEX_ACCESS_MATERIAL_TYPE,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _codex_token_credential_ref(*, pack_root: Path | None = None) -> dict[str, str]:
    from domain.connections.store import connection_credential_ref

    return connection_credential_ref(
        "codex",
        _CODEX_ACCESS_MATERIAL_TYPE,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _codex_capabilities(*, configured: bool, pack_root: Path | None = None) -> list[str]:
    if not configured:
        return []
    from domain.connections.store import resolve_capabilities_for_provider

    resolved = resolve_capabilities_for_provider(
        "codex",
        {"credential_kind": "codex_access_token"},
        pack_root=pack_root,
    )
    return list(resolved.get("capabilities") or [])


def _stored_token_exists(pack_root: Path | None = None) -> bool:
    if _codex_token_credential_ref(pack_root=pack_root):
        return True
    try:
        from domain.connections.store import connection_secret_key

        credential_key = connection_secret_key(
            "codex",
            _DEFAULT_CONNECTION_ID,
            _CODEX_ACCESS_MATERIAL_TYPE,
        )
        if _get_store(pack_root).has_secret(credential_key):
            return True
    except Exception:
        pass
    if not _secrets_dir(pack_root).exists():
        return False
    try:
        return any(
            meta.key == _CODEX_TOKEN_KEY and meta.exists and not meta.deleted
            for meta in _get_store(pack_root).list_keys()
        )
    except Exception:
        return False


def read_codex_access_token(*, pack_root: Path | None = None) -> str:
    return _read_secret_value(pack_root)


def save_codex_access_token(value: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    token = str(value or "").strip()
    if not token:
        return {"success": False, "provider_id": "codex", "error": "codex access token is required"}
    try:
        saved = _save_codex_token_bundle(token, pack_root=pack_root)
    except RuntimeError as exc:
        return {"success": False, "provider_id": "codex", "error": str(exc)}
    return {
        "success": True,
        "provider_id": "codex",
        "configured": True,
        "credential_ref": saved.get("credential_ref", {}),
        "status": codex_connection_status(pack_root=pack_root),
    }


def clear_codex_access_token(*, pack_root: Path | None = None) -> dict[str, Any]:
    _delete_codex_token_bundle(pack_root=pack_root)
    result = _get_store(pack_root).delete_secret(
        _CODEX_TOKEN_KEY,
        actor="defaultspack",
        reason="clear codex access token",
    )
    return {
        "success": True,
        "provider_id": "codex",
        "configured": False,
        "cleared": True,
        "error": "" if result.error and "not found" in str(result.error).lower() else result.error,
        "status": codex_connection_status(pack_root=pack_root),
    }


def codex_connection_status(*, pack_root: Path | None = None) -> dict[str, Any]:
    credential_ref = _codex_token_credential_ref(pack_root=pack_root)
    stored_configured = _stored_token_exists(pack_root)
    configured = bool(stored_configured)
    connection_status = "connected" if configured else "missing_token"
    capabilities = _codex_capabilities(configured=configured, pack_root=pack_root)
    return {
        "supported": True,
        "backend_supported": True,
        "provider_id": "codex",
        "provider_kind": "codex",
        "display_label": "Codex",
        "service_kind": "dev",
        "auth_type": "codex",
        "platform_api_key_required": False,
        "auth_methods": _codex_auth_methods(access_token_configured=configured),
        "active_auth_methods": ["codex_access_token"] if configured else [],
        "credential_kind": "codex_access_token",
        "credential_ref": credential_ref,
        "scopes": [],
        "capabilities": capabilities,
        "expires_at": "",
        "status": connection_status,
        "connected": configured,
        "configured": configured,
        "token_configured": configured,
        "token_source": "secret_store" if credential_ref or stored_configured else "missing",
        "can_clear": stored_configured,
        "connect_enabled": False,
        "connection_status": connection_status,
        "status_label": "Token saved" if configured else "Token needed",
        "disabled_reason": "" if configured else "Save Codex access token",
        "config_hint": "Save a Codex access token for local/programmatic workflow use. This is not a Platform API key or Workspace Agent token.",
    }
