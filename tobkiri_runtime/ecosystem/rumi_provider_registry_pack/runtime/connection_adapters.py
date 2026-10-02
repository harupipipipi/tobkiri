"""Provider-specific connection metadata adapters owned by the registry Pack."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from core_runtime.connections.adapter import GenericConnectionAdapter
from core_runtime.connections.models import ConnectionProvider
from core_runtime.connections.templates import CredentialBundle


_ACCOUNT_ID_ENV = ("RUMI_CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID")
_ZONE_ID_ENV = ("RUMI_CLOUDFLARE_ZONE_ID", "CLOUDFLARE_ZONE_ID")
_REQUESTED_CAPABILITIES_ENV = (
    "RUMI_CLOUDFLARE_OAUTH_REQUESTED_CAPABILITIES",
    "CLOUDFLARE_REQUESTED_CAPABILITIES",
    "CLOUDFLARE_API_TOKEN_REQUESTED_CAPABILITIES",
)
_SECRET_METADATA_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "token",
    "client_secret",
    "private_key",
}


class CloudflareConnectionAdapter(GenericConnectionAdapter):
    def normalize_token_metadata(
        self,
        *,
        provider: ConnectionProvider,
        credential_bundle: CredentialBundle,
        secret_material: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = super().normalize_token_metadata(
            provider=provider,
            credential_bundle=credential_bundle,
            secret_material=secret_material,
        )
        token_metadata = _mapping(credential_bundle.token_metadata)
        context = {
            **_mapping(_mapping(secret_material.get("credentials")).get("context")),
            **_mapping(secret_material.get("context")),
        }

        account_id = _first_text(
            token_metadata.get("account_id"),
            _nested_text(token_metadata.get("account"), "id"),
            context.get("account_id"),
            _first_env(_ACCOUNT_ID_ENV),
        )
        zone_id = _first_text(
            token_metadata.get("zone_id"),
            _nested_text(token_metadata.get("zone"), "id"),
            context.get("zone_id"),
            _first_env(_ZONE_ID_ENV),
        )
        requested_capabilities = _normal_string_list(metadata.get("requested_capabilities"))
        if not requested_capabilities and not _normal_string_list(metadata.get("scopes")):
            requested_capabilities = _normal_string_list(
                token_metadata.get("requested_capabilities")
                or token_metadata.get("requestedCapabilities")
                or context.get("requested_capabilities")
                or _first_env(_REQUESTED_CAPABILITIES_ENV)
            )

        account_label = _cloudflare_account_label(
            metadata,
            token_metadata=token_metadata,
            context=context,
            fallback=str(metadata.get("account_label") or provider.provider_id),
        )
        clean_metadata = {
            key: value
            for key, value in metadata.items()
            if str(key).lower() not in _SECRET_METADATA_KEYS
        }
        clean_metadata.update(
            {
                "provider_id": "cloudflare",
                "account_label": account_label,
                "requested_capabilities": requested_capabilities,
                "account_id": account_id,
                "zone_id": zone_id,
                "account_id_configured": bool(account_id),
                "zone_id_configured": bool(zone_id),
                "cloudflare_account_status": "configured" if account_id else "missing_account_id",
                "status": str(metadata.get("status") or "not_connected"),
            }
        )
        return clean_metadata


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        clean = str(os.environ.get(name) or "").strip()
        if clean:
            return clean
    return ""


def _nested_text(value: Any, key: str) -> str:
    return str(_mapping(value).get(key) or "").strip()


def _normal_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [item for item in text.replace(",", " ").split() if item]


def _cloudflare_account_label(
    metadata: Mapping[str, Any],
    *,
    token_metadata: Mapping[str, Any],
    context: Mapping[str, Any],
    fallback: str,
) -> str:
    account_name = _first_text(
        token_metadata.get("account_name"),
        token_metadata.get("account_label"),
        _nested_text(token_metadata.get("account"), "name"),
        context.get("account_name"),
    )
    if account_name:
        return f"Cloudflare: {account_name}"
    existing = _first_text(metadata.get("account_label"), fallback)
    return existing if existing.lower().startswith("cloudflare") else existing
