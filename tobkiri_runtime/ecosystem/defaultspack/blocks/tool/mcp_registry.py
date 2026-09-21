from __future__ import annotations

from blocks._common import error, ok
from domain.tool.mcp_approval import obsolete_mcp_approvals
from blocks.tool._safety import (
    approved_or_request,
    record_tool_attempt,
    record_tool_execution,
    record_tool_failure,
)
from domain.tool.mcp_client import McpClient
from domain.tool.mcp_registry import McpRegistry
from domain.tool.registry import ToolRegistry


_LIFECYCLE_OPERATIONS = {
    "disconnect": "tool.mcp_disconnect",
    "remove": "tool.mcp_remove",
}


def _server_id(input_data):
    return str(
        input_data.get("server_id")
        or input_data.get("server_name")
        or input_data.get("name")
        or ""
    ).strip()


def _run_disconnect(registry, server_id):
    server = registry.get_server(server_id)
    if server is None:
        return error("MCP server '{}' not found".format(server_id), code="NOT_FOUND")
    server_name = str(server.get("server_name") or server.get("name") or server_id)
    McpClient().disconnect(server_name)
    removed_tools = ToolRegistry().unregister_mcp_server(server_name)
    registry.mark_disconnected(server_id)
    return ok(
        {
            "server_id": server_id,
            "status": "disconnected",
            "inspect": registry.inspect_server(server_id),
            "removed_runtime_tools": removed_tools,
        }
    )


def _run_remove(registry, server_id):
    disconnected = _run_disconnect(registry, server_id)
    if disconnected.get("status") == "error":
        return disconnected
    if not registry.delete_server(server_id):
        return error("MCP server '{}' could not be removed".format(server_id), code="REMOVE_FAILED")
    data = disconnected.get("data") if isinstance(disconnected.get("data"), dict) else {}
    return ok(
        {
            "server_id": server_id,
            "removed": True,
            "status": "removed",
            "removed_runtime_tools": data.get("removed_runtime_tools", []),
        }
    )


def _run_lifecycle(input_data, context, action):
    server_id = _server_id(input_data)
    if not server_id:
        return error("'server_id' is required", code="INVALID_INPUT")
    registry = McpRegistry()
    if registry.get_server(server_id) is None:
        return error("MCP server '{}' not found".format(server_id), code="NOT_FOUND")

    if action == "reconnect":
        config = registry.get_server_config(server_id)
        if config is None:
            return error("MCP server configuration is unavailable", code="CONFIG_UNAVAILABLE")
        # Reuse the canonical connection block.  It creates a fresh approval
        # request for the saved configuration and records the standard audit
        # events, so a prior connection grant is never silently reused.
        from blocks.tool.mcp_connect import run as connect_mcp

        replay_input = dict(input_data)
        replay_input["server_id"] = server_id
        replay_input["config"] = config
        return connect_mcp(replay_input, context)

    if action == "remove" and input_data.get("confirm") is not True:
        return error(
            "Set confirm=true to remove this MCP registration; disconnect keeps the registration.",
            code="CONFIRMATION_REQUIRED",
        )

    operation = _LIFECYCLE_OPERATIONS[action]
    approval_input = dict(input_data)
    approval_input["server_id"] = server_id
    config_digest = registry.config_digest(server_id)
    if config_digest is None:
        return error("MCP server configuration is unavailable", code="CONFIG_UNAVAILABLE")
    approval_input["config_digest"] = config_digest
    record_tool_attempt(operation, "high", approval_input)
    approval = approved_or_request(approval_input, context, operation, "high")
    if approval is not None:
        return approval

    try:
        result = _run_remove(registry, server_id) if action == "remove" else _run_disconnect(registry, server_id)
    except Exception as exc:
        record_tool_failure(operation, "high", approval_input, str(exc), server_id=server_id)
        return error("MCP {} failed: {}".format(action, exc), code="MCP_LIFECYCLE_ERROR")
    if result.get("status") == "error":
        details = result.get("error") if isinstance(result.get("error"), dict) else {}
        record_tool_failure(operation, "high", approval_input, str(details.get("message") or action), server_id=server_id)
        return result
    record_tool_execution(operation, "high", approval_input, server_id=server_id, action=action)
    return result


def run(input_data, context=None):
    registry = McpRegistry()
    method = str(input_data.get("_method") or input_data.get("method") or "GET").upper()
    action = str(input_data.get("action") or "").strip().lower()
    try:
        if action in {"disconnect", "reconnect", "remove", "delete"}:
            if action == "delete":
                action = "remove"
            return _run_lifecycle(input_data, context, action)
        if method == "DELETE":
            return _run_lifecycle({**input_data, "action": "remove"}, context, "remove")
        if method == "GET" or action == "list":
            from blocks.tool.mcp_list import run as list_mcp

            return list_mcp(input_data, context)
        if method == "POST" or action in {"add", "create", "upsert"}:
            payload = (
                input_data.get("server")
                if isinstance(input_data.get("server"), dict)
                else input_data
            )
            server = registry.add_server(payload)
            obsolete_mcp_approvals(
                str(server.get("server_id") or ""),
                reason="MCP server configuration was updated",
            )
            return ok({"server": server})
        return error("unsupported method: " + method, code="INVALID_INPUT")
    except ValueError as exc:
        return error(str(exc), code="INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), code="MCP_REGISTRY_ERROR")
