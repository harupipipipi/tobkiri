from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core_runtime.connections.credential_store import CredentialEnvelope
from core_runtime.connections.import_service import ConnectionImportService
from core_runtime.connections.permission_resolver import resolve_connection_permissions
from core_runtime.connections.templates import CREDENTIAL_BUNDLE_SCHEMA

_PREFIX = "RUMICONN"
_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9_]+")


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _connection_manifest_root(pack_root: Path | None = None) -> Path:
    candidate = (pack_root or _pack_root()) / "config" / "settings_control_center" / "providers"
    if candidate.exists():
        return candidate
    return _pack_root() / "config" / "settings_control_center" / "providers"


def _connection_registry(pack_root: Path | None = None):
    from core_runtime.connections.registry import ConnectionsRegistry

    registry = ConnectionsRegistry()
    root = _connection_manifest_root(pack_root)
    if root.exists():
        registry.load_manifest_dir(root)
    return registry


def _connection_provider(provider_id: str, *, pack_root: Path | None = None):
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return None
    try:
        return _connection_registry(pack_root).get(provider_id)
    except KeyError:
        return None


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
    return _secrets_dir(pack_root) / "connection_credentials.json"


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str, *, fallback: str, max_length: int) -> str:
    normalized = _SLUG_PATTERN.sub("_", str(value or "").strip()).strip("_").upper()
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        normalized = fallback
    return normalized[:max_length]


def connection_secret_key(provider_id: str, connection_id: str = "default", material_type: str = "credential_bundle") -> str:
    provider_slug = _slug(provider_id, fallback="PROVIDER", max_length=16)
    material_slug = _slug(material_type, fallback="CREDENTIAL", max_length=22)
    connection_slug = _slug(connection_id, fallback="DEFAULT", max_length=18)
    return f"{_PREFIX}_{provider_slug}_{material_slug}_{connection_slug}"[:64]


def _read_metadata(pack_root: Path | None = None) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_metadata_path(pack_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _write_metadata(data: dict[str, dict[str, Any]], pack_root: Path | None = None) -> None:
    path = _metadata_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _get_store(pack_root: Path | None = None):
    from core_runtime.secrets_store import SecretsStore

    return SecretsStore(str(_secrets_dir(pack_root)))


class DefaultspackConnectionCredentialStore:
    def __init__(self, *, pack_root: Path | None = None) -> None:
        self.pack_root = pack_root

    def put(self, provider_id: str, connection_id: str, material_type: str, secret_material: dict[str, Any]) -> CredentialEnvelope:
        key = connection_secret_key(provider_id, connection_id, material_type)
        now = _now_ts()
        metadata = _read_metadata(self.pack_root)
        existing = metadata.get(key, {})
        payload = {
            **dict(secret_material),
            "provider_id": str(provider_id or "").strip(),
            "connection_id": str(connection_id or "default").strip() or "default",
            "material_type": str(material_type or "credential_bundle").strip() or "credential_bundle",
        }
        result = _get_store(self.pack_root).set_secret(
            key,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            actor="defaultspack",
            reason=f"save connection credential {provider_id}:{material_type}",
        )
        if not result.success:
            raise RuntimeError(result.error or f"failed to save connection credential {key}")
        metadata[key] = {
            **existing,
            "credential_id": key,
            "provider_id": payload["provider_id"],
            "connection_id": payload["connection_id"],
            "material_type": payload["material_type"],
            "created_at": str(existing.get("created_at") or now),
            "updated_at": now,
            "key_version": "defaultspack-secrets-v1",
        }
        token_metadata = payload.get("token_metadata")
        if isinstance(token_metadata, dict):
            for field in (
                "scopes",
                "capabilities",
                "approval_required_capabilities",
                "rejected_capabilities",
                "expires_at",
                "status",
                "account_label",
                "credential_kind",
            ):
                if field in token_metadata:
                    metadata[key][field] = token_metadata[field]
        _write_metadata(metadata, self.pack_root)
        return CredentialEnvelope(
            credential_id=key,
            provider_id=payload["provider_id"],
            connection_id=payload["connection_id"],
            material_type=payload["material_type"],
            ciphertext="",
            key_version="defaultspack-secrets-v1",
        )

    def get(self, credential_id: str) -> dict[str, Any]:
        raw_value = _get_store(self.pack_root)._internal_read_value(
            str(credential_id or ""),
            caller_id=f"defaultspack.connections:{credential_id}",
        )
        if not raw_value:
            raise KeyError(f"Unknown credential: {credential_id}")
        payload = json.loads(raw_value)
        if not isinstance(payload, dict):
            raise KeyError(f"Invalid credential payload: {credential_id}")
        return payload

    def delete(self, credential_id: str) -> None:
        try:
            _get_store(self.pack_root).delete_secret(
                str(credential_id or ""),
                actor="defaultspack",
                reason="delete connection credential",
            )
        except Exception:
            pass
        metadata = _read_metadata(self.pack_root)
        if metadata.pop(str(credential_id or ""), None) is not None:
            _write_metadata(metadata, self.pack_root)


def save_connection_credential(
    provider_id: str,
    material_type: str,
    secret_material: dict[str, Any],
    *,
    connection_id: str = "default",
    token_metadata: dict[str, Any] | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    payload = dict(secret_material)
    if token_metadata:
        payload["token_metadata"] = dict(token_metadata)
    store = DefaultspackConnectionCredentialStore(pack_root=pack_root)
    envelope = store.put(provider_id, connection_id, material_type, payload)
    return {
        "success": True,
        "credential_ref": _credential_ref_from_envelope(envelope),
    }


def read_connection_credential(
    provider_id: str,
    material_type: str,
    *,
    connection_id: str = "default",
    pack_root: Path | None = None,
) -> dict[str, Any]:
    key = connection_secret_key(provider_id, connection_id, material_type)
    try:
        return DefaultspackConnectionCredentialStore(pack_root=pack_root).get(key)
    except Exception:
        return {}


def delete_connection_credential(
    provider_id: str,
    material_type: str,
    *,
    connection_id: str = "default",
    pack_root: Path | None = None,
) -> None:
    key = connection_secret_key(provider_id, connection_id, material_type)
    DefaultspackConnectionCredentialStore(pack_root=pack_root).delete(key)


def connection_credential_ref(
    provider_id: str,
    material_type: str,
    *,
    connection_id: str = "default",
    pack_root: Path | None = None,
) -> dict[str, str]:
    key = connection_secret_key(provider_id, connection_id, material_type)
    metadata = _read_metadata(pack_root).get(key, {})
    if not metadata:
        return {}
    try:
        exists = _get_store(pack_root).has_secret(key)
    except Exception:
        exists = False
    if not exists:
        return {}
    return {
        "credential_id": key,
        "provider_id": str(metadata.get("provider_id") or provider_id),
        "connection_id": str(metadata.get("connection_id") or connection_id),
        "key_version": str(metadata.get("key_version") or "defaultspack-secrets-v1"),
    }


def import_connection_bundle(
    raw_bundle: str | dict[str, Any],
    *,
    provider_id: str = "",
    pack_root: Path | None = None,
) -> dict[str, Any]:
    registry = _connection_registry(pack_root)
    bundle = _coerce_connection_import_payload(raw_bundle, provider_id=provider_id)
    result = ConnectionImportService(
        registry,
        DefaultspackConnectionCredentialStore(pack_root=pack_root),
    ).import_connection(bundle)
    return result


def resolve_capabilities_for_provider(provider_id: str, token_metadata: dict[str, Any], *, pack_root: Path | None = None) -> dict[str, Any]:
    provider = _connection_provider(provider_id, pack_root=pack_root)
    if provider is None:
        return {
            "scopes": [],
            "capabilities": [],
            "approval_required_capabilities": [],
            "rejected_capabilities": [],
        }
    resolved = resolve_connection_permissions(provider, token_metadata)
    return resolved.to_dict()


def _coerce_connection_import_payload(raw_bundle: str | dict[str, Any], *, provider_id: str = "") -> dict[str, Any] | str:
    if isinstance(raw_bundle, str):
        text = raw_bundle.strip()
        if not text:
            raise ValueError("credential bundle JSON or token is required")
        if text.startswith("{"):
            payload = json.loads(text)
            if isinstance(payload, dict):
                return _coerce_connection_import_payload(payload, provider_id=provider_id)
        if "=" in text:
            return _bundle_from_flat_secret_payload(
                _parse_connection_import_env_text(text),
                provider_id=provider_id,
            )
        if provider_id:
            return _bundle_from_flat_secret_payload({"access_token": text}, provider_id=provider_id)
        return raw_bundle

    payload = dict(raw_bundle)
    schema = str(payload.get("schema") or "").strip()
    if schema == CREDENTIAL_BUNDLE_SCHEMA:
        return payload
    if isinstance(payload.get("credential_bundle"), dict):
        return _coerce_connection_import_payload(dict(payload["credential_bundle"]), provider_id=provider_id)
    if isinstance(payload.get("connection"), dict):
        return _coerce_connection_import_payload(dict(payload["connection"]), provider_id=provider_id)
    if isinstance(payload.get("bundle"), dict):
        return _coerce_connection_import_payload(dict(payload["bundle"]), provider_id=provider_id)
    return _bundle_from_flat_secret_payload(payload, provider_id=provider_id)


def _bundle_from_flat_secret_payload(payload: dict[str, Any], *, provider_id: str = "") -> dict[str, Any]:
    resolved_provider_id = str(
        provider_id
        or payload.get("provider_id")
        or payload.get("provider")
        or payload.get("RUMI_CONNECTION_PROVIDER_ID")
        or payload.get("PROVIDER_ID")
        or ""
    ).strip()
    if not resolved_provider_id:
        raise ValueError("credential bundle provider_id is required")

    prefix = _slug(resolved_provider_id, fallback="PROVIDER", max_length=32)
    token = _first_flat_value(
        payload,
        [
            "access_token",
            "ACCESS_TOKEN",
            "token",
            "TOKEN",
            "api_token",
            "API_TOKEN",
            f"RUMI_DEFAULTSPACK_{prefix}_OAUTH_ACCESS_TOKEN",
            f"RUMI_{prefix}_OAUTH_ACCESS_TOKEN",
            f"RUMIOAUTH_{prefix}_ACCESS_TOKEN",
            f"{prefix}_API_TOKEN",
        ]
        + (["CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"] if resolved_provider_id == "cloudflare" else []),
    )
    refresh_token = _first_flat_value(
        payload,
        [
            "refresh_token",
            "REFRESH_TOKEN",
            f"RUMI_DEFAULTSPACK_{prefix}_OAUTH_REFRESH_TOKEN",
            f"RUMI_{prefix}_OAUTH_REFRESH_TOKEN",
            f"RUMIOAUTH_{prefix}_REFRESH_TOKEN",
        ],
    )
    id_token = _first_flat_value(
        payload,
        [
            "id_token",
            "ID_TOKEN",
            f"RUMI_DEFAULTSPACK_{prefix}_OAUTH_ID_TOKEN",
            f"RUMI_{prefix}_OAUTH_ID_TOKEN",
            f"RUMIOAUTH_{prefix}_ID_TOKEN",
        ],
    )
    credentials = dict(payload.get("credentials") or {}) if isinstance(payload.get("credentials"), dict) else {}
    if token:
        credentials.setdefault("access_token", token)
    if refresh_token:
        credentials.setdefault("refresh_token", refresh_token)
    if id_token:
        credentials.setdefault("id_token", id_token)
    if not credentials:
        raise ValueError("credential bundle does not include credential material")

    scopes = _normalize_connection_list(
        _first_flat_value(
            payload,
            [
                "scopes",
                "scope",
                "SCOPES",
                "SCOPE",
                f"RUMI_DEFAULTSPACK_{prefix}_OAUTH_SCOPES",
                f"RUMI_{prefix}_OAUTH_SCOPES",
            ],
        )
    )
    requested_capabilities = _normalize_connection_list(
        _first_flat_value(
            payload,
            [
                "requested_capabilities",
                "requestedCapabilities",
                "REQUESTED_CAPABILITIES",
                f"RUMI_DEFAULTSPACK_{prefix}_OAUTH_REQUESTED_CAPABILITIES",
                f"RUMI_{prefix}_OAUTH_REQUESTED_CAPABILITIES",
                f"RUMI_{prefix}_REQUESTED_CAPABILITIES",
            ],
        )
    )
    token_metadata = dict(payload.get("token_metadata") or payload.get("metadata") or {}) if isinstance(
        payload.get("token_metadata") or payload.get("metadata"),
        dict,
    ) else {}
    for source_key, target_key in (
        ("account_id", "account_id"),
        ("ACCOUNT_ID", "account_id"),
        (f"RUMI_{prefix}_ACCOUNT_ID", "account_id"),
        (f"{prefix}_ACCOUNT_ID", "account_id"),
        ("zone_id", "zone_id"),
        ("ZONE_ID", "zone_id"),
        (f"RUMI_{prefix}_ZONE_ID", "zone_id"),
        (f"{prefix}_ZONE_ID", "zone_id"),
        ("email", "email"),
        ("display_name", "display_name"),
        ("name", "display_name"),
    ):
        value = str(payload.get(source_key) or "").strip()
        if value:
            token_metadata[target_key] = value
    if scopes:
        token_metadata["scopes"] = scopes
    if requested_capabilities:
        token_metadata["requested_capabilities"] = requested_capabilities

    return {
        "schema": CREDENTIAL_BUNDLE_SCHEMA,
        "provider_id": resolved_provider_id,
        "connection_id": str(payload.get("connection_id") or payload.get("account_id") or "default").strip() or "default",
        "account_label": str(payload.get("account_label") or payload.get("name") or f"{resolved_provider_id} imported token").strip(),
        "material_type": str(payload.get("material_type") or "oauth2_token").strip() or "oauth2_token",
        "credentials": credentials,
        "scopes": scopes,
        "requested_capabilities": requested_capabilities,
        "expires_at": str(payload.get("expires_at") or token_metadata.get("expires_at") or "").strip(),
        "token_metadata": token_metadata,
    }


def _parse_connection_import_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _first_flat_value(payload: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            if value:
                return value
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_connection_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item for item in text.replace(",", " ").split() if item]


def _credential_ref_from_envelope(envelope: CredentialEnvelope) -> dict[str, str]:
    return {
        "credential_id": envelope.credential_id,
        "provider_id": envelope.provider_id,
        "connection_id": envelope.connection_id,
        "key_version": envelope.key_version,
    }
