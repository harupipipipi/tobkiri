import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _common import ok  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from domain.tool.mcp_client import McpClient  # noqa: E402
from domain.tool.mcp_registry import McpRegistry  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402


def run(input_data, context):
    """defaults.tool.mcp_list - return connected MCP servers and tool details."""
    mcp_client = McpClient()
    mcp_registry = McpRegistry()
    registry_servers = ToolRegistry().list_mcp_servers()
    persistent_servers = {server["server_id"]: server for server in mcp_registry.list_servers()}
    servers = mcp_client.list_servers()
    requested_server = str(
        input_data.get("server_id")
        or input_data.get("server_name")
        or ""
    ).strip()

    detailed_servers = []
    seen_server_ids = set()
    for srv in servers:
        server_name = srv.get("name", "")
        persistent = mcp_registry.get_server(server_name) or {}
        registered_config = McpRegistry.public_config(
            registry_servers.get(server_name, {}) or persistent.get("config", {})
        )
        server_id = str(registered_config.get("server_id", "") or server_name)
        seen_server_ids.add(server_id)
        if requested_server and requested_server not in {server_name, server_id}:
            continue

        raw_tools = mcp_client.get_server_tools(server_name)
        tool_details = []
        for tool in raw_tools:
            if isinstance(tool, dict):
                tool_details.append(
                    {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "inputSchema": tool.get("inputSchema", {}),
                    }
                )
            else:
                tool_details.append(
                    {"name": str(tool), "description": "", "inputSchema": {}}
                )
        detailed_servers.append(
            {
                "name": server_name,
                "server_name": server_name,
                "server_id": server_id,
                "status": srv.get("status", "unknown"),
                "tools": srv.get("tools", []),
                "tool_details": tool_details,
                "inspect": mcp_registry.inspect_server(server_name),
                "registered_config": registered_config,
                "permissions": persistent.get("permissions", {}),
                "connected": srv.get("status") == "connected",
            }
        )

    for server_id, server in sorted(persistent_servers.items()):
        server_name = str(server.get("name") or server_id)
        if server_id in seen_server_ids:
            continue
        if requested_server and requested_server not in {server_name, server_id}:
            continue
        detailed_servers.append(
            {
                "name": server_name,
                "server_name": server_name,
                "server_id": server_id,
                "status": server.get("status", "registered"),
                "connected": False,
                "tools": server.get("tools", []),
                "tool_details": [],
                "registered_config": McpRegistry.public_config(server.get("config", {})),
                "inspect": mcp_registry.inspect_server(server_id),
                "permissions": server.get("permissions", {}),
            }
        )

    return ok({"servers": detailed_servers, "count": len(detailed_servers)})
