from blocks._common import ok, error, gen_id, timestamp
from domain.tool.registry import ToolRegistry


def run(input_data, context):
    """defaults.tool.export — ツール定義を JSON でエクスポートする"""
    name = input_data.get("name")
    names = input_data.get("names")

    if name is None and names is None:
        return error("name or names is required", "MISSING_PARAM")

    registry = ToolRegistry()

    # 単一ツールの場合
    if name is not None and names is None:
        names = [name]

    if not isinstance(names, list) or len(names) == 0:
        return error("names must be a non-empty list", "INVALID_PARAM")

    exported = []
    not_found = []

    for tool_name in names:
        tool_export = registry.export_tool(tool_name)
        if tool_export is None:
            not_found.append(tool_name)
        else:
            exported.append(tool_export)

    if not exported and not_found:
        return error("Tools not found: {}".format(", ".join(not_found)), "NOT_FOUND")

    result = {
        "tools": exported,
        "count": len(exported),
    }
    if not_found:
        result["not_found"] = not_found

    return ok(result)
