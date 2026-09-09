from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

from ecosystem.defaultspack.blocks.tool import mcp_connect as mcp_connect_block
from ecosystem.defaultspack.blocks.tool import mcp_list as mcp_list_block
from ecosystem.defaultspack.domain.tool.mcp_client import McpClient, McpConnections
from ecosystem.defaultspack.domain.tool import mcp_client as mcp_module
from ecosystem.defaultspack.domain.tool.registry import ToolRegistry
from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore
from domain.tool_policy.internal_context import mark_tool_server_approval_context

pytestmark = pytest.mark.usefixtures(
    "wave7_owner_bindings",
    "defaultspack_component_catalog_selected",
    "defaultspack_v4_tool_dispatch",
)


@pytest.fixture(autouse=True)
def _reset_mcp_singletons():
    _disconnect_mcp_servers()
    McpClient._instance = None
    ToolRegistry._instance = None
    yield
    _disconnect_mcp_servers()
    McpClient._instance = None
    ToolRegistry._instance = None


def _disconnect_mcp_servers() -> None:
    client = McpClient._instance
    if client is None:
        return
    for server in client.list_servers():
        try:
            client.disconnect(server["name"])
        except Exception:
            pass


def _write_demo_mcp_server(path: Path) -> None:
    path.write_text(
        """
import json
import sys

for raw_line in sys.stdin:
    raw_line = raw_line.strip()
    if not raw_line:
        continue
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "0.1.0"},
            },
        }
    elif method == "tools/list":
        response = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "tools": [
                    {
                        "name": "ping",
                        "description": "Return pong",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                        },
                    }
                ]
            },
        }
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        response = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "content": [
                    {"type": "text", "text": "pong:" + arguments.get("message", "")}
                ]
            },
        }
    else:
        continue
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _write_digest_mcp_server(path: Path) -> None:
    path.write_text(
        """
import hashlib
import json
import sys


def respond(message, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result}) + "\\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    raw_line = raw_line.strip()
    if not raw_line:
        continue
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        respond(message, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "digest-demo", "version": "0.1.0"},
        })
    elif method == "tools/list":
        respond(message, {
            "tools": [{
                "name": "digest",
                "description": "Compute a digest from caller supplied numbers and label.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "numbers": {"type": "array", "items": {"type": "number"}},
                    },
                    "required": ["label", "numbers"],
                },
            }]
        })
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        numbers = [int(value) for value in arguments.get("numbers", [])]
        label = str(arguments.get("label", ""))
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        respond(message, {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "label": label,
                    "sum": sum(numbers),
                    "count": len(numbers),
                    "digest": digest,
                }, sort_keys=True),
            }]
        })
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_mcp_client_supports_stdio_command_and_args(tmp_path):
    server_path = tmp_path / "demo_mcp_server.py"
    _write_demo_mcp_server(server_path)

    client = McpClient()
    tools_added = client.connect(
        "demo",
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(server_path)],
        },
    )

    assert tools_added == 1
    assert [tool["name"] for tool in client.get_server_tools("demo")] == ["ping"]
    assert client.invoke("demo", "ping", {"message": "hello"})["result"] == "pong:hello"


def test_owned_connections_do_not_share_names_or_disconnect_each_other(tmp_path):
    """Two real stdio connections may use the same name without sharing state."""
    server_path = tmp_path / "owned_mcp_server.py"
    _write_demo_mcp_server(server_path)
    first, second = McpConnections(), McpConnections()
    legacy = McpClient()
    config = {"transport": "stdio", "command": sys.executable,
              "args": [str(server_path)]}
    try:
        first.connect("same", config)
        assert second.list_servers() == []
        assert second.invoke("same", "ping", {})["is_error"] is True
        second.connect("same", config)
        assert first.invoke("same", "ping", {"message": "first"})["result"] == "pong:first"
        assert second.invoke("same", "ping", {"message": "second"})["result"] == "pong:second"
        first.disconnect("same")
        assert second.invoke("same", "ping", {"message": "retained"})["result"] == "pong:retained"
        assert legacy.list_servers() == []
        assert McpClient() is legacy
    finally:
        first.disconnect("same")
        second.disconnect("same")


@pytest.mark.parametrize("failure_stage", ["_initialize", "_list_tools"])
def test_failed_handshake_stops_started_stdio_process(
    monkeypatch, tmp_path, failure_stage
):
    """A failed handshake must not leave its already started child running."""
    server_path = tmp_path / "failed_handshake_server.py"
    _write_demo_mcp_server(server_path)
    client = McpConnections()
    processes = []

    def fail_handshake(connection):
        processes.append(connection._transport._proc)
        connection.server_capabilities = {"tools": {}}
        raise RuntimeError("injected handshake failure")

    monkeypatch.setattr(mcp_module._ServerConnection, failure_stage, fail_handshake)
    try:
        with pytest.raises(RuntimeError, match="injected handshake failure"):
            client.connect("failed", {
                "transport": "stdio", "command": sys.executable,
                "args": [str(server_path)],
            })
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert client.list_servers() == [
            {"name": "failed", "status": "error", "tools": []}
        ]
        assert client._servers["failed"]._transport is None
        assert client._servers["failed"].server_capabilities == {}
        assert client.invoke("failed", "ping", {})["is_error"] is True
    finally:
        client.disconnect("failed")
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_mcp_connect_accepts_server_id_and_saved_config(monkeypatch, tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_id": "filesystem",
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": ["demo_server.py"],
                        "tool_prefix": "mcp_fs",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClient:
        def __init__(self):
            self.connected = []

        def connect(self, server_name, config):
            self.connected.append((server_name, config))
            return 1

        def get_server_tools(self, server_name):
            return [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "inputSchema": {"type": "object"},
                }
            ]

    class FakeRegistry:
        def __init__(self):
            self.servers = {}
            self.tools = []

        def register_mcp_server(self, server_name, config):
            self.servers[server_name] = config

        def list_mcp_servers(self):
            return dict(self.servers)

        def register(self, tool_def):
            self.tools.append(tool_def)

    fake_client = FakeMcpClient()
    fake_registry = FakeRegistry()

    monkeypatch.setattr(mcp_connect_block, "_mcp_config_path", lambda: config_path)
    monkeypatch.setattr(mcp_connect_block, "McpClient", lambda: fake_client)
    monkeypatch.setattr(mcp_connect_block, "ToolRegistry", lambda: fake_registry)

    result = mcp_connect_block.run(
        {"server_id": "filesystem"},
        mark_tool_server_approval_context({}),
    )

    assert result["status"] == "ok"
    assert fake_client.connected[0][0] == "filesystem"
    assert result["data"]["server_id"] == "filesystem"
    assert result["data"]["tools"] == ["mcp_fs_read_file"]
    assert fake_registry.tools[0]["name"] == "mcp_fs_read_file"
    assert fake_registry.tools[0]["execution"]["mcp_tool_name"] == "read_file"
    assert fake_registry.tools[0]["metadata"]["source"] == "mcp"
    assert fake_registry.tools[0]["metadata"]["server_id"] == "filesystem"
    assert fake_registry.tools[0]["ui"]["group_id"] == "mcp"


def test_mcp_connect_approval_binds_resolved_saved_config(monkeypatch, tmp_path):
    from domain.safety.approval import approve, reset_approval_state_for_tests

    class EmptyMcpRegistry:
        def get_server(self, server_identifier):
            return None

        def add_server(self, payload):
            raise AssertionError("approval should be checked before connecting")

    def write_config(command: str) -> None:
        config_path.write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "server_id": "filesystem",
                            "transport": "stdio",
                            "command": command,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    reset_approval_state_for_tests()
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    config_path = tmp_path / "mcp.json"
    write_config("python demo_server.py")
    monkeypatch.setattr(mcp_connect_block, "_mcp_config_path", lambda: config_path)
    monkeypatch.setattr(mcp_connect_block, "McpRegistry", lambda: EmptyMcpRegistry())

    request = mcp_connect_block.run({"server_id": "filesystem"}, {})

    assert request["status"] == "ok"
    assert request["data"]["approval_required"] is True
    assert request["data"]["config"]["command"] == "python demo_server.py"

    decision = approve(request["data"]["approval_request_id"])
    write_config("python changed_server.py")
    tampered = mcp_connect_block.run(
        {"server_id": "filesystem", "approval_token": decision["token"]},
        {},
    )

    assert tampered["status"] == "error"
    assert tampered["error"]["code"] == "APPROVAL_ARGUMENTS_CHANGED"


def test_chat_run_executes_prefixless_mcp_tool_with_tool_log_evidence(monkeypatch, tmp_path):
    import domain.chat.run_request as run_request_module
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MCP_REGISTRY_PATH", str(tmp_path / "mcp_servers.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    ChatStore._instance = None
    ToolRegistry._instance = None
    McpClient._instance = None

    server_path = tmp_path / "digest_mcp_server.py"
    _write_digest_mcp_server(server_path)
    connect_result = mcp_connect_block.run(
        {
            "server_id": "digest_server",
            "config": {
                "server_id": "digest_server",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(server_path)],
            },
        },
        mark_tool_server_approval_context({}),
    )

    assert connect_result["status"] == "ok"
    tool_id = connect_result["data"]["tools"][0]
    assert tool_id == "mcp__digest_server__digest"

    requested_payload = {"label": "invoice-" + tmp_path.name[-6:], "numbers": [7, 11, 13]}
    expected_sum = sum(requested_payload["numbers"])
    expected_digest = hashlib.sha256(
        json.dumps(requested_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    class EvidenceCheckingClient:
        def __init__(self):
            self.calls = 0

        def supports_stream(self, _model):
            return True

        def stream(self, _model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                function_names = [tool["function"]["name"] for tool in tools or []]
                assert tool_id in function_names
                yield {"type": "tool_call_start", "id": "call_digest", "name": tool_id}
                yield {
                    "type": "tool_call_delta",
                    "id": "call_digest",
                    "name": tool_id,
                    "arguments_chunk": json.dumps(requested_payload),
                }
                yield {"type": "tool_call_end", "id": "call_digest", "name": tool_id}
                yield {"type": "stream_end", "finish_reason": "tool_calls"}
                return

            tool_messages = [
                message
                for message in messages
                if message.get("role") == "tool" and message.get("name") == tool_id
            ]
            assert tool_messages, "final model turn must receive the MCP result as a tool message"
            evidence = json.loads(tool_messages[-1]["content"])
            assert evidence["label"] == requested_payload["label"]
            assert evidence["sum"] == expected_sum
            yield {
                "type": "content_delta",
                "delta": {
                    "type": "text",
                    "text": f"{evidence['label']} total={evidence['sum']} digest={evidence['digest']}",
                },
            }
            yield {"type": "stream_end", "finish_reason": "stop"}

        def complete(self, *_args, **_kwargs):
            raise AssertionError("streaming tool path should be used")

    monkeypatch.setattr(
        ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True)
    )
    monkeypatch.setattr(
        run_request_module,
        "get_model_capabilities",
        lambda _model: {
            "profile_id": "openai/gpt-5.5",
            "supports_tool_calling": True,
            "supports_thinking": True,
        },
    )

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    events = list(
        ChatRunEngine(
            client=EvidenceCheckingClient(),
            settings_owner=FrontendSettingsStore(tmp_path / "settings.json"),
        ).stream(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "Calculate the invoice total with the MCP digest tool.",
                },
                "tools": [tool_id],
            },
            {"developer_mode": True},
            stream_mode=True,
        )
    )

    final_message = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    started = [
        event
        for event in final_message["events"]
        if event.get("type") == "tool_call_started" and event.get("tool_name") == tool_id
    ]
    completed = [
        event
        for event in final_message["events"]
        if event.get("type") == "tool_call_completed" and event.get("tool_name") == tool_id
    ]
    assert len(started) == 1
    assert len(completed) == 1
    attached = [
        event
        for event in final_message["events"]
        if event.get("phase") == "tools_attached"
    ]
    assert attached and attached[-1]["tool_count"] == 1
    assert final_message["tool_logs"][0]["tool_name"] == tool_id
    result_payload = json.loads(final_message["tool_logs"][0]["result"]["data"]["result"])
    assert result_payload == {
        "label": requested_payload["label"],
        "sum": expected_sum,
        "count": len(requested_payload["numbers"]),
        "digest": expected_digest,
    }


def test_mcp_tool_is_unverified_when_selected_model_cannot_call_tools(monkeypatch, tmp_path):
    import domain.chat.run_request as run_request_module
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    tool_id = "mcp__ephemeral__digest"
    tool_def = {
        "type": "function",
        "function": {
            "name": tool_id,
            "description": "Compute a digest.",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
            },
        },
    }

    class NoToolRoutingDecision:
        selected_model = "local/no-tools"
        original_model = "local/no-tools"
        selected_group = "default"
        reason_codes = ["preferred_model", "tool_calling_unavailable"]
        warnings = ["selected_model_does_not_support_tool_calling"]
        bridge_required = False
        bridge_plan = {}
        utility_models = {}
        explanation = "tool calling unavailable"

        def to_dict(self):
            return {
                "selected_model": self.selected_model,
                "original_model": self.original_model,
                "selected_group": self.selected_group,
                "reason_codes": list(self.reason_codes),
                "warnings": list(self.warnings),
                "bridge_required": self.bridge_required,
                "bridge_plan": dict(self.bridge_plan),
                "utility_models": dict(self.utility_models),
                "explanation": self.explanation,
            }

    monkeypatch.setattr(
        run_request_module, "route_model_request", lambda _request: NoToolRoutingDecision()
    )
    monkeypatch.setattr(
        run_request_module,
        "get_model_capabilities",
        lambda _model: {
            "profile_id": "local/no-tools",
            "supports_tool_calling": False,
            "supports_thinking": False,
        },
    )

    class TextOnlyClient:
        def complete(self, _model, _messages, tools=None, params=None):
            del params
            assert tools == []
            return {
                "content": [
                    {"type": "text", "text": "I used a tool-like answer without tool calls."}
                ],
                "finish_reason": "stop",
            }

        def supports_stream(self, _model):
            return False

    store = ChatStore()
    conversation = store.create_conversation(model="local/no-tools")
    events = list(
        ChatRunEngine(
            client=TextOnlyClient(),
            settings_owner=FrontendSettingsStore(tmp_path / "settings.json"),
        ).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "Please use the MCP digest tool."},
                "tools": [tool_def],
            },
            {},
            stream_mode=False,
        )
    )

    final_message = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert final_message["tool_logs"] == []
    assert not [
        event for event in final_message["events"] if event.get("type", "").startswith("tool_call_")
    ]
    metadata = final_message["metadata"]
    assert metadata["requested_tools"] == [tool_id]
    assert metadata["attached_tools"] == []
    assert metadata["attached_provider_tools"] == []
    assert metadata["executed_tools"] == []
    assert metadata["tool_calling_unverified"] is True
    assert (
        metadata["tool_calling_unavailable_reason"]
        == "selected_model_does_not_support_tool_calling"
    )
    assert "selected_model_does_not_support_tool_calling" in metadata["model_routing"]["warnings"]
    ChatStore._instance = None


def test_mcp_list_filters_by_server_id(monkeypatch):
    class FakeMcpClient:
        def list_servers(self):
            return [
                {"name": "filesystem", "status": "connected", "tools": ["read_file"]},
                {"name": "github", "status": "connected", "tools": ["search_issues"]},
            ]

        def get_server_tools(self, server_name):
            return [
                {
                    "name": "read_file" if server_name == "filesystem" else "search_issues",
                    "description": "",
                    "inputSchema": {},
                }
            ]

    class FakeRegistry:
        def list_mcp_servers(self):
            return {
                "filesystem": {"server_id": "filesystem"},
                "github": {"server_id": "github"},
            }

    monkeypatch.setattr(mcp_list_block, "McpClient", lambda: FakeMcpClient())
    monkeypatch.setattr(mcp_list_block, "ToolRegistry", lambda: FakeRegistry())

    result = mcp_list_block.run({"server_id": "filesystem"}, {})

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["servers"][0]["server_id"] == "filesystem"
    assert result["data"]["servers"][0]["tools"] == ["read_file"]
