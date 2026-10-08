"""Secret-free finite API projections from captured Host registry resources."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.provider_compiler.model_access import (
    effective_model_access,
)
from tobkiri_protocol.provider_compiler.native_filters import (
    CAPABILITY_REVISION, is_official_openrouter,
)


def project_model_access(
    registry_snapshot: Mapping[str, Any],
    provider_instance_id: str,
    *,
    native_capability_confirmed: bool = False,
) -> dict[str, Any]:
    """Project policy for an exact saved connection without exposing its handle.

    The caller must obtain the registry through captured Host profile authority.
    Capability confirmation is server-owned evidence, never a client input.
    """
    providers = registry_snapshot.get("providers")
    matches = [item for item in providers if isinstance(item, Mapping)
               and item.get("provider_instance_id") == provider_instance_id]
    if len(matches) != 1:
        raise KeyError("provider connection is not configured")
    connection = matches[0]
    return {
        "profile_id": registry_snapshot["profile_id"],
        "provider_instance_id": provider_instance_id,
        "registry_revision": registry_snapshot["revision"],
        "model_access": effective_model_access(connection),
        "native_capability": (
            CAPABILITY_REVISION if native_capability_confirmed
            and is_official_openrouter(connection) else None
        ),
    }


def project_model_access_catalog(
    registry_snapshot: Mapping[str, Any], provider_instance_id: str,
    catalog_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind public provider IDs to an opaque connection; never claim key access."""
    binding = project_model_access(registry_snapshot, provider_instance_id)
    connection = next(item for item in registry_snapshot["providers"]
                      if item["provider_instance_id"] == provider_instance_id)
    metadata = connection.get("metadata") or {}
    catalog_provider_id = metadata.get("catalog_provider_id")
    inventory = (catalog_snapshot.get("inventory") or {}).get(catalog_provider_id)
    inventory = inventory if isinstance(inventory, Mapping) else {}
    current = inventory.get("stale") is False and inventory.get("source") in {
        "openrouter_models_api", "openrouter_models_api_filtered",
    }
    models = []
    for item in catalog_snapshot.get("models") or []:
        if not isinstance(item, Mapping) or item.get("provider_id") != catalog_provider_id:
            continue
        model_id = item.get("provider_model_id")
        if not isinstance(model_id, str) or not model_id:
            continue
        models.append({"model_id": model_id,
                       "display_name": str(item.get("display_name") or model_id)})
    return {"profile_id": binding["profile_id"],
            "provider_instance_id": provider_instance_id,
            "models": models, "status": "live" if current else "unavailable"}
