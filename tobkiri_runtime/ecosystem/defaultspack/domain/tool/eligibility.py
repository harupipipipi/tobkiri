from __future__ import annotations

from typing import Any

from domain.capabilities.runtime_snapshot import RuntimeCapabilitySnapshot
from domain.tool.schema_adapter import (
    is_tool_rejected_by_policy,
    mapping_or_empty,
    tool_name_from_definition,
    tool_requires_approval_by_policy,
)


REJECTION_CODE_MAP = {
    "missing_capability": "MISSING_CAPABILITY",
    "missing_input": "MISSING_INPUT",
    "model_unsupported": "MODEL_UNSUPPORTED",
    "disabled_by_user": "DISABLED_BY_USER",
    "disabled_by_policy": "DISABLED_BY_POLICY",
    "requires_approval": "REQUIRES_APPROVAL",
    "not_connected_to_profile": "NOT_CONNECTED",
    "requires_trusted_workspace": "REQUIRES_TRUSTED_WORKSPACE",
    "missing_api_key": "MISSING_API_KEY",
    "attachment_not_supported": "ATTACHMENT_NOT_SUPPORTED",
    "risk_blocked": "RISK_BLOCKED",
}


def filter_tool_definitions_by_eligibility(
    tools: list[dict[str, Any]],
    snapshot: RuntimeCapabilitySnapshot | dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    connected_tool_names: set[str] | None = None,
) -> dict[str, Any]:
    normalized_snapshot = _coerce_snapshot(snapshot)
    allowed_tools: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for tool in tools:
        entry = evaluate_tool_eligibility(
            tool,
            normalized_snapshot,
            policy=policy,
            connected_tool_names=connected_tool_names,
        )
        entries.append(entry)
        if entry["status"] in {"allowed", "approval_required"}:
            allowed_tools.append(tool)
    return {"allowed_tools": allowed_tools, "entries": entries, "runtime_capability_snapshot": normalized_snapshot.as_dict()}


def evaluate_tool_eligibility(
    tool: dict[str, Any],
    snapshot: RuntimeCapabilitySnapshot | dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    connected_tool_names: set[str] | None = None,
) -> dict[str, Any]:
    tool = tool if isinstance(tool, dict) else {"tool_id": tool_name_from_definition(tool)}
    snapshot_obj = _coerce_snapshot(snapshot)
    policy = policy if isinstance(policy, dict) else {}
    tool_name = tool_name_from_definition(tool)
    required = _tool_requirements(tool)
    actual = snapshot_obj.as_dict()

    metadata = mapping_or_empty(tool.get("metadata"))
    if tool.get("enabled") is False or metadata.get("enabled") is False:
        return _entry(tool_name, "blocked", "disabled_by_user", required, actual)
    if is_tool_rejected_by_policy(tool, policy):
        return _entry(tool_name, "blocked", "disabled_by_policy", required, actual)
    if connected_tool_names is not None and connected_tool_names and tool_name not in connected_tool_names:
        return _entry(tool_name, "blocked", "not_connected_to_profile", required, actual)

    attachment_policy = str(tool.get("attachment_policy") or metadata.get("attachment_policy") or "").strip().lower()
    supports_attachments = tool.get("supports_attachments")
    has_non_text_input = any(token in snapshot_obj.input_traits for token in ("input.image", "input.file"))
    if has_non_text_input and (supports_attachments is False or attachment_policy in {"none", "forbid", "forbidden"}):
        return _entry(tool_name, "blocked", "attachment_not_supported", required, actual)

    missing_model_caps = sorted(set(required["model_capabilities"]) - set(snapshot_obj.model_capabilities))
    if missing_model_caps:
        return _entry(tool_name, "blocked", "model_unsupported", required, actual, missing=missing_model_caps)
    missing_input = sorted(set(required["input_modalities"]) - set(snapshot_obj.input_traits))
    if missing_input:
        return _entry(tool_name, "blocked", "missing_input", required, actual, missing=missing_input)
    missing_runtime = sorted(set(required["runtime_capabilities"]) - set(snapshot_obj.runtime_capabilities))
    if missing_runtime:
        return _entry(tool_name, "blocked", "missing_capability", required, actual, missing=missing_runtime)

    cap_req = required["capability_requirements"]
    all_requirements = [token for token in cap_req.get("requires_all", []) if token]
    any_requirements = [token for token in cap_req.get("requires_any", []) if token]
    forbidden = [token for token in cap_req.get("forbids", []) if token]
    tags = set(snapshot_obj.tags)
    if any(token not in tags for token in all_requirements):
        return _entry(tool_name, "blocked", "missing_capability", required, actual, missing=[token for token in all_requirements if token not in tags])
    if any_requirements and not any(token in tags for token in any_requirements):
        return _entry(tool_name, "blocked", "missing_capability", required, actual, missing=any_requirements)
    if any(token in tags for token in forbidden):
        return _entry(tool_name, "blocked", "risk_blocked", required, actual, missing=[token for token in forbidden if token in tags])

    raw_availability = tool.get("availability")
    if isinstance(raw_availability, dict):
        availability = raw_availability
    else:
        availability = mapping_or_empty(metadata.get("availability"))
    if availability and str(availability.get("status") or "") == "missing_api_key":
        return _entry(tool_name, "blocked", "missing_api_key", required, actual)

    if tool_requires_approval_by_policy(tool, policy):
        return _entry(tool_name, "approval_required", "requires_approval", required, actual)

    return _entry(tool_name, "allowed", "", required, actual)


def rejection_result(tool_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    reason_code = str(entry.get("reason_code") or "missing_capability")
    return {
        "status": "rejected",
        "code": REJECTION_CODE_MAP.get(reason_code, "MISSING_CAPABILITY"),
        "reason_code": reason_code,
        "reason": entry.get("reason"),
        "required": entry.get("required"),
        "actual": entry.get("actual"),
        "repair_suggestions": list(entry.get("repair_suggestions") or []),
        "tool_name": tool_name,
    }


def _coerce_snapshot(snapshot: RuntimeCapabilitySnapshot | dict[str, Any]) -> RuntimeCapabilitySnapshot:
    if isinstance(snapshot, RuntimeCapabilitySnapshot):
        return snapshot
    value = snapshot if isinstance(snapshot, dict) else {}
    return RuntimeCapabilitySnapshot(
        input_traits=_string_list(value.get("input_traits")),
        model_capabilities=_string_list(value.get("model_capabilities")),
        runtime_capabilities=_string_list(value.get("runtime_capabilities")),
        policy_capabilities=_string_list(value.get("policy_capabilities")),
        tags=_string_list(value.get("tags")),
    )


def _tool_requirements(tool: dict[str, Any]) -> dict[str, Any]:
    tool = tool if isinstance(tool, dict) else {}
    metadata = mapping_or_empty(tool.get("metadata"))
    raw_capability_requirements = tool.get("capability_requirements")
    capability_requirements = (
        raw_capability_requirements
        if isinstance(raw_capability_requirements, dict)
        else mapping_or_empty(metadata.get("capability_requirements"))
    )
    return {
        "capability_requirements": {
            "requires_all": _string_list(capability_requirements.get("requires_all")),
            "requires_any": _string_list(capability_requirements.get("requires_any")),
            "forbids": _string_list(capability_requirements.get("forbids")),
        },
        "model_capabilities": _string_list(tool.get("requires_model_capabilities") or metadata.get("requires_model_capabilities")),
        "input_modalities": _string_list(tool.get("requires_input_modalities") or metadata.get("requires_input_modalities")),
        "runtime_capabilities": _string_list(tool.get("requires_runtime_capabilities") or metadata.get("requires_runtime_capabilities")),
        "attachment_policy": str(tool.get("attachment_policy") or metadata.get("attachment_policy") or "").strip(),
        "supports_attachments": tool.get("supports_attachments", metadata.get("supports_attachments")),
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        values = []
    return values


def _entry(
    tool_name: str,
    status: str,
    reason_code: str,
    required: dict[str, Any],
    actual: dict[str, Any],
    *,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": status,
        "reason_code": reason_code,
        "reason": _reason_text(reason_code, missing or []),
        "required": required,
        "actual": actual,
        "repair_suggestions": _repair_suggestions(reason_code),
    }


def _reason_text(reason_code: str, missing: list[str]) -> str:
    labels = {
        "missing_capability": "required runtime capability is missing",
        "missing_input": "required input modality is missing",
        "model_unsupported": "selected model does not support this tool",
        "disabled_by_user": "tool is disabled by the user",
        "disabled_by_policy": "tool is disabled by policy",
        "requires_approval": "tool requires approval",
        "not_connected_to_profile": "tool is not connected to the active runtime profile",
        "requires_trusted_workspace": "tool requires a trusted workspace",
        "missing_api_key": "tool setup is incomplete",
        "attachment_not_supported": "tool does not support the current attachments",
        "risk_blocked": "tool is blocked by risk policy",
    }
    base = labels.get(reason_code, reason_code)
    if missing:
        return "{}: {}".format(base, ", ".join(missing))
    return base


def _repair_suggestions(reason_code: str) -> list[str]:
    suggestions = {
        "missing_capability": ["Adjust the runtime profile or choose a compatible tool."],
        "missing_input": ["Attach the required input type and try again."],
        "model_unsupported": ["Switch to a compatible model or disable the tool for this turn."],
        "disabled_by_user": ["Enable the tool in settings or the tool manager."],
        "disabled_by_policy": ["Review tool policy settings or request a safer alternative."],
        "requires_approval": ["Approve the tool execution when prompted."],
        "not_connected_to_profile": ["Connect the tool in the active runtime profile."],
        "requires_trusted_workspace": ["Use a trusted workspace for this action."],
        "missing_api_key": ["Configure the required API key or integration setup."],
        "attachment_not_supported": ["Remove the attachment or pick a tool that supports attachments."],
        "risk_blocked": ["Use a lower-risk tool or adjust policy if appropriate."],
    }
    return list(suggestions.get(reason_code, []))
