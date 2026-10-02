"""Resolve active Capability Graph runtime profiles for flows and APIs."""

from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_runtime_profile_context(
    context: Dict[str, Any],
    *,
    interface_registry: Any = None,
    startup_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return context enriched with `_runtime_profile_key` and `_capability_profile`."""
    enriched = dict(context or {})
    if isinstance(enriched.get("capability_profile"), dict):
        enriched.setdefault("_capability_profile", enriched["capability_profile"])
        return enriched
    if isinstance(enriched.get("runtime_profile"), dict):
        enriched.setdefault("_capability_profile", enriched["runtime_profile"])
        enriched.setdefault("_runtime_profile_key", enriched["runtime_profile"].get("registry_key"))
        return enriched

    registry = interface_registry or enriched.get("interface_registry")
    runtime_profile_key = _runtime_profile_key_from_context(enriched, startup_profile=startup_profile)
    if runtime_profile_key:
        enriched.setdefault("_runtime_profile_key", runtime_profile_key)
    if runtime_profile_key and registry is not None:
        getter = getattr(registry, "get", None)
        if callable(getter):
            runtime_profile = getter(runtime_profile_key)
            if isinstance(runtime_profile, dict):
                enriched.setdefault("runtime_profile", runtime_profile)
                enriched.setdefault("_capability_profile", runtime_profile)
    return enriched


def _runtime_profile_key_from_context(
    context: Dict[str, Any],
    *,
    startup_profile: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    for key in ("runtime_profile_key", "_runtime_profile_key"):
        value = context.get(key)
        if isinstance(value, str) and value:
            return value

    active_metadata = _active_metadata(context)
    startup_graph = active_metadata.get("startup_capability_graph")
    if isinstance(startup_graph, dict):
        value = startup_graph.get("runtime_profile_key")
        if isinstance(value, str) and value:
            return value

    if isinstance(startup_profile, dict):
        value = startup_profile.get("last_runtime_profile_key")
        if isinstance(value, str) and value:
            return value
    return None


def _active_metadata(context: Dict[str, Any]) -> Dict[str, Any]:
    active = context.get("active_ecosystem")
    metadata = getattr(active, "metadata", None)
    if isinstance(metadata, dict):
        return metadata
    config = getattr(active, "_config", None)
    metadata = getattr(config, "metadata", None)
    if isinstance(metadata, dict):
        return metadata
    getter = getattr(active, "get_metadata", None)
    if callable(getter):
        try:
            value = getter("startup_capability_graph")
        except Exception:
            value = None
        if isinstance(value, dict):
            return {"startup_capability_graph": value}
    try:
        from backend_core.ecosystem.active_ecosystem import get_active_ecosystem_manager

        manager = get_active_ecosystem_manager()
        config = getattr(manager, "_config", None)
        metadata = getattr(config, "metadata", None)
        if isinstance(metadata, dict):
            return metadata
    except Exception:
        pass
    return {}
