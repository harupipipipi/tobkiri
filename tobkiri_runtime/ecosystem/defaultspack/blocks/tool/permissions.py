from blocks._common import ok, error, timestamp

from backend.tool.permission_policy import get_tool_permission_policy_manager
from domain.tool.registry import ToolRegistry


def _tool_name(input_data):
    return input_data.get("name") or input_data.get("tool_name") or ""


def run_get(input_data, context):
    del context
    manager = get_tool_permission_policy_manager()
    tool_name = _tool_name(input_data)
    if tool_name:
        registry = ToolRegistry()
        tool_def = registry.get(tool_name)
        if tool_def is None:
            for item in registry.list_tools():
                if item.get("name") == tool_name:
                    tool_def = item
                    break
        evaluation = manager.evaluate(tool_name, tool_def=tool_def)
        return ok({"tool_name": tool_name, "decision": evaluation, "policy": evaluation.get("policy", {})})
    policy = manager.load()
    return ok({"policies": policy.get("tools", {}), "policy": policy})


def run_put(input_data, context):
    del context
    manager = get_tool_permission_policy_manager()
    tool_name = _tool_name(input_data)
    policy = input_data.get("policy", {})
    if not isinstance(policy, dict):
        return error("policy must be an object", "INVALID_PARAM")
    policy = dict(policy)
    policy.setdefault("updated_at", timestamp())
    replace = bool(input_data.get("replace", False)) and not tool_name
    if tool_name:
        tools = dict(policy.get("tools", {}))
        action = policy.get("action") or policy.get("mode")
        if action:
            tools[tool_name] = action
        policy["tools"] = tools
    stored = manager.update(policy, replace=replace)
    return ok({"tool_name": tool_name, "policy": stored})


def run_check(input_data, context):
    del context
    manager = get_tool_permission_policy_manager()
    tool_name = _tool_name(input_data)
    if not tool_name:
        return error("tool_name is required", "MISSING_PARAM")
    registry = ToolRegistry()
    tool_def = registry.get(tool_name)
    if tool_def is None:
        for item in registry.list_tools():
            if item.get("name") == tool_name:
                tool_def = item
                break
    decision = manager.decide(
        tool_name=tool_name,
        tool_def=tool_def,
        arguments=input_data.get("arguments"),
    )
    return ok({"tool_name": tool_name, "decision": decision})


def run(input_data, context):
    handler = str((input_data or {}).get("_handler") or "").strip()
    if handler == "run_put":
        return run_put(input_data, context)
    if handler == "run_check":
        return run_check(input_data, context)
    return run_get(input_data, context)
