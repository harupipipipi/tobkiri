from __future__ import annotations

from core_runtime.settings.models import (
    SettingContribution,
    SettingSectionId,
    SettingStatus,
)

from .models import MCPServerDefinition, MCPToolDefinition


def mcp_server_to_setting(server: MCPServerDefinition, discovered_tools: list[MCPToolDefinition]) -> SettingContribution:
    status: SettingStatus = "configured"
    if not server.enabled:
        status = "disabled"
    elif server.required_provider_id and not server.required_connection_id:
        status = "missing"

    return SettingContribution(
        id=f"mcp.server.{server.server_id}",
        owner="mcp",
        title=server.display_name,
        description=f"{len(discovered_tools)} tools discovered."
        + (f" Requires {server.required_provider_id} connection." if server.required_provider_id else ""),
        section=SettingSectionId.TOOLS_MCP,
        priority=60,
        frequency="weekly",
        audience="normal",
        risk="medium" if any(tool.risk_level != "read" for tool in discovered_tools) else "low",
        component="ToolsMcpPanel.MCPServerCard",
        requires=[f"connection.{server.required_provider_id}"] if server.required_provider_id else [],
        profile_aware=True,
        status=status,
        metadata={"server_id": server.server_id, "tool_count": len(discovered_tools)},
    )
