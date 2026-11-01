"""Redacted legacy projection over provider health and credential contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract

from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
    list_provider_catalog,
)

CONTRACT_VERSION = "provider-health.v2-compat"
_HEALTH_CONTRACT = "rumi.resource.ai.provider.health.v1"
_CREDENTIAL_STATUS = "rumi.resource.credential.status.v1"


def provider_health_report(
    *,
    pack_root: Path | None = None,
    active_provider_ids: Iterable[str] | None = None,
    provider_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return health without probing providers or reading secret sources."""
    del pack_root, active_provider_ids
    allowed = {str(item).strip() for item in provider_ids or [] if str(item).strip()}
    health = _invoke(_HEALTH_CONTRACT, "get", {})
    credentials = _invoke(_CREDENTIAL_STATUS, "list", {})
    health_items = health.get("providers") if isinstance(health, Mapping) else []
    credential_items = (
        credentials.get("credentials")
        if isinstance(credentials, Mapping)
        else []
    )
    health_items = health_items if isinstance(health_items, list) else []
    credential_items = (
        credential_items if isinstance(credential_items, list) else []
    )
    health_by_id = {
        str(item.get("provider_instance_id") or ""): dict(item)
        for item in health_items
        if isinstance(item, Mapping)
    }
    credential_by_id = {
        str(item.get("provider_instance_id") or ""): dict(item)
        for item in credential_items
        if isinstance(item, Mapping)
    }
    providers = []
    for catalog in list_provider_catalog():
        provider_id = str(catalog.get("provider_id") or "")
        if not provider_id or (allowed and provider_id not in allowed):
            continue
        instance_id = f"provider.{provider_id}"
        evidence = health_by_id.get(instance_id, {})
        credential = credential_by_id.get(instance_id)
        status = str(evidence.get("status") or "unknown")
        providers.append(
            {
                "provider_id": provider_id,
                "display_name": str(catalog.get("display_name") or provider_id),
                "kind": str(catalog.get("kind") or "unknown"),
                "status": status,
                "health_code": status,
                "runtime": {
                    "configured": bool(catalog.get("configured")),
                    "status": status,
                    "supports_invoke": True,
                    "active": bool(catalog.get("configured")),
                    "observed_at": evidence.get("observed_at"),
                    "verified": bool(evidence.get("verified", False)),
                },
                "credential": {
                    "configured": credential is not None,
                    "source": "opaque_handle" if credential else "none",
                    "masked": credential is not None,
                    "scopes": list(credential.get("scopes") or []) if credential else [],
                },
                "models": {"default_model": "", "default_model_for": {}},
                "diagnostics": [
                    {
                        "severity": "info",
                        "code": "remote_health_unknown"
                        if status == "unknown" else "verified_health",
                        "message": "Remote health remains unknown until verified."
                        if status == "unknown" else "Health is backed by verified evidence.",
                    }
                ],
            }
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(providers),
            "configured": sum(
                1 for item in providers if item["runtime"]["configured"]
            ),
            "unknown": sum(1 for item in providers if item["status"] == "unknown"),
            "warnings": 0,
            "errors": 0,
        },
        "providers": providers,
    }


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("interface registry is unavailable")
    return invoke_global_contract(registry, contract_id, operation, dict(payload))
