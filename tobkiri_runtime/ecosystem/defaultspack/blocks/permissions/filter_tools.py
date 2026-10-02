from pathlib import Path

import yaml


from blocks._common import ok


_WRITE_ACTIONS = ("write", "delete", "update", "create", "patch", "commit", "push")


def _load_policy(workspace):
    if not isinstance(workspace, dict):
        return {}
    policy_path = Path(str(workspace.get("permissions_dir") or "")) / "tool_policy.yaml"
    if not policy_path.is_file():
        return {}
    data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _tool_name(tool):
    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        return str(tool.get("name") or tool.get("id") or tool.get("function", {}).get("name") or "")
    return ""


def _tool_risk(tool):
    if isinstance(tool, dict):
        return str(tool.get("risk") or tool.get("risk_level") or "").lower()
    return ""


def _requires_network(tool):
    return isinstance(tool, dict) and bool(tool.get("requires_network") or tool.get("network"))


def _is_write_like_tool(name):
    lowered = name.lower()
    return any(action in lowered for action in _WRITE_ACTIONS)


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    tools = data.get("tools")
    if not isinstance(tools, list):
        tools = []
    workspace_policy = _load_policy(data.get("workspace"))
    policy = {
        "network_default": workspace_policy.get("network_default", "deny"),
        "write_actions_require_approval": True,
        "high_risk_tools_require_approval": bool(workspace_policy.get("high_risk_tools_require_approval", True)),
        "allow_client_supplied_approved": False,
    }
    permitted = []
    blocked = []
    approval_required = []
    for tool in tools:
        name = _tool_name(tool)
        risk = _tool_risk(tool)
        requires_approval = False
        if risk == "high":
            requires_approval = True
        if _is_write_like_tool(name):
            requires_approval = True
        if policy["network_default"] == "deny" and _requires_network(tool):
            blocked.append({"tool": tool, "reason": "network_denied_by_profile_default"})
            continue
        normalized = dict(tool) if isinstance(tool, dict) else {"name": name}
        normalized.pop("approved", None)
        normalized["requires_approval"] = requires_approval
        permitted.append(normalized)
        if requires_approval:
            approval_required.append(normalized)
    return ok(
        {
            "profile_id": data.get("profile_id"),
            "tools": permitted,
            "blocked": blocked,
            "approval_required": approval_required,
            "policy": policy,
        }
    )
