from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List


INTEGRATION_SECRET_KEYS: Dict[str, List[str]] = {
    "discord": [
        "DISCORD_BOT_TOKEN",
        "DISCORD_APPLICATION_ID",
        "DISCORD_PUBLIC_KEY",
    ],
    "line": [
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_SECRET",
    ],
    "slack": [
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "SLACK_APP_TOKEN",
    ],
}


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


def integration_secret_keys(provider_id: str | None = None) -> List[str]:
    if provider_id:
        return list(INTEGRATION_SECRET_KEYS.get(str(provider_id or "").strip(), []))
    keys: List[str] = []
    for provider_keys in INTEGRATION_SECRET_KEYS.values():
        for key in provider_keys:
            if key not in keys:
                keys.append(key)
    return keys


def provider_for_secret_key(key: str) -> str:
    normalized = str(key or "").strip()
    for provider_id, keys in INTEGRATION_SECRET_KEYS.items():
        if normalized in keys:
            return provider_id
    return ""


def get_integration_secret(provider_id: str, key: str, *, pack_root: Path | None = None) -> str:
    normalized_provider = str(provider_id or "").strip()
    normalized_key = str(key or "").strip()
    if normalized_key not in integration_secret_keys(normalized_provider):
        return ""
    value = ""
    if (_secrets_dir(pack_root) / f"{normalized_key}.json").exists():
        value = _get_store(pack_root)._internal_read_value(
            normalized_key,
            caller_id=f"defaultspack.integrations:{normalized_provider}",
        )
    if value:
        return str(value or "").strip()
    try:
        from domain.external.token_store import read_external_token

        legacy_kind = {
            "LINE_CHANNEL_SECRET": "channel_secret",
            "LINE_CHANNEL_ACCESS_TOKEN": "channel_access_token",
            "DISCORD_BOT_TOKEN": "bot_token",
            "DISCORD_APPLICATION_ID": "application_id",
            "DISCORD_PUBLIC_KEY": "public_key",
            "SLACK_BOT_TOKEN": "bot_token",
            "SLACK_SIGNING_SECRET": "signing_secret",
            "SLACK_APP_TOKEN": "app_token",
        }.get(normalized_key, "")
        return read_external_token(
            normalized_provider,
            kind=legacy_kind,
            legacy_key=normalized_key,
            pack_root=pack_root,
        )
    except Exception:
        return ""


def load_integration_secrets_into_env(*, pack_root: Path | None = None) -> Dict[str, bool]:
    loaded: Dict[str, bool] = {}
    for provider_id, keys in INTEGRATION_SECRET_KEYS.items():
        configured = False
        for key in keys:
            if get_integration_secret(provider_id, key, pack_root=pack_root):
                configured = True
        loaded[provider_id] = configured
    return loaded


def integration_secret_status(*, pack_root: Path | None = None) -> List[Dict[str, Any]]:
    result = []
    for provider_id, keys in sorted(INTEGRATION_SECRET_KEYS.items()):
        configured_keys = [key for key in keys if get_integration_secret(provider_id, key, pack_root=pack_root)]
        result.append(
            {
                "provider_id": provider_id,
                "keys": list(keys),
                "configured_keys": configured_keys,
                "configured": bool(configured_keys),
            }
        )
    return result


def set_integration_secret(
    provider_id: str,
    key: str,
    value: str,
    *,
    pack_root: Path | None = None,
) -> Dict[str, Any]:
    normalized_provider = str(provider_id or "").strip()
    normalized_key = str(key or "").strip()
    keys = integration_secret_keys(normalized_provider)
    if normalized_key not in keys:
        return {
            "success": False,
            "provider_id": normalized_provider,
            "key": normalized_key,
            "error": "unsupported integration secret",
        }

    cleaned = str(value or "").strip()
    if not cleaned:
        result = _get_store(pack_root).delete_secret(
            normalized_key,
            actor="defaultspack",
            reason=f"clear {normalized_provider} integration secret",
        )
        return {
            "success": bool(result.success),
            "provider_id": normalized_provider,
            "key": normalized_key,
            "configured": False,
            "cleared": True,
            "error": result.error,
        }

    result = _get_store(pack_root).set_secret(
        normalized_key,
        cleaned,
        actor="defaultspack",
        reason=f"set {normalized_provider} integration secret",
    )
    return {
        "success": bool(result.success),
        "provider_id": normalized_provider,
        "key": normalized_key,
        "configured": bool(result.success),
        "created": bool(result.created),
        "error": result.error,
    }
