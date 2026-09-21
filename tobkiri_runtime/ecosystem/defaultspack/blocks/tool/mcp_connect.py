import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _common import error, ok  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from domain.tool.mcp_client import McpClient  # noqa: E402
from domain.tool.mcp_approval import (  # noqa: E402
    build_mcp_snapshot,
    create_mcp_approval_request,
    obsolete_mcp_approvals,
    verify_mcp_approval,
)
from domain.tool.mcp_registry import McpRegistry  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.tool_policy.internal_context import (  # noqa: E402
    tool_server_approval_context_is_internal,
)
from blocks.tool._safety import (  # noqa: E402
    record_tool_attempt,
    record_tool_execution,
    record_tool_failure,
)
from domain.safety.audit import record_approval, record_denial  # noqa: E402


OPERATION = "tool.mcp_connect"
RISK = "high"


def _mcp_config_path():
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "tools" / "mcp.json"


def _load_saved_mcp_config(server_identifier):
    config_path = _mcp_config_path()
    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        modern = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
        if isinstance(modern, dict):
            candidate = modern.get(server_identifier)
            if isinstance(candidate, dict):
                return dict(candidate)
        servers = raw.get("servers", []) if isinstance(raw, dict) else raw
        if isinstance(servers, dict):
            servers = list(servers.values())
        if isinstance(servers, list):
            for server in servers:
                if not isinstance(server, dict):
                    continue
                candidates = {
                    str(server.get("server_id", "") or "").strip(),
                    str(server.get("name", "") or "").strip(),
                }
                if server_identifier in candidates:
                    return dict(server)
    registry = McpRegistry()
    get_server_config = getattr(registry, "get_server_config", None)
    registry_config = get_server_config(server_identifier) if callable(get_server_config) else None
    if registry_config:
        return registry_config
    registry_server = registry.get_server(server_identifier)
    if registry_server:
        return dict(registry_server.get("config") or {})
    return None


def _resolve_config(input_data, requested_server):
    config = input_data.get("config")
    if config is not None:
        return dict(config), "inline"
    if not requested_server:
        return None, "missing"
    registry_server = McpRegistry().get_server(requested_server)
    if registry_server:
        return dict(registry_server.get("config") or {}), "registry"
    saved = _load_saved_mcp_config(requested_server)
    return (saved, "shared_mcp_json") if saved is not None else (None, "missing")


def _approval_token(input_data):
    token = str(input_data.get("approval_token") or "").strip()
    if token:
        return token
    headers = input_data.get("_headers")
    if isinstance(headers, dict):
        return str(headers.get("X-Rumi-Approval") or headers.get("x-rumi-approval") or "").strip()
    return ""


def _resolve_server_name(input_data, config):
    for candidate in (
        input_data.get("server_id"),
        config.get("server_id") if isinstance(config, dict) else None,
        input_data.get("server_name"),
        config.get("name") if isinstance(config, dict) else None,
    ):
        server_name = str(candidate or "").strip()
        if server_name:
            return server_name
    return ""


def _public_tool_name(tool_name, config):
    prefix = ""
    if isinstance(config, dict):
        prefix = str(config.get("tool_prefix", "") or "").strip()
    if prefix:
        return "{}_{}".format(prefix, tool_name)
    return tool_name


def _tool_registry_id(server_name, tool_name, config):
    public_name = _public_tool_name(tool_name, config)
    if public_name != tool_name:
        return public_name
    return "mcp__{}__{}".format(server_name, tool_name)


def run(input_data, context):
    """defaults.tool.mcp_connect - connect to an MCP server."""
    requested_server = str(
        input_data.get("server_id") or input_data.get("server_name") or ""
    ).strip()
    config, server_source = _resolve_config(input_data, requested_server)

    server_name = _resolve_server_name(input_data, config)
    if not server_name:
        return error("server_name or server_id is required", "MISSING_PARAM")
    if config is None:
        return error(
            "config is required, or provide a server_id present in mcp.json",
            "MISSING_PARAM",
        )

    try:
        snapshot = build_mcp_snapshot(
            server_name,
            config,
            server_source=server_source,
            input_data=dict(input_data or {}),
            context=context,
        )
    except (OSError, ValueError) as exc:
        return error(str(exc), "INVALID_PARAM")

    effective_config = snapshot["effective_config"]
    transport = effective_config["transport"]
    approval_input = dict(snapshot["binding_args"])
    approval_verified = tool_server_approval_context_is_internal(context)

    record_tool_attempt(OPERATION, RISK, approval_input)
    if not approval_verified:
        token = _approval_token(input_data)
        if not token:
            obsolete_mcp_approvals(
                server_name,
                keep_scope_digest=snapshot["scope_digest"],
            )
            request = create_mcp_approval_request(snapshot)
            record_approval(
                OPERATION,
                request["approval_request_id"],
                "requested",
                risk_level=RISK,
            )
            return ok(request)
        verification = verify_mcp_approval(token, snapshot)
        if not verification.valid:
            record_denial(
                OPERATION,
                RISK,
                verification.code or "APPROVAL_INVALID",
                approval_input,
                request_id=verification.request_id,
            )
            result = error(
                verification.message or "approval token is invalid",
                verification.code or "APPROVAL_INVALID",
                details={
                    "recoverable": True,
                    "action": "request_new_approval",
                    "server_id": server_name,
                },
            )
            result["_http_status"] = 403
            return result
        record_approval(
            OPERATION,
            verification.request_id,
            "token_accepted",
        )
        approval_verified = verification.valid

    mcp_registry = McpRegistry()
    mcp_registry.add_server(
        {
            "server_id": server_name,
            "name": server_name,
            "config": effective_config,
        }
    )

    mcp_client = McpClient()
    registry = ToolRegistry()
    unregister = getattr(registry, "unregister_mcp_server", None)
    if callable(unregister):
        unregister(server_name)
    try:
        tools_added = mcp_client.connect(server_name, effective_config)
    except Exception:
        safe_failure = "MCP server failed to start or initialize"
        record_tool_failure(
            OPERATION,
            RISK,
            approval_input,
            safe_failure,
            server_name=server_name,
        )
        if hasattr(mcp_registry, "mark_connection_failed"):
            mcp_registry.mark_connection_failed(server_name, reason=safe_failure)
        return error(
            safe_failure,
            "MCP_CONNECT_ERROR",
            details={
                "recoverable": True,
                "action": "retry_connection",
                "requires_new_approval": True,
                "server_id": server_name,
            },
        )

    registry = ToolRegistry()
    registry.register_mcp_server(server_name, effective_config)

    server_tools = mcp_client.get_server_tools(server_name)
    registered_tools = []
    for tool in server_tools:
        if not isinstance(tool, dict):
            continue
        tool_name = tool.get("name", "")
        if not tool_name:
            continue
        public_name = _public_tool_name(tool_name, effective_config)
        tool_id = _tool_registry_id(server_name, tool_name, effective_config)
        server_id = str(effective_config.get("server_id", "") or server_name)
        description = str(tool.get("description", "") or "")
        registered_tools.append(tool_id)
        registry.register(
            {
                "tool_id": tool_id,
                "name": public_name,
                "summary": description,
                "tags": ["mcp", server_name],
                "schema": {"parameters": tool.get("inputSchema", {})},
                "execution": {
                    "type": "mcp",
                    "server_name": server_name,
                    "mcp_tool_name": tool_name,
                },
                "category": "tool",
                "ui": {
                    "group_id": "mcp",
                    "group_label": "MCP",
                    "group_icon": "terminal",
                    "label": public_name,
                    "description": description,
                    "keywords": " ".join(["mcp", server_name, server_id, tool_name, public_name]),
                },
                "metadata": {
                    "source": "mcp",
                    "server_id": server_id,
                    "server_name": server_name,
                    "mcp_tool_name": tool_name,
                    "transport": transport,
                    "description": description,
                },
            }
        )

    record_tool_execution(
        OPERATION, RISK, approval_input, server_name=server_name, tools_added=tools_added
    )
    mcp_registry.mark_connected(
        server_name,
        tools=registered_tools,
        approved=approval_verified,
    )
    inspect = mcp_registry.inspect_server(server_name)
    return ok(
        {
            "server_id": str(effective_config.get("server_id", "") or server_name),
            "server_name": server_name,
            "status": "connected",
            "tools_added": tools_added,
            "tools": registered_tools,
            "permission": {
                "approved": approval_verified,
                "source": "verified_local_approval",
            },
            "server": {
                "name": server_name,
                "transport": transport,
                "config": dict(snapshot["review"]["config"]),
                "config_digest": snapshot["config_digest"],
                "inspect": inspect,
            },
        }
    )
