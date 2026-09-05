"""Defaultspack-owned contributions for launch, web, and computer surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


_SURFACE_CONTRIBUTION_SCHEMA = "io.tobkiri.surface-contribution.v1"
_OWNER_PACK_ID = "defaultspack"
_DEFAULTSPACK_SURFACE_ENVIRONMENTS = {
    "browser": {
        "RUMI_DEFAULTSPACK_OPEN_BROWSER": "1",
        "RUMI_DEFAULTSPACK_SURFACE": "browser",
    },
    "desktop": {
        "RUMI_DEFAULTSPACK_OPEN_BROWSER": "1",
        "RUMI_DEFAULTSPACK_SURFACE": "webview",
    },
}


def defaultspack_surface_launch_contribution(
    target: Mapping[str, Any],
) -> dict[str, Any]:
    """Wrap one Defaultspack launch target in the generic surface contract."""
    pack_id = _text(target.get("pack_id"))
    if pack_id != _OWNER_PACK_ID:
        raise ValueError("Defaultspack can contribute only its own launch target.")

    launch_target = dict(target)
    launch_target["env"] = {
        key: value
        for key, value in _string_mapping(target.get("env")).items()
        if key not in _defaultspack_environment_keys()
    }
    launch_target["env_by_surface"] = _merged_surface_environments(target.get("env_by_surface"))
    return {
        "schema": _SURFACE_CONTRIBUTION_SCHEMA,
        "kind": "surface_launch_target",
        "owner_pack_id": _OWNER_PACK_ID,
        "target": launch_target,
    }


def register_defaultspack_surface_contribution(
    runtime_profile: dict[str, Any],
    target: Mapping[str, Any],
) -> dict[str, Any]:
    """Upsert Defaultspack's generic surface contribution into a profile."""
    contribution = defaultspack_surface_launch_contribution(target)
    contributions = runtime_profile.setdefault("surface_contributions", [])
    if not isinstance(contributions, list):
        raise ValueError("surface_contributions must be a list.")
    for index, existing in enumerate(contributions):
        if not isinstance(existing, Mapping):
            continue
        if existing.get("owner_pack_id") == _OWNER_PACK_ID:
            contributions[index] = contribution
            return contribution
    contributions.append(contribution)
    return contribution


def migrate_defaultspack_frontend_surface_contributions(
    runtime_profile: dict[str, Any],
) -> int:
    """Migrate legacy Defaultspack frontend records into generic contributions."""
    defaultspack = runtime_profile.get(_OWNER_PACK_ID)
    if not isinstance(defaultspack, Mapping):
        return 0
    frontends = defaultspack.get("frontends")
    if not isinstance(frontends, Mapping):
        return 0
    migrated = 0
    for frontend in frontends.values():
        if not isinstance(frontend, Mapping):
            continue
        target = frontend.get("surface_launch_target")
        if not isinstance(target, Mapping):
            continue
        if _text(target.get("pack_id")) != _OWNER_PACK_ID:
            continue
        register_defaultspack_surface_contribution(runtime_profile, target)
        migrated += 1
    return migrated


def defaultspack_web_mounts(pack_root: Path) -> tuple[dict[str, Any], ...]:
    """Return Defaultspack's authenticated UI mounts and legacy UI alias."""
    ui_root = Path(pack_root).resolve() / "ui"
    return (
        {
            "path_prefix": "/chat",
            "web_root": ui_root,
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
            "auth_bootstrap": True,
        },
        {
            "path_prefix": "/static",
            "web_root": ui_root,
            "spa_fallback": False,
            "index_file": "shell.html",
            "auth_required": True,
            "auth_bootstrap": False,
        },
        {
            "path_prefix": "/desktops",
            "web_root": ui_root,
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
            "auth_bootstrap": True,
        },
    )


def defaultspack_computer_artifact_destination(
    chat_store_path: Path,
) -> dict[str, str]:
    """Build the Pack-owned contract value for conversation computer artifacts."""
    conversation_root = Path(chat_store_path).expanduser().resolve().parent / "conversations"
    return {
        "schema": "io.tobkiri.computer-artifact-destination.v1",
        "kind": "conversation_workspace",
        "root": str(conversation_root),
    }


def _defaultspack_environment_keys() -> set[str]:
    return {
        key for environment in _DEFAULTSPACK_SURFACE_ENVIRONMENTS.values() for key in environment
    }


def _merged_surface_environments(value: Any) -> dict[str, dict[str, str]]:
    environments = {
        mode: dict(environment) for mode, environment in _DEFAULTSPACK_SURFACE_ENVIRONMENTS.items()
    }
    if not isinstance(value, Mapping):
        return environments
    for mode in environments:
        environments[mode].update(_string_mapping(value.get(mode)))
    return environments


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        clean_key = _text(key)
        clean_value = _text(item)
        if clean_key and clean_value:
            result[clean_key] = clean_value
    return result


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "defaultspack_computer_artifact_destination",
    "defaultspack_surface_launch_contribution",
    "defaultspack_web_mounts",
    "migrate_defaultspack_frontend_surface_contributions",
    "register_defaultspack_surface_contribution",
]
