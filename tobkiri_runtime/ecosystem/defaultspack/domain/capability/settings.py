"""Canonical capability settings and backward-compatible normalization."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_CAPABILITY_SETTINGS: dict[str, Any] = {
    "schema_version": "tobkiri.capability-settings/v2",
    "capabilities": {
        "enabled": True,
        "selection": {"default_mode": "auto"},
        "approval": {
            "actions": {
                "read": "auto",
                "search": "auto",
                "create": "confirm",
                "update": "confirm",
                "send": "confirm",
                "execute": "confirm",
                "computer": "confirm",
                "delete": "confirm",
                "credential": "confirm",
            }
        },
        "activities": {"overrides": {}},
        "services": {"overrides": {}},
        "tools": {"overrides": {}},
        "ui": {
            "pinned_items": [
                "widget:capability-master",
                "activity:browser",
            ],
            "placements": {},
        },
        "advanced": {
            "selector": "hybrid",
            "max_candidate_tools": 20,
            "max_attached_tools": 8,
            "max_tool_schema_tokens": 8000,
            "max_skill_tokens": 3000,
            "max_attached_skills": 3,
            "trace_level": "summary",
        },
    },
}


def normalize_capability_settings(value: Any) -> dict[str, Any]:
    """Merge persisted settings into safe product defaults."""

    normalized = deepcopy(DEFAULT_CAPABILITY_SETTINGS)
    if not isinstance(value, dict):
        return normalized
    source = (
        value.get("capabilities")
        if isinstance(value.get("capabilities"), dict)
        else value
    )
    capabilities = normalized["capabilities"]
    _merge_dict(capabilities, source)
    capabilities["enabled"] = bool(capabilities.get("enabled", True))
    advanced = capabilities["advanced"]
    advanced["max_candidate_tools"] = _bounded_int(
        advanced.get("max_candidate_tools"), 20, 1, 20
    )
    advanced["max_attached_tools"] = _bounded_int(
        advanced.get("max_attached_tools"), 8, 1, 8
    )
    advanced["max_attached_skills"] = _bounded_int(
        advanced.get("max_attached_skills"), 3, 1, 3
    )
    advanced["max_tool_schema_tokens"] = _bounded_int(
        advanced.get("max_tool_schema_tokens"), 8000, 256, 64000
    )
    advanced["max_skill_tokens"] = _bounded_int(
        advanced.get("max_skill_tokens"), 3000, 128, 16000
    )
    return normalized


def _merge_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key not in target:
            continue
        if isinstance(target[key], dict) and isinstance(value, dict):
            _merge_dict(target[key], value)
        else:
            target[key] = deepcopy(value)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
