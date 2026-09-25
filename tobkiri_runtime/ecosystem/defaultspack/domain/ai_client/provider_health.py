"""Redacted legacy projection over provider health and credential contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)

CONTRACT_VERSION = "provider-health.v2-compat"
_HEALTH_CONTRACT = "tobkiri.resource.ai.provider.health.v1"
_CREDENTIAL_STATUS = "tobkiri.resource.credential.status.v1"


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
    health_by_id: dict[str, dict[str, Any]] = {
        str(item.get("provider_instance_id") or ""): dict(item)
        for item in health_items
        if isinstance(item, Mapping)
    }
    credential_by_id = {
        str(item.get("provider_instance_id") or ""): dict(item)
        for item in credential_items
        if isinstance(item, Mapping)
    }
    instance_ids = set(health_by_id) | set(credential_by_id)
    if allowed:
        instance_ids |= {f"provider.{provider_id}" for provider_id in allowed}
    providers = []
    for instance_id in sorted(instance_ids):
        provider_id = instance_id.removeprefix("provider.")
        if not provider_id or (allowed and provider_id not in allowed):
            continue
        evidence = health_by_id.get(instance_id, {})
        credential = credential_by_id.get(instance_id)
        status = str(evidence.get("status") or "unknown")
        observed_at = evidence.get("observed_at")
        credential_updated_at = credential.get("updated_at") if credential else None
        verified = bool(evidence.get("verified", False))
        credential_usability = str(
            credential.get("usability") or "unknown"
        ) if credential else "unknown"
        if credential_usability == "invalid":
            status = "invalid"
            verified = True
        elif verified and _is_older_evidence(observed_at, credential_updated_at):
            verified = False
            status = "unknown"
        providers.append(
            {
                "provider_id": provider_id,
                "status": status,
                "health_code": status,
                "runtime": {
                    "configured": credential is not None,
                    "status": status,
                    "supports_invoke": True,
                    "active": credential is not None,
                    "observed_at": observed_at,
                    "verified": verified,
                },
                "credential": {
                    "configured": credential is not None,
                    "source": str(credential.get("source") or "provider_default")
                    if credential else "none",
                    "masked": credential is not None,
                    "scopes": list(credential.get("scopes") or []) if credential else [],
                    "opaque_id": credential.get("opaque_id") if credential else None,
                    "updated_at": credential_updated_at,
                    "reason_code": credential.get("reason_code")
                    if credential else "not_configured",
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
    return invoke_global_contract(
        registry,
        contract_id,
        operation,
        {"profile_id": captured_profile_id(registry), **dict(payload)},
    )


def _is_older_evidence(observed_at: Any, credential_updated_at: Any) -> bool:
    """Return true when health evidence predates the active credential source."""

    try:
        observed = float(observed_at)
    except (TypeError, ValueError):
        return credential_updated_at is not None
    if credential_updated_at is None:
        return False
    try:
        updated = datetime.fromisoformat(
            str(credential_updated_at).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return True
    return observed < updated
