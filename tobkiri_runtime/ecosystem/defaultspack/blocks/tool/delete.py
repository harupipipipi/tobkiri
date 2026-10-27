from blocks._common import ok, error, gen_id, timestamp
from domain.tool.registry import ToolRegistry
from blocks.tool._safety import (
    approved_or_request,
    record_tool_attempt,
    record_tool_execution,
    record_tool_failure,
)


OPERATION = "tool.delete"
RISK = "high"


def run(input_data, context):
    """defaults.tool.delete — 動的ツールを削除する"""
    name = input_data.get("name")
    if not name:
        return error("name is required", "MISSING_PARAM")

    registry = ToolRegistry()

    existing = registry.get(name)
    if existing is None:
        return error("Tool '{}' not found".format(name), "NOT_FOUND")

    exec_type = existing.get("execution", {}).get("type", "")
    if exec_type != "dynamic":
        return error("Only dynamic tools can be deleted", "NOT_DYNAMIC")

    record_tool_attempt(OPERATION, RISK, input_data)
    approval = approved_or_request(input_data, context, OPERATION, RISK)
    if approval is not None:
        return approval

    deleted = registry.unregister_dynamic(name)
    if deleted is None:
        record_tool_failure(OPERATION, RISK, input_data, "delete returned None", tool_name=name)
        return error("Failed to delete tool '{}'".format(name), "DELETE_ERROR")

    record_tool_execution(OPERATION, RISK, input_data, tool_name=name)
    return ok({
        "deleted": name,
        "tool_id": deleted.get("tool_id", name),
    })
