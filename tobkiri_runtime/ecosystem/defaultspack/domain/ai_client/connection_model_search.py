"""Search public catalog candidates tied to an explicit saved connection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def search_connection_catalog(
    filters: Mapping[str, Any],
    profiles: list[Mapping[str, Any]],
    catalog_models: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return unverified candidates without credentials or ambient discovery.

    The captured host resolves the connection and provider before calling this
    projection. Binding is an intended selection destination, not evidence that
    the account can access the public catalog model.
    """
    from domain.ai_client.model_search import (
        get_profile_catalog,
        search_models,
        _normalize_registry_profiles,
    )

    connection_id = filters.get("connection_id")
    provider_id = filters.get("provider_id")
    if not isinstance(connection_id, str) or not connection_id:
        raise PermissionError("model search connection is invalid")
    if not isinstance(provider_id, str) or not provider_id:
        raise PermissionError("model search connection provider is unavailable")
    public_models = [
        dict(model) for model in catalog_models
        if isinstance(model, Mapping) and model.get("provider_id") == provider_id
    ]
    # An explicit snapshot avoids the ambient discovery/cache compatibility path.
    public_profiles = get_profile_catalog(
        registry_profiles=[], catalog_models=public_models, settings=None,
    )
    merged = {str(item.get("profile_id")): item for item in public_profiles}
    bound = []
    for raw in profiles:
        if not isinstance(raw, Mapping) or raw.get("enabled") is False:
            continue
        metadata = raw.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        if metadata.get("provider_connection_id") != connection_id:
            continue
        record = dict(raw)
        # Transport identity comes from the connection, not a maker prefix.
        record["provider_id"] = provider_id
        record["provider"] = provider_id
        record["metadata"] = {"provider_connection_id": connection_id}
        bound.append(record)
    saved_profile_ids: set[str] = set()
    for item in _normalize_registry_profiles(bound):
        identifier = str(item.get("profile_id"))
        saved_profile_ids.add(identifier)
        merged[identifier] = item
    result = search_models(dict(filters), profiles=list(merged.values()), settings={})
    for item in result["models"]:
        # Never expose unrelated metadata or credential material.
        item["metadata"] = {"provider_connection_id": connection_id}
        item["connection_id"] = connection_id
        item["provenance"] = "provider_public_catalog"
        item["reachability"] = "unverified"
        item["availability"] = {"status": "unverified"}
        # A saved route exists independently of account entitlement or access.
        item["route_configured"] = item["profile_id"] in saved_profile_ids
        item.pop("configured", None)
        item.pop("requires_api_key", None)
        item["notes"] = ""
    result["filters_applied"]["connection_id"] = connection_id
    return result
