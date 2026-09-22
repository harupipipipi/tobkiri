from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from domain.frontend_settings import frontend_settings_path
from domain.tool.service_catalog import (
    infer_action_class,
    infer_service_id,
    minimum_requires_confirm,
    more_restrictive_permission,
)
from domain.tool.schema_adapter import mapping_or_empty, tool_name_from_definition


PERMISSION_MODES = {"auto", "confirm", "block"}
WRITE_APPROVAL_ACTION_CLASSES = {"create", "update", "send", "execute", "computer", "delete"}
HIGH_RISK_LEVELS = {"high", "critical"}
DEFAULT_ACTION_PERMISSIONS: dict[str, str] = {
    "read": "auto",
    "search": "auto",
    "create": "confirm",
    "update": "confirm",
    "send": "confirm",
    "execute": "confirm",
    "computer": "confirm",
    "delete": "confirm",
}


class ToolPermissionResolver:
    def __init__(self, settings: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> None:
        self._pack_root = pack_root or Path(__file__).resolve().parents[2]
        self._settings = settings if isinstance(settings, dict) else read_frontend_settings(self._pack_root)
        self._tool_settings = mapping_or_empty(self._settings.get("tools"))

    def resolve(self, tool: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context if isinstance(context, dict) else {}
        tool_id = _tool_id(tool)
        service_id = infer_service_id(tool)
        action_class = infer_action_class(tool)
        steps: list[dict[str, str]] = []

        minimum = "confirm" if minimum_requires_confirm(tool) else "auto"
        steps.append({"source": "built_in_minimum", "value": minimum})
        hard_minimum = _hard_minimum_permission(tool, action_class)

        action_default = self._action_permission(action_class)
        steps.append({"source": f"action:{action_class}", "value": action_default})
        effective = _apply_hard_minimum(action_default, hard_minimum)

        service_override = _override_value(self._tool_settings.get("service_permission_overrides"), service_id, action_class)
        if service_override:
            steps.append({"source": f"service:{service_id}", "value": service_override})
            effective = _apply_hard_minimum(service_override, hard_minimum)

        tool_override = _override_value(self._tool_settings.get("tool_permission_overrides"), tool_id, action_class)
        legacy_disabled = tool_id in _string_set(self._tool_settings.get("disabled_tool_ids"))
        if legacy_disabled and not tool_override:
            tool_override = "block"
        if tool_override:
            steps.append({"source": f"tool:{tool_id}", "value": tool_override})
            effective = _apply_hard_minimum(tool_override, hard_minimum)

        profile_mode = _profile_policy_mode(tool, tool_id, service_id, action_class, context.get("profile_policy"))
        if profile_mode:
            steps.append({"source": "profile_policy", "value": profile_mode})
            effective = more_restrictive_permission(effective, profile_mode)

        runtime_mode = _runtime_policy_mode(tool_id, service_id, context)
        if runtime_mode:
            steps.append({"source": "runtime_authority_policy", "value": runtime_mode})
            effective = more_restrictive_permission(effective, runtime_mode)

        return {
            "tool_id": tool_id,
            "service_id": service_id,
            "action_class": action_class,
            "permission": effective,
            "minimum_permission": minimum,
            "sources": steps,
        }

    def filter_blocked(self, tools: list[dict[str, Any]], *, context: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        allowed: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        for tool in tools:
            resolution = self.resolve(tool, context=context)
            entries.append(resolution)
            if resolution["permission"] != "block":
                allowed.append(tool)
        return allowed, entries

    def _action_permission(self, action_class: str) -> str:
        overrides = self._tool_settings.get("standard_permissions")
        if not isinstance(overrides, dict):
            overrides = self._tool_settings.get("action_permissions")
        if not isinstance(overrides, dict):
            overrides = {}
        value = str(overrides.get(action_class) or DEFAULT_ACTION_PERMISSIONS.get(action_class, "confirm")).strip().lower()
        if action_class == "delete" and value == "auto":
            value = "confirm"
        return value if value in PERMISSION_MODES else DEFAULT_ACTION_PERMISSIONS.get(action_class, "confirm")


def _read_frontend_settings(pack_root: Path) -> dict[str, Any]:
    path = frontend_settings_path(pack_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_frontend_settings(pack_root: Path | None = None) -> dict[str, Any]:
    return _read_frontend_settings(pack_root or Path(__file__).resolve().parents[2])


def _override_value(container: Any, target_id: str, action_class: str) -> str:
    if not isinstance(container, dict):
        return ""
    raw = container.get(target_id)
    if isinstance(raw, str):
        value = raw.strip().lower()
        return value if value in PERMISSION_MODES else ""
    if isinstance(raw, dict):
        value = str(raw.get(action_class) or raw.get("*") or raw.get("default") or "").strip().lower()
        return value if value in PERMISSION_MODES else ""
    return ""


def _hard_minimum_permission(tool: dict[str, Any], action_class: str) -> str:
    metadata = mapping_or_empty(tool.get("metadata"))
    risk = str(tool.get("risk") or metadata.get("risk") or "").strip().lower()
    if bool(tool.get("requires_approval") or metadata.get("requires_approval")):
        return "confirm"
    if risk in HIGH_RISK_LEVELS:
        return "confirm"
    if action_class == "delete":
        return "confirm"
    return "auto"


def _apply_hard_minimum(permission: str, hard_minimum: str) -> str:
    value = permission if permission in PERMISSION_MODES else "confirm"
    if hard_minimum == "confirm":
        return more_restrictive_permission(value, "confirm")
    return value


def _profile_policy_mode(tool: dict[str, Any], tool_id: str, service_id: str, action_class: str, policy: Any) -> str:
    if not isinstance(policy, dict):
        return ""
    deny = _string_set(policy.get("tool_denylist") or policy.get("disabled_tools") or policy.get("blocked_tools"))
    if tool_id in deny or service_id in deny:
        return "block"
    allow = _string_set(policy.get("tool_allowlist"))
    if allow and tool_id not in allow and service_id not in allow:
        return "block"
    if policy.get("write_actions_require_approval") is True and _write_approval_policy_applies(tool, action_class):
        return "confirm"
    if policy.get("high_risk_tools_require_approval") is True and _high_risk_policy_applies(tool):
        return "confirm"
    return ""


def _runtime_policy_mode(tool_id: str, service_id: str, context: dict[str, Any]) -> str:
    policy = context.get("runtime_tool_permission_overrides")
    value = _override_value(policy, tool_id, "*") or _override_value(policy, service_id, "*")
    if value:
        return value
    blocked = _string_set(context.get("blocked_tools") or context.get("disabled_tools"))
    if tool_id in blocked or service_id in blocked:
        return "block"
    return ""


def _tool_id(tool: dict[str, Any]) -> str:
    return str(tool.get("tool_id") or tool_name_from_definition(tool) or tool.get("name") or "").strip()


def _write_approval_policy_applies(tool: dict[str, Any], action_class: str) -> bool:
    metadata = mapping_or_empty(tool.get("metadata"))
    if action_class in WRITE_APPROVAL_ACTION_CLASSES:
        return True
    if bool(tool.get("write_action") or metadata.get("write_action")):
        return True
    action_type = str(tool.get("action_type") or metadata.get("action_type") or "").strip().lower()
    return action_type in {"write", "file_write", "delete", "create", "update", "patch", "commit", "push"}


def _high_risk_policy_applies(tool: dict[str, Any]) -> bool:
    metadata = mapping_or_empty(tool.get("metadata"))
    execution = mapping_or_empty(tool.get("execution"))
    for container in (tool, metadata, execution):
        for key in ("risk", "risk_level"):
            if str(container.get(key) or "").strip().lower() in HIGH_RISK_LEVELS:
                return True
    return False


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        values = []
    return {item for item in values if item}
