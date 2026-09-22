"""Resolve user-facing startup surface launch targets from runtime profiles."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

_SURFACE_ENV_KEYS = {
    "RUMI_PROFILE_SURFACE",
}
_SURFACE_CONTRIBUTION_SCHEMA = "io.tobkiri.surface-contribution.v1"


def extract_surface_launch_target(
    runtime_profile: Optional[Dict[str, Any]],
    *,
    fallback_pack_id: Optional[str],
    surfaces: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the canonical surface launch target for a compiled runtime profile."""
    if not isinstance(runtime_profile, dict):
        return normalize_surface_launch_target(
            None,
            fallback_pack_id=fallback_pack_id,
            surfaces=surfaces,
        )

    explicit = normalize_surface_launch_target(
        _nested_dict(runtime_profile, "launch", "surface"),
        fallback_pack_id=None,
        surfaces=surfaces,
    )
    if explicit:
        return explicit

    target = _from_surface_contributions(runtime_profile, surfaces=surfaces)
    if target:
        return target

    target = _from_launch_metadata_nodes(runtime_profile, surfaces=surfaces)
    if target:
        return target

    return normalize_surface_launch_target(
        None,
        fallback_pack_id=fallback_pack_id,
        surfaces=surfaces,
    )


def normalize_surface_launch_target(
    target: Any,
    *,
    fallback_pack_id: Optional[str],
    surfaces: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize a stored launch target, falling back to a pack launch when needed."""
    if isinstance(target, Mapping):
        normalized = _normalize_target_mapping(target, surfaces=surfaces)
        if normalized:
            return normalized

    pack_id = _clean_string(fallback_pack_id)
    if not pack_id:
        return None
    mode = resolve_surface_mode(surfaces)
    return {
        "kind": "desktop_app",
        "pack_id": pack_id,
        "principal_id": pack_id,
        "surface": mode,
        "env": surface_env(mode),
        "source": "startup_profile_fallback",
    }


def surface_launch_target_from_instance(
    *,
    runtime_profile: Dict[str, Any],
    instance: Any,
    nodes: Dict[str, Any],
    profile: Any = None,
    surfaces: Optional[Dict[str, Any]] = None,
    diagnostics: Optional[list[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Build a launch target for the graph node instance that provides a surface."""
    del profile
    node_id = _clean_string(getattr(instance, "ref", None))
    node_instance_id = _clean_string(getattr(instance, "id", None))
    node = nodes.get(node_id) if isinstance(nodes, dict) else None
    node_payload = _node_to_mapping(node)
    if not node_payload:
        node_payload = _runtime_node_payload(runtime_profile, node_instance_id).get("node") or {}
    return _target_from_node_payload(
        node_payload,
        node_instance_id=node_instance_id,
        node_id=node_id,
        surfaces=surfaces,
        diagnostics=diagnostics,
    )


def resolve_surface_mode(surfaces: Any) -> str:
    if not isinstance(surfaces, dict):
        return "browser"
    preferred = str(surfaces.get("preferred") or "").strip().lower()
    enabled = {
        str(surface).strip().lower()
        for surface in surfaces.get("enabled", [])
        if isinstance(surface, str)
    }
    if preferred in {"desktop", "webview", "native"}:
        return "desktop"
    if preferred in {"browser", "web"}:
        return "browser"
    if "desktop" in enabled and "browser" not in enabled and "web" not in enabled:
        return "desktop"
    return "browser"


def surface_env(mode: str) -> Dict[str, str]:
    """Return core-owned surface selection environment for a launch target."""
    normalized = "desktop" if str(mode).strip().lower() == "desktop" else "browser"
    return {"RUMI_PROFILE_SURFACE": normalized}


def _from_surface_contributions(
    runtime_profile: Dict[str, Any],
    *,
    surfaces: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Resolve a launch target supplied by a validated Pack contribution."""
    contributions = runtime_profile.get("surface_contributions")
    if not isinstance(contributions, (list, tuple)):
        return None
    for contribution in contributions:
        if not isinstance(contribution, Mapping):
            continue
        if contribution.get("schema") != _SURFACE_CONTRIBUTION_SCHEMA:
            continue
        if contribution.get("kind") != "surface_launch_target":
            continue
        owner_pack_id = _clean_string(contribution.get("owner_pack_id"))
        target = normalize_surface_launch_target(
            contribution.get("target"),
            fallback_pack_id=None,
            surfaces=surfaces,
        )
        if target and owner_pack_id == target["pack_id"]:
            return target
    return None


def _from_launch_metadata_nodes(
    runtime_profile: Dict[str, Any],
    *,
    surfaces: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    runtime_nodes = runtime_profile.get("nodes")
    if not isinstance(runtime_nodes, dict):
        return None
    for node_instance_id in sorted(runtime_nodes):
        payload = runtime_nodes.get(node_instance_id)
        if not isinstance(payload, dict):
            continue
        node = payload.get("node")
        if not isinstance(node, dict):
            continue
        launch = node.get("metadata", {}).get("launch") if isinstance(node.get("metadata"), dict) else None
        if not isinstance(launch, dict) or launch.get("default") is not True:
            continue
        target = _target_from_node_payload(
            node,
            node_instance_id=node_instance_id,
            node_id=_clean_string(payload.get("node_id") or node.get("node_id")),
            surfaces=surfaces,
            diagnostics=None,
        )
        if target:
            return target
    return None


def _target_from_node_payload(
    node_payload: Mapping[str, Any],
    *,
    node_instance_id: str,
    node_id: str,
    surfaces: Optional[Dict[str, Any]],
    diagnostics: Optional[list[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    metadata = node_payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    launch = metadata.get("launch")
    if launch is None:
        launch = {}
    if not isinstance(launch, Mapping):
        return None
    kind = _clean_string(launch.get("kind") or "desktop_app")
    if kind != "desktop_app":
        return None

    node_pack_id = _clean_string(metadata.get("pack_id")) or _node_pack_id(node_id)
    target_pack_id = _clean_string(launch.get("pack_id")) or node_pack_id
    if not node_pack_id or not target_pack_id:
        return None
    if target_pack_id != node_pack_id:
        _diagnose(
            diagnostics,
            "error",
            "launch_pack_mismatch",
            "Surface node launch target pack does not match the node pack",
            node_id=node_id,
            node_instance_id=node_instance_id,
            node_pack_id=node_pack_id,
            launch_pack_id=target_pack_id,
        )
        return None

    principal_id = _clean_string(launch.get("principal_id")) or target_pack_id
    if principal_id != target_pack_id:
        _diagnose(
            diagnostics,
            "error",
            "launch_principal_mismatch",
            "Surface node launch principal must match the target pack",
            node_id=node_id,
            node_instance_id=node_instance_id,
            principal_id=principal_id,
            launch_pack_id=target_pack_id,
        )
        return None

    explicit_surface = _clean_string(launch.get("surface"))
    mode = explicit_surface or resolve_surface_mode(surfaces)
    env = surface_env(mode)
    env.update(_string_dict(launch.get("env")))
    target: Dict[str, Any] = {
        "kind": "desktop_app",
        "pack_id": target_pack_id,
        "principal_id": principal_id,
        "surface": mode,
        "node_instance_id": node_instance_id,
        "node_id": node_id,
        "env": env,
        "source": "capability_graph",
        "surface_source": "metadata" if explicit_surface else "profile",
    }
    component_full_id = _component_full_id(metadata, target_pack_id)
    if component_full_id:
        target["component_full_id"] = component_full_id
    return target


def _normalize_target_mapping(
    target: Mapping[str, Any],
    *,
    surfaces: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    kind = _clean_string(target.get("kind") or "desktop_app")
    if kind != "desktop_app":
        return None
    pack_id = _clean_string(target.get("pack_id"))
    if not pack_id:
        return None
    principal_id = _clean_string(target.get("principal_id")) or pack_id
    if principal_id != pack_id:
        return None
    surface_source = _clean_string(target.get("surface_source"))
    if surface_source == "profile":
        mode = resolve_surface_mode(surfaces)
    else:
        mode = _clean_string(target.get("surface")) or resolve_surface_mode(surfaces)
    env = surface_env(mode)
    env.update(_surface_environment(target.get("env_by_surface"), mode))
    target_env = _string_dict(target.get("env"))
    if surface_source == "profile":
        target_env = {
            key: value
            for key, value in target_env.items()
            if key not in _SURFACE_ENV_KEYS
        }
    env.update(target_env)
    normalized: Dict[str, Any] = {
        "kind": "desktop_app",
        "pack_id": pack_id,
        "principal_id": principal_id,
        "surface": mode,
        "env": env,
        "source": _clean_string(target.get("source")) or "capability_graph",
    }
    for key in ("node_instance_id", "node_id", "component_full_id", "surface_source"):
        value = _clean_string(target.get(key))
        if value:
            normalized[key] = value
    return normalized


def _surface_environment(value: Any, mode: str) -> Dict[str, str]:
    """Return Pack-supplied, mode-specific environment from a generic contract."""
    if not isinstance(value, Mapping):
        return {}
    normalized_mode = "desktop" if mode == "desktop" else "browser"
    environment = _string_dict(value.get(normalized_mode))
    return {
        key: item
        for key, item in environment.items()
        if key not in _SURFACE_ENV_KEYS
    }


def _runtime_node_payload(runtime_profile: Dict[str, Any], node_instance_id: str) -> Dict[str, Any]:
    runtime_nodes = runtime_profile.get("nodes")
    if not isinstance(runtime_nodes, dict):
        return {}
    payload = runtime_nodes.get(node_instance_id)
    return dict(payload) if isinstance(payload, dict) else {}


def _node_to_mapping(node: Any) -> Dict[str, Any]:
    if isinstance(node, Mapping):
        return dict(node)
    to_dict = getattr(node, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _nested_dict(data: Dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _node_pack_id(node_id: str) -> str:
    return node_id.split(".", 1)[0] if "." in node_id else ""


def _component_full_id(metadata: Mapping[str, Any], pack_id: str) -> str:
    explicit = _clean_string(metadata.get("component_full_id"))
    if explicit:
        return explicit
    component_type = _clean_string(metadata.get("component_type") or metadata.get("component"))
    component_id = _clean_string(metadata.get("component_id"))
    if component_type and component_id:
        return f"{pack_id}:{component_type}:{component_id}"
    return ""


def _string_dict(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and key:
            result[key] = str(item)
    return result


def _clean_string(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) and value.strip() else ""


def _diagnose(
    diagnostics: Optional[list[Dict[str, Any]]],
    level: str,
    code: str,
    message: str,
    **meta: Any,
) -> None:
    if diagnostics is None:
        return
    diagnostics.append(
        {
            "level": level,
            "code": code,
            "message": message,
            **meta,
        }
    )
