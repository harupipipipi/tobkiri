from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .models import ConnectionProvider
from .templates import CredentialBundle


class ConnectionAdapter(Protocol):
    def normalize_token_metadata(
        self,
        *,
        provider: ConnectionProvider,
        credential_bundle: CredentialBundle,
        secret_material: dict[str, Any],
    ) -> dict[str, Any]: ...


class GenericConnectionAdapter:
    def normalize_token_metadata(
        self,
        *,
        provider: ConnectionProvider,
        credential_bundle: CredentialBundle,
        secret_material: dict[str, Any],
    ) -> dict[str, Any]:
        raw_credentials = secret_material.get("credentials")
        credentials: dict[str, Any] = (
            raw_credentials if isinstance(raw_credentials, dict) else {}
        )
        metadata = dict(credential_bundle.token_metadata)
        scopes = credential_bundle.scopes or _normalize_scopes(metadata.get("scope") or metadata.get("scopes") or credentials.get("scope"))
        expires_at = credential_bundle.expires_at or str(metadata.get("expires_at") or "").strip()
        expires_at = expires_at or _expires_at_from_seconds(metadata.get("expires_in") or credentials.get("expires_in"))
        account_label = (
            credential_bundle.account_label
            or str(metadata.get("email") or "").strip()
            or str(metadata.get("name") or metadata.get("display_name") or "").strip()
            or provider.provider_id
        )
        material_type = str(secret_material.get("material_type") or credential_bundle.material_type or "").strip()
        return {
            **metadata,
            "provider_id": provider.provider_id,
            "connection_id": credential_bundle.connection_id,
            "account_label": account_label,
            "material_type": material_type,
            "credential_kind": str(metadata.get("credential_kind") or material_type),
            "scopes": scopes,
            "requested_capabilities": list(credential_bundle.requested_capabilities),
            "expires_at": expires_at,
            "status": "connected" if credentials else "not_connected",
            "has_refresh_token": bool(credentials.get("refresh_token")),
        }


def load_connection_adapter(spec: dict[str, Any] | None) -> ConnectionAdapter:
    spec = spec or {}
    python_path = str(spec.get("python") or "").strip()
    if not python_path:
        return GenericConnectionAdapter()
    try:
        module_name, class_name = python_path.split(":", 1)
        module = importlib.import_module(module_name)
        adapter_type = getattr(module, class_name)
        return adapter_type()
    except Exception:
        if spec.get("sdk_optional", False):
            return GenericConnectionAdapter()
        raise


def _normalize_scopes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [item for item in text.replace(",", " ").split() if item]


def _expires_at_from_seconds(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
