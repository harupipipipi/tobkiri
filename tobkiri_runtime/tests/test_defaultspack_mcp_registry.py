from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_mcp_registry_persists_servers_and_permission_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MCP_REGISTRY_PATH", str(tmp_path / "mcp_servers.json"))

    from domain.tool.mcp_registry import McpRegistry

    registry = McpRegistry()
    server = registry.add_server(
        {
            "server_id": "filesystem",
            "name": "Filesystem",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
            "permissions": {"approved": False, "scopes": ["files"], "risk": "high"},
        }
    )

    assert server["server_id"] == "filesystem"
    assert server["permissions"]["approved"] is False
    assert McpRegistry().get_server("filesystem")["config"]["command"] == "npx"

    registry.mark_connected("filesystem", tools=["mcp__filesystem__read_file"], approved=True)
    persisted = McpRegistry().get_server("filesystem")

    assert persisted["connected"] is True
    assert persisted["permissions"]["approved"] is True
    assert persisted["tools"] == ["mcp__filesystem__read_file"]


def test_unapproved_mcp_server_tool_execution_fails_closed(
    tmp_path, monkeypatch, defaultspack_capability_plan_context
):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MCP_REGISTRY_PATH", str(tmp_path / "mcp_servers.json"))

    from domain.tool.executor import ToolExecutor
    from domain.tool.mcp_client import McpClient
    from domain.tool.mcp_registry import McpRegistry
    from domain.tool.registry import ToolRegistry

    McpClient._instance = None
    ToolRegistry._instance = None
    registry = ToolRegistry()
    registry.register(
        {
            "tool_id": "mcp_ping",
            "name": "mcp_ping",
            "execution": {"type": "mcp", "server_name": "demo", "mcp_tool_name": "ping"},
        }
    )
    McpRegistry().add_server({"server_id": "demo", "transport": "stdio", "command": "python"})
    plan_context = defaultspack_capability_plan_context("mcp_ping")

    blocked = ToolExecutor().execute(
        "mcp_ping", {"message": "hello"}, plan_context
    )

    assert blocked["is_error"] is True
    assert blocked["approval_required"] is True
    assert "not approved" in blocked["result"]

    McpRegistry().mark_connected("demo", approved=True)
    allowed = ToolExecutor().execute(
        "mcp_ping", {"message": "hello"}, plan_context
    )

    assert allowed["is_error"] is True
    assert "not connected" in allowed["result"]
