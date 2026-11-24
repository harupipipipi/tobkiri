from __future__ import annotations

from typing import Any

from domain.tool.schema_adapter import mapping_or_empty, tool_name_from_definition
from domain.tool.security import is_sandbox_capability_tool, is_trusted_pack_id, untrusted_tool_security_rejection

from .risk import resolve_tool_risk


TOOL_PERMISSION_MODES = {"deny", "ask", "allow", "dry_run", "inherit"}
DEFAULT_TOOL_PERMISSION_POLICY: dict[str, Any] = {
    "default_mode": "ask",
    "risk_defaults": {
        "low": "allow",
        "medium": "ask",
        "high": "ask",
    },
    "unknown_tool_mode": "ask",
    "untrusted_tool_mode": "deny",
    "missing_capability_mode": "deny",
    "audit": True,
    "tools": {},
}

_HIGH_RISKS = {
    "file_write",
    "file_delete",
    "shell",
    "computer",
    "credential",
    "git_write",
    "git_push",
    "external_message",
    "scheduler_create",
    "capability_mutation",
    "pack_install",
}
_MEDIUM_RISKS = {"network", "browser"}


def profile_tool_permission_policy(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(policy, dict):
        return None
    raw = policy.get("tool_permission_policy")
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        return None
    return normalize_tool_permission_policy(raw)


def normalize_tool_permission_policy(raw: dict[str, Any] | None) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    normalized: dict[str, Any] = {
        "default_mode": _mode(source.get("default_mode"), DEFAULT_TOOL_PERMISSION_POLICY["default_mode"]),
        "risk_defaults": dict(DEFAULT_TOOL_PERMISSION_POLICY["risk_defaults"]),
        "unknown_tool_mode": _mode(
            source.get("unknown_tool_mode"),
            DEFAULT_TOOL_PERMISSION_POLICY["unknown_tool_mode"],
        ),
        "untrusted_tool_mode": _mode(
            source.get("untrusted_tool_mode"),
            DEFAULT_TOOL_PERMISSION_POLICY["untrusted_tool_mode"],
        ),
        "missing_capability_mode": _mode(
            source.get("missing_capability_mode"),
            DEFAULT_TOOL_PERMISSION_POLICY["missing_capability_mode"],
        ),
        "audit": source.get("audit", DEFAULT_TOOL_PERMISSION_POLICY["audit"]) is not False,
        "tools": {},
    }
    risk_defaults = source.get("risk_defaults")
    if isinstance(risk_defaults, dict):
        for level in ("low", "medium", "high"):
            if level in risk_defaults:
                normalized["risk_defaults"][level] = _mode(
                    risk_defaults.get(level),
                    normalized["risk_defaults"][level],
                )
    tools = source.get("tools")
    if isinstance(tools, dict):
        normalized["tools"] = {
            str(name).strip(): _normalize_tool_rule(rule)
            for name, rule in tools.items()
            if str(name).strip()
        }
    return normalized


def resolve_profile_tool_permission(
    tool_def: Any,
    tool_name: str,
    arguments: dict[str, Any] | None,
    policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    permission_policy = profile_tool_permission_policy(policy)
    if permission_policy is None:
        return None

    name = str(tool_name or tool_name_from_definition(tool_def) or "").strip()
    if not name:
        mode = permission_policy["unknown_tool_mode"]
        return _decision(
            mode,
            tool_name="",
            action="default",
            risk="read_only",
            risk_level="low",
            matched_by="unknown_tool_mode",
            matched_value=mode,
            reason="unknown tool",
            audit_required=permission_policy["audit"],
        )

    risk = resolve_tool_risk(tool_def, name)
    risk_level = tool_risk_level(tool_def, risk)
    action = infer_tool_action(name, arguments)
    reason = ""
    matched_by = ""
    matched_value = ""

    special_mode = _special_mode(tool_def, permission_policy)
    if special_mode:
        mode, matched_by, reason = special_mode
        matched_value = mode
    else:
        tool_rule = _tool_rule(permission_policy, name, tool_def)
        mode, matched_by, matched_value = _mode_for_tool_action(
            permission_policy,
            tool_rule,
            name,
            action,
            risk_level,
        )

    return _decision(
        mode,
        tool_name=name,
        action=action,
        risk=risk,
        risk_level=risk_level,
        matched_by=matched_by,
        matched_value=matched_value,
        reason=reason or _reason_for_mode(mode, matched_by),
        audit_required=permission_policy["audit"],
    )


def tool_risk_level(tool_def: Any, risk: str) -> str:
    if isinstance(tool_def, dict):
        explicit = str(tool_def.get("risk_level") or "").strip().lower()
        metadata = tool_def.get("metadata")
        if not explicit and isinstance(metadata, dict):
            explicit = str(metadata.get("risk_level") or "").strip().lower()
        if explicit in {"low", "medium", "high"}:
            return explicit
        if tool_def.get("requires_approval") is True:
            return "high"
    if risk in _HIGH_RISKS:
        return "high"
    if risk in _MEDIUM_RISKS:
        return "medium"
    return "low"


def infer_tool_action(tool_name: str, arguments: dict[str, Any] | None) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    raw = str(args.get("action") or args.get("operation") or args.get("method") or "").strip()
    if not raw:
        return "default"
    lowered = raw.lower().replace("_", "-")
    if tool_name in {"browser_use", "computer_use", "browser_computer"}:
        aliases = {
            "": "session",
            "session": "browser.session",
            "open": "browser.open_url",
            "open-url": "browser.open_url",
            "open_url": "browser.open_url",
            "browser-open-url": "browser.open_url",
            "browser_open_url": "browser.open_url",
            "click": "computer.click",
            "screenshot": "computer.screenshot",
            "state": "computer.context",
            "context": "computer.context",
            "type": "computer.type",
            "key": "computer.key",
            "move": "computer.move",
            "drag": "computer.drag",
            "scroll": "computer.scroll",
        }
        return aliases.get(lowered, raw)
    return raw


def action_key_candidates(tool_name: str, action: str) -> list[str]:
    action = str(action or "default").strip() or "default"
    candidates = [action, action.lower(), f"{tool_name}.{action}", f"{tool_name}:{action}"]
    if "." in action:
        suffix = action.rsplit(".", 1)[-1]
        candidates.extend([suffix, suffix.lower(), f"{tool_name}.{suffix}", f"{tool_name}:{suffix}"])
    return _dedupe(candidates)


def _mode_for_tool_action(
    policy: dict[str, Any],
    tool_rule: dict[str, Any] | None,
    tool_name: str,
    action: str,
    risk_level: str,
) -> tuple[str, str, str]:
    if tool_rule is not None:
        actions = mapping_or_empty(tool_rule.get("actions"))
        for candidate in action_key_candidates(tool_name, action):
            if candidate in actions:
                mode = _mode_from_rule(actions[candidate], "inherit")
                if mode != "inherit":
                    return mode, "tool_action", candidate
        mode = _mode(tool_rule.get("mode"), "inherit")
        if mode != "inherit":
            return mode, "tool", tool_name

    risk_defaults = mapping_or_empty(policy.get("risk_defaults"))
    risk_mode = _mode(risk_defaults.get(risk_level), "inherit")
    if risk_mode != "inherit":
        return risk_mode, "risk_defaults", risk_level
    return _mode(policy.get("default_mode"), DEFAULT_TOOL_PERMISSION_POLICY["default_mode"]), "default_mode", ""


def _tool_rule(policy: dict[str, Any], tool_name: str, tool_def: Any) -> dict[str, Any] | None:
    tools = policy.get("tools")
    if not isinstance(tools, dict):
        return None
    keys = [tool_name]
    if isinstance(tool_def, dict):
        for key in ("tool_id", "name"):
            value = str(tool_def.get(key) or "").strip()
            if value:
                keys.append(value)
    for key in _dedupe(keys):
        rule = tools.get(key)
        if isinstance(rule, dict):
            return rule
    return None


def _normalize_tool_rule(rule: Any) -> dict[str, Any]:
    if isinstance(rule, str):
        return {"mode": _mode(rule, "inherit"), "actions": {}}
    if not isinstance(rule, dict):
        return {"mode": "inherit", "actions": {}}
    actions = rule.get("actions")
    if not isinstance(actions, dict):
        actions = rule.get("action_modes")
    normalized_actions = {}
    if isinstance(actions, dict):
        normalized_actions = {
            str(name).strip(): _mode_from_rule(value, "inherit")
            for name, value in actions.items()
            if str(name).strip()
        }
    return {
        "mode": _mode(rule.get("mode"), "inherit"),
        "actions": normalized_actions,
    }


def _mode_from_rule(rule: Any, default: str) -> str:
    if isinstance(rule, dict):
        return _mode(rule.get("mode"), default)
    return _mode(rule, default)


def _mode(value: Any, default: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in TOOL_PERMISSION_MODES:
        return normalized
    return default if default in TOOL_PERMISSION_MODES else "ask"


def _special_mode(tool_def: Any, policy: dict[str, Any]) -> tuple[str, str, str] | None:
    if not isinstance(tool_def, dict):
        mode = _mode(policy.get("unknown_tool_mode"), "ask")
        return mode, "unknown_tool_mode", "unknown tool"
    security_rejection = untrusted_tool_security_rejection(tool_def)
    if security_rejection is not None:
        return "deny", "tool_security", security_rejection
    if is_sandbox_capability_tool(tool_def):
        return "allow", "sandbox_capability", "sandbox capability"
    if _is_explicitly_untrusted_tool(tool_def):
        mode = _mode(policy.get("untrusted_tool_mode"), "deny")
        if mode != "inherit":
            return mode, "untrusted_tool_mode", "untrusted tool source"
    if _has_missing_runtime_capability(tool_def):
        mode = _mode(policy.get("missing_capability_mode"), "deny")
        if mode != "inherit":
            return mode, "missing_capability_mode", "missing runtime capability"
    return None


def _is_explicitly_untrusted_tool(tool_def: dict[str, Any]) -> bool:
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("trusted") is False:
            return True
        source_pack_id = str(metadata.get("source_pack_id") or "").strip()
        if source_pack_id:
            return not is_trusted_pack_id(source_pack_id)
    source_pack_id = str(tool_def.get("source_pack_id") or "").strip()
    if source_pack_id:
        return not is_trusted_pack_id(source_pack_id)
    return tool_def.get("trusted") is False


def _has_missing_runtime_capability(tool_def: dict[str, Any]) -> bool:
    if tool_def.get("missing_capabilities") or tool_def.get("missing_runtime_capabilities"):
        return True
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict) and (
        metadata.get("missing_capabilities") or metadata.get("missing_runtime_capabilities")
    ):
        return True
    return False


def _decision(
    mode: str,
    *,
    tool_name: str,
    action: str,
    risk: str,
    risk_level: str,
    matched_by: str,
    matched_value: str,
    reason: str,
    audit_required: bool,
) -> dict[str, Any]:
    status = {
        "deny": "denied",
        "ask": "approval_required",
        "allow": "allowed",
        "dry_run": "dry_run",
        "inherit": "approval_required",
    }.get(mode, "approval_required")
    return {
        "allowed": status == "allowed",
        "requires_approval": status == "approval_required",
        "status": status,
        "mode": mode,
        "action": action,
        "risk": risk,
        "risk_level": risk_level,
        "tool_name": tool_name,
        "matched_by": matched_by,
        "matched_value": str(matched_value or ""),
        "reason": reason,
        "audit_required": bool(audit_required),
    }


def _reason_for_mode(mode: str, matched_by: str) -> str:
    if mode == "deny":
        return f"denied by {matched_by}"
    if mode == "ask":
        return f"approval required by {matched_by}"
    if mode == "allow":
        return f"allowed by {matched_by}"
    if mode == "dry_run":
        return f"dry-run by {matched_by}"
    return f"permission inherited from {matched_by}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result
