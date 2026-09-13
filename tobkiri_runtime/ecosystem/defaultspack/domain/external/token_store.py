from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


_TOKEN_PREFIX = "RUMIEXT"
_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9_]+")

EXTERNAL_REQUIRED_TOKENS: dict[str, list[dict[str, Any]]] = {
    "line": [
        {"kind": "channel_secret", "legacy_key": "LINE_CHANNEL_SECRET"},
        {"kind": "channel_access_token", "legacy_key": "LINE_CHANNEL_ACCESS_TOKEN"},
    ],
    "discord": [
        {"kind": "bot_token", "legacy_key": "DISCORD_BOT_TOKEN"},
        {"kind": "application_id", "legacy_key": "DISCORD_APPLICATION_ID"},
        {"kind": "public_key", "legacy_key": "DISCORD_PUBLIC_KEY"},
    ],
    "slack": [
        {"kind": "bot_token", "legacy_key": "SLACK_BOT_TOKEN"},
        {"kind": "signing_secret", "legacy_key": "SLACK_SIGNING_SECRET"},
        {"kind": "app_token", "legacy_key": "SLACK_APP_TOKEN"},
    ],
    "generic": [
        {"kind": "webhook_shared_secret", "legacy_key": ""},
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


def _metadata_path(pack_root: Path | None = None) -> Path:
    return _secrets_dir(pack_root) / "external_tokens.json"


def _get_store(pack_root: Path | None = None):
    from core_runtime.secrets_store import SecretsStore

    return SecretsStore(str(_secrets_dir(pack_root)))


def _read_metadata(pack_root: Path | None = None) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_metadata_path(pack_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}


def _write_metadata(data: dict[str, dict[str, Any]], pack_root: Path | None = None) -> None:
    path = _metadata_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _slug(value: str, *, fallback: str = "TOKEN", max_length: int = 36) -> str:
    normalized = _SLUG_PATTERN.sub("_", str(value or "").strip()).strip("_").upper()
    normalized = re.sub(r"_+", "_", normalized)
    return (normalized or fallback)[:max_length]


def external_token_secret_key(provider_id: str, token_id: str) -> str:
    provider_slug = _slug(provider_id, fallback="PROVIDER", max_length=18)
    token_slug = _slug(token_id, fallback="MAIN", max_length=36)
    return f"{_TOKEN_PREFIX}_{provider_slug}_{token_slug}"[:64]


def _provider_from_key(key: str) -> str:
    prefix = f"{_TOKEN_PREFIX}_"
    if not key.startswith(prefix):
        return ""
    slug = key[len(prefix):].split("_", 1)[0].lower()
    provider_map = {_slug(provider_id, max_length=18).lower(): provider_id for provider_id in EXTERNAL_REQUIRED_TOKENS}
    return provider_map.get(slug, slug)


def _token_id_from_key(key: str, provider_id: str) -> str:
    prefix = f"{_TOKEN_PREFIX}_{_slug(provider_id, fallback='PROVIDER', max_length=18)}_"
    return key[len(prefix):].lower() if key.startswith(prefix) else key.lower()


def _read_secret_value(key: str, caller_id: str, *, pack_root: Path | None = None) -> str:
    if not key:
        return ""
    try:
        return str(_get_store(pack_root)._internal_read_value(key, caller_id=caller_id) or "").strip()
    except Exception:
        return ""


def _legacy_key_for_kind(provider_id: str, kind: str) -> str:
    for item in EXTERNAL_REQUIRED_TOKENS.get(str(provider_id or "").strip(), []):
        if str(item.get("kind") or "") == kind:
            return str(item.get("legacy_key") or "")
    return ""


def read_external_token(
    provider_id: str,
    *,
    token_id: str | None = None,
    kind: str | None = None,
    legacy_key: str | None = None,
    pack_root: Path | None = None,
) -> str:
    provider_id = str(provider_id or "").strip()
    token_id = str(token_id or "").strip()
    kind = str(kind or "").strip()
    if token_id:
        key = external_token_secret_key(provider_id, token_id)
        value = _read_secret_value(key, f"defaultspack.external:{provider_id}:{token_id}", pack_root=pack_root)
        if value:
            return value
    if kind:
        for item in external_named_tokens(provider_id, pack_root=pack_root):
            if item.get("kind") == kind and item.get("configured"):
                value = _read_secret_value(str(item.get("key") or ""), f"defaultspack.external:{provider_id}:{kind}", pack_root=pack_root)
                if value:
                    return value
    key = str(legacy_key or _legacy_key_for_kind(provider_id, kind)).strip()
    if key:
        return _read_secret_value(key, f"defaultspack.external:{provider_id}:legacy", pack_root=pack_root)
    return ""


def set_external_token(
    provider_id: str,
    value: str,
    *,
    token_id: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    scopes: list[str] | None = None,
    endpoint_ids: list[str] | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    token_id = str(token_id or name or kind or "main").strip()
    if not provider_id or not token_id:
        return {"success": False, "provider_id": provider_id, "error": "provider_id and token_id are required"}
    key = external_token_secret_key(provider_id, token_id)
    display_name = str(name or token_id).strip()
    cleaned = str(value or "").strip()
    if not cleaned:
        result = _get_store(pack_root).delete_secret(
            key,
            actor="defaultspack",
            reason=f"clear {provider_id} external token",
        )
        if result.success:
            metadata = _read_metadata(pack_root)
            metadata.pop(key, None)
            _write_metadata(metadata, pack_root)
        return {
            "success": bool(result.success),
            "provider_id": provider_id,
            "token_id": token_id,
            "key": key,
            "configured": False,
            "cleared": True,
            "error": result.error,
        }

    result = _get_store(pack_root).set_secret(
        key,
        cleaned,
        actor="defaultspack",
        reason=f"set {provider_id} external token",
    )
    if result.success:
        metadata = _read_metadata(pack_root)
        metadata[key] = {
            "provider_id": provider_id,
            "token_id": token_id,
            "name": display_name,
            "kind": str(kind or "token").strip(),
            "scopes": list(scopes or []),
            "endpoint_ids": list(endpoint_ids or []),
        }
        _write_metadata(metadata, pack_root)
    return {
        "success": bool(result.success),
        "provider_id": provider_id,
        "token_id": token_id,
        "name": display_name,
        "kind": str(kind or "token").strip(),
        "key": key,
        "configured": bool(result.success),
        "created": bool(result.created),
        "error": result.error,
    }


def delete_external_token(provider_id: str, token_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    return set_external_token(provider_id, "", token_id=token_id, pack_root=pack_root)


def rename_external_token(
    provider_id: str,
    token_id: str,
    name: str,
    *,
    new_token_id: str | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    token_id = str(token_id or "").strip()
    target_token_id = str(new_token_id or name or token_id).strip()
    display_name = str(name or target_token_id).strip()
    if not provider_id or not token_id or not target_token_id:
        return {"success": False, "provider_id": provider_id, "token_id": token_id, "error": "provider_id and token_id are required"}
    old_key = external_token_secret_key(provider_id, token_id)
    value = _read_secret_value(old_key, f"defaultspack.external:{provider_id}:{token_id}:rename", pack_root=pack_root)
    if not value:
        return {"success": False, "provider_id": provider_id, "token_id": token_id, "error": "token not found"}
    metadata = _read_metadata(pack_root)
    old_meta = metadata.get(old_key, {})
    saved = set_external_token(
        provider_id,
        value,
        token_id=target_token_id,
        name=display_name,
        kind=str(old_meta.get("kind") or "token"),
        scopes=list(old_meta.get("scopes") if isinstance(old_meta.get("scopes"), list) else []),
        endpoint_ids=list(old_meta.get("endpoint_ids") if isinstance(old_meta.get("endpoint_ids"), list) else []),
        pack_root=pack_root,
    )
    if not saved.get("success"):
        return saved
    if old_key != str(saved.get("key")):
        _get_store(pack_root).delete_secret(old_key, actor="defaultspack", reason=f"rename {provider_id} external token")
        metadata = _read_metadata(pack_root)
        metadata.pop(old_key, None)
        _write_metadata(metadata, pack_root)
    saved["renamed"] = True
    return saved


def external_named_tokens(provider_id: str = "", *, pack_root: Path | None = None) -> list[dict[str, Any]]:
    provider_id = str(provider_id or "").strip()
    if not _secrets_dir(pack_root).exists():
        return []
    metadata = _read_metadata(pack_root)
    store = _get_store(pack_root)
    items: list[dict[str, Any]] = []
    for meta in store.list_keys():
        key = str(meta.key or "")
        if not key.startswith(f"{_TOKEN_PREFIX}_") or meta.deleted:
            continue
        stored = metadata.get(key, {})
        key_provider = str(stored.get("provider_id") or "").strip() or _provider_from_key(key)
        if provider_id and key_provider != provider_id:
            continue
        token_id = str(stored.get("token_id") or _token_id_from_key(key, key_provider))
        items.append(
            {
                "provider_id": str(stored.get("provider_id") or key_provider),
                "token_id": token_id,
                "name": str(stored.get("name") or token_id),
                "kind": str(stored.get("kind") or "token"),
                "key": key,
                "label": f"{key_provider}:{token_id}:***",
                "configured": bool(meta.exists),
                "created_at": meta.created_at,
                "updated_at": meta.updated_at,
                "scopes": list(stored.get("scopes") if isinstance(stored.get("scopes"), list) else []),
                "endpoint_ids": list(stored.get("endpoint_ids") if isinstance(stored.get("endpoint_ids"), list) else []),
            }
        )
    return sorted(items, key=lambda item: (str(item.get("provider_id")), str(item.get("token_id"))))


def external_token_status(*, pack_root: Path | None = None) -> list[dict[str, Any]]:
    provider_ids = set(EXTERNAL_REQUIRED_TOKENS.keys())
    for token in external_named_tokens("", pack_root=pack_root):
        provider_id = str(token.get("provider_id") or "").strip()
        if provider_id:
            provider_ids.add(provider_id)
    providers = sorted(provider_ids)
    result: list[dict[str, Any]] = []
    for provider_id in providers:
        required = []
        for item in EXTERNAL_REQUIRED_TOKENS.get(provider_id, []):
            kind = str(item.get("kind") or "")
            required.append(
                {
                    "kind": kind,
                    "configured": bool(read_external_token(provider_id, kind=kind, pack_root=pack_root)),
                }
            )
        tokens = external_named_tokens(provider_id, pack_root=pack_root)
        result.append(
            {
                "provider_id": provider_id,
                "tokens": tokens,
                "configured": any(token.get("configured") for token in tokens) or any(item["configured"] for item in required),
                "required_tokens": required,
            }
        )
    return result
