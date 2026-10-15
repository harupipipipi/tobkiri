from blocks._common import ok, error, gen_id, timestamp
from domain.tool.permission_checker import PermissionChecker
from domain.tool.registry import ToolRegistry


def run(input_data, context):
    """defaults.tool.list — 登録済みツール一覧を返す"""
    filter_dict = input_data.get("filter")

    registry = ToolRegistry()
    checker = PermissionChecker(registry=registry)
    tools_raw = registry.list_tools(filter_dict=filter_dict)

    tools = []
    for t in tools_raw:
        tool_name = t.get("tool_id") or t.get("name")
        decision = checker.decide(tool_name, context=context, tool_def=t)
        tools.append({
            "tool_id": t["tool_id"],
            "name": t["name"],
            "summary": t["summary"],
            "tags": t.get("tags", []),
            "schema": t.get("schema", {}),
            "execution": t.get("execution", {}),
            "permission": {
                "action": decision.get("action", "allow"),
                "allowed": decision.get("allowed", False),
                "matched_by": decision.get("matched_by"),
                "matched_value": decision.get("matched_value"),
                "reason": decision.get("reason", ""),
            },
        })

    return ok({"tools": tools, "count": len(tools)})
