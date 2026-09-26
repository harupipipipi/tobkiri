from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GOOGLE_DEFAULT_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/generative-language",
]
_GOOGLE_IDENTITY_SCOPES = ["openid", "email", "profile"]
_GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_GOOGLE_GMAIL_LABELS_SCOPE = "https://www.googleapis.com/auth/gmail.labels"
_GOOGLE_GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
_GOOGLE_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GOOGLE_SCOPE_MODES = {
    "google_identity": list(_GOOGLE_IDENTITY_SCOPES),
    "google_ai": list(_GOOGLE_DEFAULT_SCOPES),
    "google_workspace": [*_GOOGLE_IDENTITY_SCOPES, _GOOGLE_DRIVE_FILE_SCOPE, _GOOGLE_GMAIL_LABELS_SCOPE],
    "google_drive": [*_GOOGLE_IDENTITY_SCOPES, _GOOGLE_DRIVE_FILE_SCOPE],
    "google_gmail_labels": [*_GOOGLE_IDENTITY_SCOPES, _GOOGLE_GMAIL_LABELS_SCOPE],
    "google_gmail_metadata": [*_GOOGLE_IDENTITY_SCOPES, _GOOGLE_GMAIL_METADATA_SCOPE],
    "google_gmail_readonly": [*_GOOGLE_IDENTITY_SCOPES, _GOOGLE_GMAIL_READONLY_SCOPE],
}
_GOOGLE_SCOPE_MODE_DETAILS = {
    "google_identity": {
        "label": "Google identity",
        "description": "Basic Google sign-in identity only.",
        "services": ["identity"],
        "surface": "accounts_connections",
    },
    "google_drive": {
        "label": "Google Drive selected files",
        "description": "Drive file scope for files created, opened, or explicitly shared with Rumi.",
        "services": ["identity", "drive_file"],
        "surface": "accounts_connections",
    },
    "google_gmail_labels": {
        "label": "Gmail labels",
        "description": "Low-friction Gmail labels access without message bodies.",
        "services": ["identity", "gmail_labels"],
        "surface": "accounts_connections",
    },
    "google_gmail_metadata": {
        "label": "Gmail metadata/search",
        "description": "Restricted Gmail metadata scope for search and message metadata.",
        "services": ["identity", "gmail_metadata"],
        "restricted": True,
        "warning": "Restricted Gmail scopes require explicit self-host acknowledgement or Google verification review.",
        "surface": "accounts_connections",
    },
    "google_gmail_readonly": {
        "label": "Gmail read-only bodies",
        "description": "Restricted Gmail read-only scope for message bodies.",
        "services": ["identity", "gmail_readonly"],
        "restricted": True,
        "warning": "Restricted Gmail scopes can expose message content and may require Google security review.",
        "surface": "accounts_connections",
    },
    "google_ai": {
        "label": "Google AI",
        "description": "Gemini / Generative Language API access for model calls.",
        "services": ["identity", "generative_language"],
        "surface": "models_api",
    },
}

_CLIENT_CONFIG_SECRET_KEYS = {
    "google": "RUMIOAUTH_GOOGLE_CLIENT_CONFIG",
    "cloudflare": "RUMIOAUTH_CLOUDFLARE_CLIENT_CONFIG",
}
_ACCESS_TOKEN_SECRET_KEYS = {
    "google": "RUMIOAUTH_GOOGLE_ACCESS_TOKEN",
    "cloudflare": "RUMIOAUTH_CLOUDFLARE_ACCESS_TOKEN",
}
_REFRESH_TOKEN_SECRET_KEYS = {
    "google": "RUMIOAUTH_GOOGLE_REFRESH_TOKEN",
    "cloudflare": "RUMIOAUTH_CLOUDFLARE_REFRESH_TOKEN",
}
_ID_TOKEN_SECRET_KEYS = {
    "google": "RUMIOAUTH_GOOGLE_ID_TOKEN",
    "cloudflare": "RUMIOAUTH_CLOUDFLARE_ID_TOKEN",
}
_PROVIDER_OAUTH_ENV_ALIASES = {
    ("cloudflare", "ACCESS_TOKEN"): ["CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"],
    ("cloudflare", "SCOPES"): ["CLOUDFLARE_OAUTH_SCOPES", "CLOUDFLARE_API_TOKEN_SCOPES"],
    ("cloudflare", "REQUESTED_CAPABILITIES"): [
        "CLOUDFLARE_REQUESTED_CAPABILITIES",
        "CLOUDFLARE_API_TOKEN_REQUESTED_CAPABILITIES",
    ],
    ("cloudflare", "ACCOUNT_ID"): ["RUMI_CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID"],
    ("cloudflare", "ZONE_ID"): ["RUMI_CLOUDFLARE_ZONE_ID", "CLOUDFLARE_ZONE_ID"],
}

_OAUTH_CLIENT_MATERIAL_TYPE = "oauth2_client_config"
_OAUTH_TOKEN_MATERIAL_TYPE = "oauth2_token"
_DEFAULT_CONNECTION_ID = "default"
_OAUTH_RUNTIME_PROVIDER_IDS = {"cloudflare", "google"}
_PENDING_STATE_TTL_SECONDS = 600
_ACCESS_TOKEN_SKEW_SECONDS = 60
_pending_states: dict[str, dict[str, Any]] = {}
_connection_cache_lock = threading.RLock()
_connection_registry_cache: dict[str, tuple[tuple[Any, ...], Any]] = {}
_connection_manifest_signature_cache: dict[
    str, tuple[tuple[Any, ...], tuple[tuple[str, int, int], ...]]
] = {}


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _connection_manifest_root(pack_root: Path | None = None) -> Path:
    candidate = (pack_root or _pack_root()) / "config" / "settings_control_center" / "providers"
    if candidate.exists():
        return candidate
    return _pack_root() / "config" / "settings_control_center" / "providers"


def _connection_manifest_signature(
    root: Path,
) -> tuple[tuple[str, int, int], ...]:
    """Return a manifest signature without rescanning an unchanged tree.

    The registry is read on several provider-status paths.  The fast path
    checks the known files and directories with ``stat``; a recursive scan is
    only needed after a directory changes.  This still notices additions,
    removals, and edits to manifests while avoiding repeated JSON loading.
    """
    cache_key = str(root)
    try:
        root.stat()
    except OSError:
        with _connection_cache_lock:
            _connection_manifest_signature_cache.pop(cache_key, None)
            _connection_registry_cache.pop(cache_key, None)
        return ()

    with _connection_cache_lock:
        cached = _connection_manifest_signature_cache.get(cache_key)
        if cached is not None:
            tree_signature, manifest_signature = cached
            tree_unchanged = True
            for relative_path, mtime_ns, size in tree_signature:
                path = root / relative_path
                try:
                    stat = path.stat()
                except OSError:
                    tree_unchanged = False
                    break
                if stat.st_mtime_ns != mtime_ns or stat.st_size != size:
                    tree_unchanged = False
                    break
            if tree_unchanged:
                for relative_path, mtime_ns, size in manifest_signature:
                    path = root / relative_path
                    try:
                        stat = path.stat()
                    except OSError:
                        tree_unchanged = False
                        break
                    if stat.st_mtime_ns != mtime_ns or stat.st_size != size:
                        tree_unchanged = False
                        break
            if tree_unchanged:
                return manifest_signature

        from core_runtime.connections.registry import discover_connection_manifests

        manifest_paths = discover_connection_manifests(root)
        directories = sorted({root, *(path.parent for path in manifest_paths)})
        tree_entries: list[tuple[str, int, int]] = []
        for directory in directories:
            try:
                stat = directory.stat()
            except OSError:
                continue
            relative_path = str(directory.relative_to(root))
            tree_entries.append((relative_path, stat.st_mtime_ns, stat.st_size))

        manifest_entries: list[tuple[str, int, int]] = []
        for path in manifest_paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            manifest_entries.append(
                (str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size)
            )

        tree_signature = tuple(sorted(set(tree_entries)))
        manifest_signature = tuple(sorted(manifest_entries))
        _connection_manifest_signature_cache[cache_key] = (
            tree_signature,
            manifest_signature,
        )
        return manifest_signature


def _connection_registry(pack_root: Path | None = None):
    from core_runtime.connections.registry import ConnectionsRegistry

    root = _connection_manifest_root(pack_root)
    signature = _connection_manifest_signature(root)
    cache_key = str(root)
    with _connection_cache_lock:
        cached = _connection_registry_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        registry = ConnectionsRegistry()
        if signature:
            registry.load_manifest_dir(root)
        _connection_registry_cache[cache_key] = (signature, registry)
        return registry


def _connection_provider(provider_id: str, *, pack_root: Path | None = None):
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return None
    try:
        return _connection_registry(pack_root).get(provider_id)
    except KeyError:
        return None


def _connection_provider_ids(*, pack_root: Path | None = None) -> set[str]:
    return {
        str(item.get("provider_id") or item.get("providerId") or "").strip()
        for item in _connection_registry(pack_root).list_providers()
        if isinstance(item, dict)
        and str(item.get("provider_id") or item.get("providerId") or "").strip()
    }


def _dotenv_candidates(pack_root: Path | None = None) -> list[Path]:
    root = pack_root or _pack_root()
    candidates = [
        root / ".env",
        root / "config" / "settings_control_center" / "oauth.env",
    ]
    candidates.extend(parent / ".env" for parent in root.parents[:4])
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        # Do not resolve filesystem links merely to deduplicate a fixed list
        # of dotenv candidates.  ``resolve()`` can traverse a symlinked or
        # unavailable mount and block provider discovery for every model.
        lexical_path = candidate.expanduser().absolute()
        if lexical_path in seen:
            continue
        seen.add(lexical_path)
        ordered.append(candidate)
    return ordered


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}
        if quoted:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _env_value(name: str, *, pack_root: Path | None = None) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    from core_runtime.host_contract import host_contract_value

    value = host_contract_value(name)
    if value:
        return value
    return ""


def _first_env_value(names: list[str], *, pack_root: Path | None = None) -> str:
    for name in names:
        value = _env_value(name, pack_root=pack_root)
        if value:
            return value
    return ""


def _provider_env_prefix(provider_id: str) -> str:
    return str(provider_id or "").strip().upper().replace("-", "_")


def _provider_oauth_env_names(provider_id: str, suffix: str) -> list[str]:
    prefix = _provider_env_prefix(provider_id)
    suffix = str(suffix or "").strip().upper()
    if not prefix or not suffix:
        return []
    names = [
        f"RUMI_DEFAULTSPACK_{prefix}_OAUTH_{suffix}",
        f"RUMI_{prefix}_OAUTH_{suffix}",
    ]
    names.extend(_PROVIDER_OAUTH_ENV_ALIASES.get((str(provider_id or "").strip().lower(), suffix), []))
    return names


def _normalize_scope_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            return [str(item).strip() for item in payload if str(item).strip()]
    return [item for item in text.replace(",", " ").split() if item]


def _scopes_from_env(provider_id: str, *, pack_root: Path | None = None) -> list[str]:
    raw = _first_env_value(_provider_oauth_env_names(provider_id, "SCOPES"), pack_root=pack_root)
    return _normalize_scope_list(raw)


def _requested_capabilities_from_env(provider_id: str, *, pack_root: Path | None = None) -> list[str]:
    raw = _first_env_value(_provider_oauth_env_names(provider_id, "REQUESTED_CAPABILITIES"), pack_root=pack_root)
    return _normalize_scope_list(raw)


def _provider_context_from_env(provider_id: str, *, pack_root: Path | None = None) -> dict[str, str]:
    context: dict[str, str] = {}
    account_id = _first_env_value(_provider_oauth_env_names(provider_id, "ACCOUNT_ID"), pack_root=pack_root)
    zone_id = _first_env_value(_provider_oauth_env_names(provider_id, "ZONE_ID"), pack_root=pack_root)
    if account_id:
        context["account_id"] = account_id
    if zone_id:
        context["zone_id"] = zone_id
    return context


def _secrets_dir(pack_root: Path | None = None) -> Path:
    # A path override only selects the persistent store.  It is intentionally
    # separate from _env_value(), which resolves credential material solely
    # through the explicit host contract.
    override = ""
    if pack_root is None:
        override = os.getenv("RUMI_DEFAULTSPACK_SECRETS_DIR", "").strip()
    if override:
        return Path(override)
    if pack_root is None:
        configured_user_data = os.getenv("RUMI_USER_DATA", "").strip()
        if configured_user_data:
            return Path(configured_user_data).expanduser() / "secrets"
    return (pack_root or _pack_root()) / "user_data" / "secrets"


def _metadata_path(pack_root: Path | None = None) -> Path:
    return (pack_root or _pack_root()) / "user_data" / "settings" / "provider_oauth.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _cleanup_pending_states() -> None:
    cutoff = time.time() - _PENDING_STATE_TTL_SECONDS
    expired = [state for state, entry in _pending_states.items() if float(entry.get("created_at") or 0.0) < cutoff]
    for state in expired:
        _pending_states.pop(state, None)


def _read_metadata(pack_root: Path | None = None) -> dict[str, dict[str, Any]]:
    path = _metadata_path(pack_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(value, dict)
    }


def _write_metadata(data: dict[str, dict[str, Any]], pack_root: Path | None = None) -> None:
    path = _metadata_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _get_store(pack_root: Path | None = None):
    from core_runtime.secrets_store import SecretsStore

    return SecretsStore(str(_secrets_dir(pack_root)))


def _secret_key(mapping: dict[str, str], provider_id: str) -> str:
    return str(mapping.get(str(provider_id or "").strip(), "")).strip()


def _read_secret(key: str, caller_id: str, *, pack_root: Path | None = None) -> str:
    if not key:
        return ""
    value = _env_value(key, pack_root=pack_root).strip()
    if value:
        return value
    try:
        secret = _get_store(pack_root)._internal_read_value(key, caller_id=caller_id)
    except Exception:
        return ""
    return str(secret or "").strip()


def _read_provider_oauth_secret(
    provider_id: str,
    mapping: dict[str, str],
    suffix: str,
    caller_suffix: str,
    *,
    pack_root: Path | None = None,
) -> str:
    provider_id = str(provider_id or "").strip()
    bundle_value = _read_provider_oauth_bundle_value(provider_id, suffix, pack_root=pack_root)
    if bundle_value:
        return bundle_value
    key = _secret_key(mapping, provider_id)
    value = _read_secret(key, f"defaultspack.oauth:{provider_id}:{caller_suffix}", pack_root=pack_root)
    if value:
        return value
    value = _first_env_value(_provider_oauth_env_names(provider_id, suffix), pack_root=pack_root).strip()
    if value:
        return value
    return ""


def _read_connection_credential(provider_id: str, material_type: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    from domain.connections.store import read_connection_credential

    return read_connection_credential(
        provider_id,
        material_type,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _save_connection_credential(
    provider_id: str,
    material_type: str,
    secret_material: dict[str, Any],
    *,
    token_metadata: dict[str, Any] | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    from domain.connections.store import save_connection_credential

    return save_connection_credential(
        provider_id,
        material_type,
        secret_material,
        connection_id=_DEFAULT_CONNECTION_ID,
        token_metadata=token_metadata,
        pack_root=pack_root,
    )


def _delete_connection_credential(provider_id: str, material_type: str, *, pack_root: Path | None = None) -> None:
    from domain.connections.store import delete_connection_credential

    delete_connection_credential(
        provider_id,
        material_type,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _connection_credential_ref(provider_id: str, material_type: str, *, pack_root: Path | None = None) -> dict[str, str]:
    from domain.connections.store import connection_credential_ref

    return connection_credential_ref(
        provider_id,
        material_type,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _resolve_connection_capabilities(provider_id: str, token_metadata: dict[str, Any], *, pack_root: Path | None = None) -> dict[str, Any]:
    from domain.connections.store import resolve_capabilities_for_provider

    return resolve_capabilities_for_provider(provider_id, token_metadata, pack_root=pack_root)


def _provider_granted_capabilities(provider_id: str, token_metadata: dict[str, Any], *, pack_root: Path | None = None) -> list[str]:
    from core_runtime.connections.permission_resolver import provider_granted_capabilities

    provider = _connection_provider(provider_id, pack_root=pack_root)
    if provider is None:
        return []
    return provider_granted_capabilities(provider, token_metadata)


def _read_provider_oauth_bundle_value(provider_id: str, suffix: str, *, pack_root: Path | None = None) -> str:
    payload = _read_connection_credential(provider_id, _OAUTH_TOKEN_MATERIAL_TYPE, pack_root=pack_root)
    credentials_value = payload.get("credentials")
    credentials: dict[str, object] = (
        {str(key): value for key, value in credentials_value.items()}
        if isinstance(credentials_value, dict)
        else {}
    )
    lookup = {
        "ACCESS_TOKEN": "access_token",
        "REFRESH_TOKEN": "refresh_token",
        "ID_TOKEN": "id_token",
    }
    field = lookup.get(str(suffix or "").strip().upper(), "")
    return str(credentials.get(field) or "").strip() if field else ""


def _set_secret(key: str, value: str, *, actor: str, reason: str, pack_root: Path | None = None) -> None:
    result = _get_store(pack_root).set_secret(key, value, actor=actor, reason=reason)
    if not result.success:
        raise RuntimeError(result.error or f"failed to save secret {key}")


def _delete_secret(key: str, *, actor: str, reason: str, pack_root: Path | None = None) -> None:
    if not key:
        return
    try:
        _get_store(pack_root).delete_secret(key, actor=actor, reason=reason)
    except Exception:
        pass
    os.environ.pop(key, None)


def _reset_ai_client() -> None:
    try:
        from domain.ai_client.client import AIClient

        AIClient._instance = None
    except Exception:
        pass


def provider_supports_oauth(provider_id: str) -> bool:
    provider_id = str(provider_id or "").strip()
    provider = _connection_provider(provider_id)
    return provider is not None and provider.oauth is not None


def _client_id_label(client_id: str) -> str:
    client_id = str(client_id or "").strip()
    if not client_id:
        return ""
    if len(client_id) <= 18:
        return client_id
    return f"{client_id[:10]}...{client_id[-8:]}"


def _default_scopes(provider_id: str, scope_mode: str | None = None, *, pack_root: Path | None = None) -> list[str]:
    provider_id = str(provider_id or "").strip()
    if provider_id != "google":
        client = load_provider_client_config(provider_id, pack_root=pack_root)
        client_scopes = _normalize_scope_list((client or {}).get("scopes"))
        if client_scopes:
            return client_scopes
        provider = _connection_provider(provider_id, pack_root=pack_root)
        manifest_scopes = list(provider.oauth.default_scopes if provider and provider.oauth else [])
        if manifest_scopes:
            return manifest_scopes
        return _scopes_from_env(provider_id, pack_root=pack_root)
    mode = str(scope_mode or "google_identity").strip() or "google_identity"
    if mode == "default":
        mode = "google_identity"
    if mode not in _GOOGLE_SCOPE_MODES:
        raise ValueError(f"unsupported Google OAuth scope mode: {mode}")
    override = _first_env_value(
        ["RUMI_DEFAULTSPACK_GOOGLE_OAUTH_SCOPES", "RUMI_GOOGLE_OAUTH_SCOPES"],
        pack_root=pack_root,
    )
    if override and mode == "google_ai":
        return [item for item in override.split() if item]
    return list(_GOOGLE_SCOPE_MODES[mode])


def _google_scope_mode_rows(*, pack_root: Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mode in (
        "google_identity",
        "google_drive",
        "google_gmail_labels",
        "google_gmail_metadata",
        "google_gmail_readonly",
        "google_ai",
    ):
        details_value = _GOOGLE_SCOPE_MODE_DETAILS[mode]
        details: dict[str, object] = (
            {str(key): item for key, item in details_value.items()}
            if isinstance(details_value, dict)
            else {}
        )
        rows.append(
            {
                "id": mode,
                "label": str(details.get("label") or mode),
                "description": str(details.get("description") or ""),
                "scopes": _default_scopes("google", mode, pack_root=pack_root),
                "services": (
                    list(services_value)
                    if isinstance(
                        services_value := details.get("services"), list
                    )
                    else []
                ),
                "restricted": bool(details.get("restricted")),
                "warning": str(details.get("warning") or ""),
                "surface": str(details.get("surface") or ""),
            }
        )
    return rows


def _normalize_requested_services(services: Any) -> list[str]:
    if not isinstance(services, list):
        return []
    normalized: list[str] = []
    for item in services:
        value = str(item or "").strip().lower().replace("-", "_")
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _scope_mode_from_services(provider_id: str, services: Any) -> str | None:
    if str(provider_id or "").strip() != "google":
        return None
    service_set = set(_normalize_requested_services(services))
    if not service_set:
        return None
    if service_set & {"gmail_readonly", "readonly_body", "gmail:readonly_body"}:
        return "google_gmail_readonly"
    if service_set & {"gmail_metadata", "metadata_search", "gmail:metadata_search"}:
        return "google_gmail_metadata"
    has_drive = bool(service_set & {"drive", "drive_file", "google_drive"})
    has_gmail_labels = bool(service_set & {"gmail", "gmail_labels", "labels_only", "gmail:labels_only"})
    if has_drive and has_gmail_labels:
        return "google_workspace"
    if has_drive:
        return "google_drive"
    if has_gmail_labels:
        return "google_gmail_labels"
    if service_set & {"ai", "google_ai", "generative_language"}:
        return "google_ai"
    if "identity" in service_set:
        return "google_identity"
    return None


def _load_env_client_config_for_root(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any] | None:
    provider_id = str(provider_id or "").strip()
    raw_json = _first_env_value(_provider_oauth_env_names(provider_id, "CLIENT_JSON"), pack_root=pack_root)
    raw_id = _first_env_value(_provider_oauth_env_names(provider_id, "CLIENT_ID"), pack_root=pack_root)
    raw_secret = _first_env_value(_provider_oauth_env_names(provider_id, "CLIENT_SECRET"), pack_root=pack_root)
    raw_redirect_uri = _first_env_value(_provider_oauth_env_names(provider_id, "REDIRECT_URI"), pack_root=pack_root)
    raw_scopes = _scopes_from_env(provider_id, pack_root=pack_root)
    if raw_json:
        config = _parse_provider_client_config(provider_id, raw_json)
    elif raw_id:
        config = {
            "provider_id": provider_id,
            "client_id": raw_id,
            "client_secret": raw_secret,
            "redirect_uris": [],
            "scopes": [],
            "source": "env",
        }
    else:
        return None
    if raw_secret and not str(config.get("client_secret") or "").strip():
        config["client_secret"] = raw_secret
    if raw_redirect_uri:
        redirect_uris = [
            raw_redirect_uri,
            *[
                str(item).strip()
                for item in (config.get("redirect_uris") or [])
                if str(item).strip() and str(item).strip() != raw_redirect_uri
            ],
        ]
        config["redirect_uris"] = redirect_uris
    if raw_scopes:
        config["scopes"] = raw_scopes
    config["source"] = "env"
    return config


def _load_env_client_config(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any] | None:
    return _load_env_client_config_for_root(provider_id, pack_root=pack_root)


def _parse_standard_client_config(provider_id: str, raw_value: str, provider_label: str) -> dict[str, Any]:
    text = str(raw_value or "").strip()
    if not text:
        raise ValueError(f"{provider_label} OAuth client config is required")
    client_id = ""
    client_secret = ""
    redirect_uris: list[str] = []
    scopes: list[str] = []
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{provider_label} OAuth client config must be valid JSON") from exc
        if isinstance(payload.get("installed"), dict):
            payload = payload["installed"]
        elif isinstance(payload.get("web"), dict):
            payload = payload["web"]
        if not isinstance(payload, dict):
            raise ValueError(f"{provider_label} OAuth client config JSON is invalid")
        client_id = str(payload.get("client_id") or "").strip()
        client_secret = str(payload.get("client_secret") or "").strip()
        redirect_uris = [
            str(item).strip()
            for item in (payload.get("redirect_uris") or [])
            if str(item).strip()
        ]
        redirect_uri = str(payload.get("redirect_uri") or "").strip()
        if redirect_uri and redirect_uri not in redirect_uris:
            redirect_uris.insert(0, redirect_uri)
        scopes = _normalize_scope_list(payload.get("scopes") or payload.get("scope"))
    else:
        client_id = text
    if not client_id:
        raise ValueError(f"{provider_label} OAuth client_id is required")
    return {
        "provider_id": provider_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "scopes": scopes,
        "source": "stored",
    }


def _parse_google_client_config(raw_value: str) -> dict[str, Any]:
    return _parse_standard_client_config("google", raw_value, "Google")


def _parse_provider_client_config(provider_id: str, raw_value: str) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if provider_id == "google":
        return _parse_google_client_config(raw_value)
    provider = _connection_provider(provider_id)
    if provider is None or provider.oauth is None:
        raise ValueError(f"OAuth is not supported for provider '{provider_id}'")
    label = provider.display_name if isinstance(provider.display_name, str) else provider_id
    return _parse_standard_client_config(provider_id, raw_value, str(label or provider_id))


def load_provider_client_config(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any] | None:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return None
    bundle = _read_connection_credential(provider_id, _OAUTH_CLIENT_MATERIAL_TYPE, pack_root=pack_root)
    credentials = bundle.get("credentials") if isinstance(bundle.get("credentials"), dict) else {}
    if credentials:
        config = {
            "provider_id": provider_id,
            "client_id": str(credentials.get("client_id") or "").strip(),
            "client_secret": str(credentials.get("client_secret") or "").strip(),
            "redirect_uris": [
                str(item).strip()
                for item in (credentials.get("redirect_uris") or [])
                if str(item).strip()
            ],
            "scopes": _normalize_scope_list(credentials.get("scopes")),
            "source": "secret_store",
            "credential_ref": _connection_credential_ref(provider_id, _OAUTH_CLIENT_MATERIAL_TYPE, pack_root=pack_root),
        }
        if config["client_id"]:
            return config
    key = _secret_key(_CLIENT_CONFIG_SECRET_KEYS, provider_id)
    raw_value = _read_secret(key, f"defaultspack.oauth:{provider_id}:client", pack_root=pack_root)
    if raw_value:
        config = _parse_provider_client_config(provider_id, raw_value)
        config["source"] = "secret_store"
        return config
    return _load_env_client_config(provider_id, pack_root=pack_root)


def save_provider_oauth_client_config(
    provider_id: str,
    raw_value: str,
    *,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    config = _parse_provider_client_config(provider_id, raw_value)
    try:
        saved = _save_connection_credential(
            provider_id,
            _OAUTH_CLIENT_MATERIAL_TYPE,
            {
                "credentials": {
                    "client_id": config.get("client_id"),
                    "client_secret": config.get("client_secret"),
                    "redirect_uris": config.get("redirect_uris") or [],
                    "scopes": _normalize_scope_list(config.get("scopes")),
                },
            },
            token_metadata={
                "credential_kind": _OAUTH_CLIENT_MATERIAL_TYPE,
                "scopes": _normalize_scope_list(config.get("scopes")),
                "status": "configured",
                "account_label": _client_id_label(str(config.get("client_id") or "")),
            },
            pack_root=pack_root,
        )
    except RuntimeError as exc:
        return {"success": False, "provider_id": provider_id, "error": str(exc)}
    return {
        "success": True,
        "provider_id": provider_id,
        "client_configured": True,
        "client_label": _client_id_label(str(config.get("client_id") or "")),
        "credential_ref": saved.get("credential_ref", {}),
    }


def clear_provider_oauth_client_config(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    _delete_connection_credential(provider_id, _OAUTH_CLIENT_MATERIAL_TYPE, pack_root=pack_root)
    _delete_secret(
        _secret_key(_CLIENT_CONFIG_SECRET_KEYS, provider_id),
        actor="defaultspack",
        reason=f"clear {provider_id} oauth client config",
        pack_root=pack_root,
    )
    disconnect_provider_oauth(provider_id, pack_root=pack_root)
    return {"success": True, "provider_id": provider_id, "client_configured": False, "connected": False}


def _provider_bundle_metadata(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    payload = _read_connection_credential(provider_id, _OAUTH_TOKEN_MATERIAL_TYPE, pack_root=pack_root)
    token_metadata_value = payload.get("token_metadata")
    if not isinstance(token_metadata_value, dict):
        return {}
    return {str(key): item for key, item in token_metadata_value.items()}


def _provider_metadata(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    return {
        **_provider_bundle_metadata(provider_id, pack_root=pack_root),
        **dict(_read_metadata(pack_root).get(provider_id, {})),
    }


def _write_provider_metadata(provider_id: str, payload: dict[str, Any], *, pack_root: Path | None = None) -> None:
    provider_id = str(provider_id or "").strip()
    metadata = _read_metadata(pack_root)
    metadata[provider_id] = dict(payload)
    _write_metadata(metadata, pack_root)


def _expires_at_text(expires_in: Any) -> str:
    try:
        seconds = int(expires_in or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return ""
    return _isoformat(_now_utc() + timedelta(seconds=seconds))


def save_provider_oauth_connection(
    provider_id: str,
    token_data: dict[str, Any],
    *,
    userinfo: dict[str, Any] | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    if not access_token and not refresh_token:
        return {"success": False, "provider_id": provider_id, "error": "token payload is missing access and refresh tokens"}

    existing = _provider_metadata(provider_id, pack_root=pack_root)
    scopes = [
        item
        for item in str(token_data.get("scope") or "").split()
        if item
    ] or list(existing.get("scopes") or [])
    expires_at = _expires_at_text(token_data.get("expires_in")) or str(existing.get("expires_at") or "")
    profile = dict(userinfo or {})
    metadata = {
        **existing,
        "provider_id": provider_id,
        "connected": True,
        "token_type": str(token_data.get("token_type") or existing.get("token_type") or "Bearer"),
        "scopes": scopes,
        "scope_mode": str(token_data.get("scope_mode") or existing.get("scope_mode") or "").strip(),
        "services": list(token_data.get("services") or existing.get("services") or []),
        "expires_at": expires_at,
        "connected_at": str(existing.get("connected_at") or _isoformat(_now_utc())),
        "updated_at": _isoformat(_now_utc()),
        "email": str(profile.get("email") or existing.get("email") or "").strip(),
        "display_name": str(profile.get("name") or existing.get("display_name") or "").strip(),
        "picture_url": str(profile.get("picture") or existing.get("picture_url") or "").strip(),
        "sub": str(profile.get("sub") or existing.get("sub") or "").strip(),
        "has_refresh_token": bool(refresh_token or existing.get("has_refresh_token")),
    }
    capability_metadata = {**metadata, "credential_kind": _OAUTH_TOKEN_MATERIAL_TYPE}
    explicit_requested_capabilities = _normalize_scope_list(
        token_data.get("requested_capabilities")
        or token_data.get("requestedCapabilities")
    )
    requested_capabilities = (
        explicit_requested_capabilities
        or _provider_granted_capabilities(
            provider_id,
            capability_metadata,
            pack_root=pack_root,
        )
    )
    if requested_capabilities:
        capability_metadata["requested_capabilities"] = requested_capabilities
    resolved = _resolve_connection_capabilities(provider_id, capability_metadata, pack_root=pack_root)
    metadata["capabilities"] = list(resolved.get("capabilities") or [])
    metadata["approval_required_capabilities"] = list(resolved.get("approval_required_capabilities") or [])
    metadata["rejected_capabilities"] = list(resolved.get("rejected_capabilities") or [])
    metadata["requested_capabilities"] = requested_capabilities
    saved = _save_connection_credential(
        provider_id,
        _OAUTH_TOKEN_MATERIAL_TYPE,
        {
            "credentials": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "token_type": metadata.get("token_type", "Bearer"),
            },
        },
        token_metadata={
            **metadata,
            "credential_kind": _OAUTH_TOKEN_MATERIAL_TYPE,
            "scopes": list(resolved.get("scopes") or scopes),
            "capabilities": metadata["capabilities"],
            "approval_required_capabilities": metadata["approval_required_capabilities"],
            "rejected_capabilities": metadata["rejected_capabilities"],
            "requested_capabilities": requested_capabilities,
            "status": "connected",
            "account_label": str(metadata.get("email") or metadata.get("display_name") or provider_id),
        },
        pack_root=pack_root,
    )
    _write_provider_metadata(provider_id, metadata, pack_root=pack_root)
    _reset_ai_client()
    return {
        "success": True,
        "provider_id": provider_id,
        "connected": True,
        "email": metadata.get("email", ""),
        "display_name": metadata.get("display_name", ""),
        "scopes": list(metadata.get("scopes") or []),
        "scope_mode": metadata.get("scope_mode", ""),
        "services": list(metadata.get("services") or []),
        "expires_at": metadata.get("expires_at", ""),
        "has_refresh_token": bool(metadata.get("has_refresh_token")),
        "credential_ref": saved.get("credential_ref", {}),
        "capabilities": metadata["capabilities"],
        "approval_required_capabilities": metadata["approval_required_capabilities"],
        "rejected_capabilities": metadata["rejected_capabilities"],
    }


def _has_valid_access_token(provider_id: str, *, pack_root: Path | None = None) -> bool:
    access_token = _read_provider_oauth_secret(
        provider_id,
        _ACCESS_TOKEN_SECRET_KEYS,
        "ACCESS_TOKEN",
        "access",
        pack_root=pack_root,
    )
    if not access_token:
        return False
    metadata = _provider_metadata(provider_id, pack_root=pack_root)
    expires_at = _parse_datetime(metadata.get("expires_at"))
    if expires_at is None:
        return True
    return expires_at > _now_utc() + timedelta(seconds=_ACCESS_TOKEN_SKEW_SECONDS)


def provider_has_oauth_connection(provider_id: str, *, pack_root: Path | None = None) -> bool:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return False
    if _read_provider_oauth_secret(
        provider_id,
        _REFRESH_TOKEN_SECRET_KEYS,
        "REFRESH_TOKEN",
        "refresh",
        pack_root=pack_root,
    ):
        return True
    return _has_valid_access_token(provider_id, pack_root=pack_root)


def _build_redirect_uri(
    provider_id: str,
    request_headers: dict[str, Any] | None = None,
    *,
    pack_root: Path | None = None,
) -> str:
    provider_id = str(provider_id or "").strip()
    explicit_redirect = _first_env_value(_provider_oauth_env_names(provider_id, "REDIRECT_URI"), pack_root=pack_root)
    if explicit_redirect:
        return explicit_redirect
    base_url = _env_value("RUMI_DEFAULTSPACK_PUBLIC_BASE_URL", pack_root=pack_root).strip().rstrip("/")
    if base_url:
        return f"{base_url}/api/ai/oauth/{urllib.parse.quote(provider_id, safe='')}/callback"
    headers = request_headers or {}
    origin = str(headers.get("Origin") or "").strip().rstrip("/")
    if origin.startswith("http://") or origin.startswith("https://"):
        return f"{origin}/api/ai/oauth/{urllib.parse.quote(provider_id, safe='')}/callback"
    host = str(headers.get("Host") or f"127.0.0.1:{os.environ.get('DEFAULTS_HTTP_PORT', '8766')}").strip()
    proto = str(headers.get("X-Forwarded-Proto") or "http").strip() or "http"
    return f"{proto}://{host}/api/ai/oauth/{urllib.parse.quote(provider_id, safe='')}/callback"


def start_provider_oauth(
    provider_id: str,
    *,
    request_headers: dict[str, Any] | None = None,
    scope_mode: str | None = None,
    services: list[str] | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    provider = _connection_provider(provider_id, pack_root=pack_root)
    if provider is None or provider.oauth is None:
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    if provider_id not in _OAUTH_RUNTIME_PROVIDER_IDS:
        if not provider.oauth.default_scopes:
            return {"success": False, "provider_id": provider_id, "error": "missing scope config", "status": "missing_scope_config"}
        return {"success": False, "provider_id": provider_id, "error": "official app required", "status": "needs_official_app"}
    client = load_provider_client_config(provider_id, pack_root=pack_root)
    if client is None:
        return {"success": False, "provider_id": provider_id, "error": "oauth client config is not saved"}
    default_scope_mode = "google_identity" if provider_id == "google" else "default"
    resolved_scope_mode = str(scope_mode or _scope_mode_from_services(provider_id, services) or default_scope_mode).strip() or default_scope_mode
    if provider_id == "google" and resolved_scope_mode == "default":
        resolved_scope_mode = "google_identity"
    try:
        scopes = _default_scopes(provider_id, resolved_scope_mode, pack_root=pack_root)
    except ValueError as exc:
        return {"success": False, "provider_id": provider_id, "error": str(exc)}
    if not scopes:
        return {"success": False, "provider_id": provider_id, "error": "missing scope config", "status": "missing_scope_config"}
    requested_services = _normalize_requested_services(services)
    if provider_id == "google" and not requested_services:
        scope_details = _GOOGLE_SCOPE_MODE_DETAILS.get(resolved_scope_mode)
        if isinstance(scope_details, dict):
            requested_services = [
                str(item)
                for item in scope_details.get("services", [])
                if str(item or "").strip()
            ]
    redirect_uri = _build_redirect_uri(provider_id, request_headers=request_headers, pack_root=pack_root)
    state = secrets.token_urlsafe(32)
    code_verifier = _generate_code_verifier() if provider.oauth.pkce_supported else ""
    code_challenge = _generate_code_challenge(code_verifier) if code_verifier else ""
    _cleanup_pending_states()
    _pending_states[state] = {
        "provider_id": provider_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "scope_mode": resolved_scope_mode,
        "services": list(requested_services),
        "scopes": list(scopes),
        "created_at": time.time(),
    }
    params = {
        "client_id": str(client.get("client_id") or ""),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if code_challenge:
        params["code_challenge_method"] = "S256"
        params["code_challenge"] = code_challenge
    if provider_id == "google":
        params.update(
            {
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
            }
        )
    authorize_url = f"{provider.oauth.authorization_url}?{urllib.parse.urlencode(params)}"
    return {
        "success": True,
        "provider_id": provider_id,
        "authorize_url": authorize_url,
        "state": state,
        "redirect_uri": redirect_uri,
        "scope_mode": resolved_scope_mode,
        "services": list(requested_services),
        "scopes": list(scopes),
    }


def _http_post_form(url: str, data: dict[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_get_json(url: str, access_token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _exchange_code_for_tokens(
    provider_id: str,
    code: str,
    *,
    redirect_uri: str,
    code_verifier: str,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider = _connection_provider(provider_id, pack_root=pack_root)
    if provider is None or provider.oauth is None:
        raise RuntimeError("oauth provider config is not available")
    client = load_provider_client_config(provider_id, pack_root=pack_root)
    if client is None:
        raise RuntimeError("oauth client config is not saved")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": str(client.get("client_id") or ""),
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    client_secret = str(client.get("client_secret") or "").strip()
    if client_secret:
        payload["client_secret"] = client_secret
    return _http_post_form(provider.oauth.token_url, payload)


def _refresh_access_token(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    provider = _connection_provider(provider_id, pack_root=pack_root)
    if provider is None or provider.oauth is None:
        raise RuntimeError("oauth provider config is not available")
    client = load_provider_client_config(provider_id, pack_root=pack_root)
    if client is None:
        raise RuntimeError("oauth client config is not saved")
    refresh_token = _read_provider_oauth_secret(
        provider_id,
        _REFRESH_TOKEN_SECRET_KEYS,
        "REFRESH_TOKEN",
        "refresh",
        pack_root=pack_root,
    )
    if not refresh_token:
        raise RuntimeError("oauth refresh token is not available")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": str(client.get("client_id") or ""),
    }
    client_secret = str(client.get("client_secret") or "").strip()
    if client_secret:
        payload["client_secret"] = client_secret
    token_data = _http_post_form(provider.oauth.token_url, payload)
    if "refresh_token" not in token_data:
        token_data["refresh_token"] = refresh_token
    return token_data


def _fetch_userinfo(provider_id: str, access_token: str) -> dict[str, Any]:
    provider = _connection_provider(provider_id)
    if provider is None or provider.oauth is None or not provider.oauth.userinfo_url:
        return {}
    return _http_get_json(provider.oauth.userinfo_url, access_token)


def finish_provider_oauth(
    provider_id: str,
    payload: dict[str, Any],
    *,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    error = str(payload.get("error") or "").strip()
    if error:
        return {
            "success": False,
            "provider_id": provider_id,
            "error": str(payload.get("error_description") or error),
            "status_code": 400,
        }
    code = str(payload.get("code") or "").strip()
    state = str(payload.get("state") or "").strip()
    if not code:
        return {"success": False, "provider_id": provider_id, "error": "missing authorization code", "status_code": 400}
    if not state:
        return {"success": False, "provider_id": provider_id, "error": "missing state", "status_code": 400}

    _cleanup_pending_states()
    pending = _pending_states.pop(state, None)
    if pending is None or str(pending.get("provider_id") or "") != provider_id:
        return {"success": False, "provider_id": provider_id, "error": "invalid or expired state", "status_code": 400}

    try:
        token_data = _exchange_code_for_tokens(
            provider_id,
            code,
            redirect_uri=str(pending.get("redirect_uri") or ""),
            code_verifier=str(pending.get("code_verifier") or ""),
            pack_root=pack_root,
        )
    except urllib.error.HTTPError as exc:
        try:
            details = exc.read().decode("utf-8", errors="replace")
        except Exception:
            details = ""
        return {
            "success": False,
            "provider_id": provider_id,
            "error": f"token exchange failed (HTTP {exc.code}) {details}".strip(),
            "status_code": 502,
        }
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        return {
            "success": False,
            "provider_id": provider_id,
            "error": f"token exchange failed: {exc}",
            "status_code": 502,
        }

    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        return {"success": False, "provider_id": provider_id, "error": "oauth token response did not include an access token", "status_code": 502}
    if not str(token_data.get("scope") or "").strip():
        token_data["scope"] = " ".join(str(item) for item in pending.get("scopes") or [] if str(item).strip())
    token_data["scope_mode"] = str(pending.get("scope_mode") or "")
    token_data["services"] = list(pending.get("services") or [])

    userinfo: dict[str, Any] = {}
    try:
        userinfo = _fetch_userinfo(provider_id, access_token)
    except Exception:
        userinfo = {}

    try:
        saved = save_provider_oauth_connection(provider_id, token_data, userinfo=userinfo, pack_root=pack_root)
    except RuntimeError as exc:
        return {"success": False, "provider_id": provider_id, "error": str(exc), "status_code": 500}
    return {
        **saved,
        "provider_id": provider_id,
        "success": True,
        "redirect_uri": str(pending.get("redirect_uri") or ""),
    }


def disconnect_provider_oauth(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    _delete_connection_credential(provider_id, _OAUTH_TOKEN_MATERIAL_TYPE, pack_root=pack_root)
    _delete_secret(
        _secret_key(_ACCESS_TOKEN_SECRET_KEYS, provider_id),
        actor="defaultspack",
        reason=f"disconnect {provider_id} oauth access token",
        pack_root=pack_root,
    )
    _delete_secret(
        _secret_key(_REFRESH_TOKEN_SECRET_KEYS, provider_id),
        actor="defaultspack",
        reason=f"disconnect {provider_id} oauth refresh token",
        pack_root=pack_root,
    )
    _delete_secret(
        _secret_key(_ID_TOKEN_SECRET_KEYS, provider_id),
        actor="defaultspack",
        reason=f"disconnect {provider_id} oauth id token",
        pack_root=pack_root,
    )
    existing = _provider_metadata(provider_id, pack_root=pack_root)
    metadata = {
        **existing,
        "provider_id": provider_id,
        "connected": False,
        "expires_at": "",
        "updated_at": _isoformat(_now_utc()),
        "disconnected_at": _isoformat(_now_utc()),
        "has_refresh_token": False,
    }
    _write_provider_metadata(provider_id, metadata, pack_root=pack_root)
    _reset_ai_client()
    return {"success": True, "provider_id": provider_id, "connected": False}


def get_provider_access_token(provider_id: str, *, pack_root: Path | None = None) -> str | None:
    provider_id = str(provider_id or "").strip()
    if not provider_supports_oauth(provider_id):
        return None
    access_token = _read_provider_oauth_secret(
        provider_id,
        _ACCESS_TOKEN_SECRET_KEYS,
        "ACCESS_TOKEN",
        "access",
        pack_root=pack_root,
    )
    if access_token and _has_valid_access_token(provider_id, pack_root=pack_root):
        return access_token
    if not _read_provider_oauth_secret(
        provider_id,
        _REFRESH_TOKEN_SECRET_KEYS,
        "REFRESH_TOKEN",
        "refresh",
        pack_root=pack_root,
    ):
        return access_token or None
    try:
        token_data = _refresh_access_token(provider_id, pack_root=pack_root)
        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            return None
        try:
            userinfo = _fetch_userinfo(provider_id, access_token)
        except Exception:
            userinfo = {}
        save_provider_oauth_connection(provider_id, token_data, userinfo=userinfo, pack_root=pack_root)
        return access_token
    except Exception:
        return access_token or None


def _provider_config_hint(provider_id: str, connection_status: str, *, client_configured: bool = False) -> str:
    if provider_id == "google":
        return "Import or paste a Google OAuth desktop client JSON to enable Google AI or Workspace browser login."
    if provider_id != "cloudflare":
        return ""
    if connection_status == "missing_self_host_config":
        return "Import a Cloudflare credential JSON, paste a token, or set CLOUDFLARE_API_TOKEN / RUMI_CLOUDFLARE_OAUTH_ACCESS_TOKEN in .env."
    if connection_status == "missing_scope_config":
        if not client_configured:
            return "Set RUMI_CLOUDFLARE_OAUTH_SCOPES for browser OAuth, or import a least-privilege Cloudflare token."
        return "Add Cloudflare scopes to the saved client JSON before connecting, or import a token directly."
    if connection_status == "not_connected":
        return "Cloudflare OAuth is ready. Click Connect in browser to finish consent."
    if connection_status == "connected":
        return "Cloudflare token is available. Add requested capabilities in the token import or .env only when deploy access is needed."
    return ""


def _localized_provider_label(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        for key in ("en", "ja"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        for candidate in value.values():
            label = str(candidate or "").strip()
            if label:
                return label
        return fallback
    label = str(value or "").strip()
    return label or fallback


def provider_oauth_status(
    provider_id: str,
    *,
    pack_root: Path | None = None,
    active_diagnostics: bool = False,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    provider = _connection_provider(provider_id, pack_root=pack_root)
    provider_label = _localized_provider_label(provider.display_name, provider_id) if provider else provider_id
    supported = provider_supports_oauth(provider_id)
    client = load_provider_client_config(provider_id, pack_root=pack_root) if supported else None
    metadata = _provider_metadata(provider_id, pack_root=pack_root) if supported else {}
    if supported:
        metadata = {**_provider_context_from_env(provider_id, pack_root=pack_root), **metadata}
        env_requested_capabilities = _requested_capabilities_from_env(provider_id, pack_root=pack_root)
        if env_requested_capabilities and not metadata.get("requested_capabilities"):
            metadata["requested_capabilities"] = env_requested_capabilities
    connected = provider_has_oauth_connection(provider_id, pack_root=pack_root) if supported else False
    default_scopes = _default_scopes(provider_id, pack_root=pack_root) if supported else list(provider.oauth.default_scopes if provider and provider.oauth else [])
    if connected:
        connection_status = "connected"
        status_label = "Connected"
        disabled_reason = ""
    elif provider is None or provider.oauth is None:
        connection_status = "unsupported"
        status_label = "Unsupported"
        disabled_reason = "Official app required"
    elif provider_id not in _OAUTH_RUNTIME_PROVIDER_IDS:
        manifest_scopes = list(provider.oauth.default_scopes if provider and provider.oauth else [])
        if not manifest_scopes:
            connection_status = "missing_scope_config"
            status_label = "Missing scope config"
            disabled_reason = "Configure self-host OAuth"
        else:
            connection_status = "needs_official_app"
            status_label = "Official app required"
            disabled_reason = "Official app required"
    elif not default_scopes:
        connection_status = "missing_scope_config"
        status_label = "Missing scope config"
        disabled_reason = "Configure self-host OAuth"
    elif client is None:
        connection_status = "missing_self_host_config"
        status_label = "Client config needed"
        disabled_reason = "Configure self-host OAuth"
    else:
        connection_status = "not_connected"
        status_label = "Ready to connect"
        disabled_reason = ""
    scope_mode = str(metadata.get("scope_mode") or "google_identity").strip() if provider_id == "google" else ""
    try:
        status_scopes = list(metadata.get("scopes") or _default_scopes(provider_id, scope_mode or None, pack_root=pack_root))
    except ValueError:
        status_scopes = list(metadata.get("scopes") or default_scopes)
    credential_ref = _connection_credential_ref(provider_id, _OAUTH_TOKEN_MATERIAL_TYPE, pack_root=pack_root) if supported else {}
    client_credential_ref = _connection_credential_ref(provider_id, _OAUTH_CLIENT_MATERIAL_TYPE, pack_root=pack_root) if supported else {}
    capability_metadata = {
        **metadata,
        "credential_kind": _OAUTH_TOKEN_MATERIAL_TYPE if credential_ref or connected else str(metadata.get("credential_kind") or ""),
        "scopes": status_scopes,
    }
    if metadata.get("requested_capabilities"):
        capability_metadata["requested_capabilities"] = _normalize_scope_list(metadata.get("requested_capabilities"))
    resolved = _resolve_connection_capabilities(
        provider_id,
        capability_metadata,
        pack_root=pack_root,
    ) if supported else {"capabilities": [], "scopes": status_scopes, "approval_required_capabilities": [], "rejected_capabilities": []}
    capabilities = list(metadata.get("capabilities") or resolved.get("capabilities") or [])
    approval_required_capabilities = list(
        metadata.get("approval_required_capabilities")
        or resolved.get("approval_required_capabilities")
        or []
    )
    rejected_capabilities = list(metadata.get("rejected_capabilities") or resolved.get("rejected_capabilities") or [])
    cloudflare_sdk = {}
    cloudflare_environment = {}
    if provider_id == "cloudflare":
        try:
            from core_runtime.cloudflare.sdk_client import (
                cloudflare_sdk_status,
            )

            cloudflare_sdk = cloudflare_sdk_status()
        except Exception:
            cloudflare_sdk = {
                "available": False,
                "status": "sdk_missing",
                "package": "cloudflare",
                "detail": "Cloudflare Python SDK status could not be loaded.",
            }
        try:
            from core_runtime.cloudflare.diagnostics import (
                cloudflare_environment_status,
            )

            active = active_diagnostics or str(os.environ.get("RUMI_CLOUDFLARE_ACTIVE_DIAGNOSTICS") or "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            cloudflare_api_token = get_provider_access_token(provider_id, pack_root=pack_root) if active else None
            cloudflare_environment = cloudflare_environment_status(
                active=active,
                api_token=cloudflare_api_token,
                connector_root=pack_root or _pack_root(),
            )
        except Exception:
            cloudflare_environment = {
                "schema": "rumi.cloudflare.environment.v1",
                "active": False,
                "status": "error",
                "blockers": [
                    {
                        "code": "CLOUDFLARE_DIAGNOSTICS_UNAVAILABLE",
                        "message": "Cloudflare environment diagnostics could not be loaded.",
                    }
                ],
            }
    provisioning = {"sdk_status": cloudflare_sdk.get("status", "")} if cloudflare_sdk else {}
    if cloudflare_environment:
        provisioning.update(
            {
                "environment": cloudflare_environment,
                "environment_status": cloudflare_environment.get("status", ""),
                "runner_deploy_ready": bool(cloudflare_environment.get("runner_deploy_ready")),
                "sandbox_ready": bool(cloudflare_environment.get("sandbox_ready")),
                "pages_ready": bool(cloudflare_environment.get("pages_ready")),
                "named_tunnel_ready": bool(cloudflare_environment.get("named_tunnel_ready")),
                "stable_pc_tunnel_ready": bool(cloudflare_environment.get("stable_pc_tunnel_ready")),
                "pc_tool_bridge_ready": bool(cloudflare_environment.get("pc_tool_bridge_ready")),
                "blockers": cloudflare_environment.get("blockers") or [],
                "constraints": cloudflare_environment.get("constraints") or {},
            }
        )
    return {
        "supported": supported,
        "backend_supported": provider_id in _OAUTH_RUNTIME_PROVIDER_IDS,
        "provider_id": provider_id,
        "display_label": provider_label,
        "service_kind": str(provider.service_kind if provider else ""),
        "auth_type": str(provider.auth_type if provider else ""),
        "client_configured": client is not None,
        "client_label": _client_id_label(str((client or {}).get("client_id") or "")),
        "client_source": str((client or {}).get("source") or ""),
        "client_can_clear": str((client or {}).get("source") or "") == "secret_store",
        "client_credential_ref": client_credential_ref,
        "connected": connected,
        "connect_enabled": supported and client is not None and bool(default_scopes),
        "connection_status": connection_status,
        "status": connection_status,
        "status_label": status_label,
        "disabled_reason": disabled_reason,
        "display_name": str(metadata.get("display_name") or provider_label).strip(),
        "email": str(metadata.get("email") or "").strip(),
        "picture_url": str(metadata.get("picture_url") or "").strip(),
        "scopes": status_scopes,
        "capabilities": capabilities,
        "approval_required_capabilities": approval_required_capabilities,
        "rejected_capabilities": rejected_capabilities,
        "default_scopes": default_scopes,
        "scope_mode": scope_mode,
        "scope_modes": _google_scope_mode_rows(pack_root=pack_root) if provider_id == "google" else [],
        "services": list(metadata.get("services") or []),
        "cloudflare_sdk": cloudflare_sdk,
        "cloudflare_environment": cloudflare_environment,
        "provisioning": provisioning,
        "expires_at": str(metadata.get("expires_at") or ""),
        "has_refresh_token": bool(metadata.get("has_refresh_token")),
        "account_id_configured": bool(metadata.get("account_id")),
        "zone_id_configured": bool(metadata.get("zone_id")),
        "credential_ref": credential_ref,
        "redirect_path": f"/api/ai/oauth/{provider_id}/callback" if supported else "",
        "config_hint": _provider_config_hint(provider_id, connection_status, client_configured=client is not None),
    }


def provider_oauth_statuses(
    *,
    pack_root: Path | None = None,
    active_diagnostics: bool = False,
) -> dict[str, dict[str, Any]]:
    return {
        provider_id: provider_oauth_status(
            provider_id,
            pack_root=pack_root,
            active_diagnostics=active_diagnostics and provider_id == "cloudflare",
        )
        for provider_id in sorted(_connection_provider_ids(pack_root=pack_root) | _OAUTH_RUNTIME_PROVIDER_IDS)
    }
