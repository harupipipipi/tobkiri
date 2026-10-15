from blocks._common import ok, error, gen_id, timestamp
from domain.tool.registry import ToolRegistry


def run(input_data, context):
    """defaults.tool.schema — ツールのスキーマを返す"""
    tool_name = input_data.get("tool_name")
    if not tool_name:
        return error("tool_name is required", "MISSING_PARAM")

    registry = ToolRegistry()
    schema = registry.get_schema(tool_name)

    if schema is None:
        return error("Tool not found", "NOT_FOUND")

    return ok({
        "schema": schema,
        "guide": None
    })
