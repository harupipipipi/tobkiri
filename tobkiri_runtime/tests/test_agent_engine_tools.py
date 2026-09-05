from __future__ import annotations

import sys
from pathlib import Path

import pytest

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.agent.engine import AgentEngine  # noqa: E402
from domain.tool.schema_adapter import adapt_tool_definition, runtime_profile_enforced_tool_names  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_agent_model_routing(monkeypatch):
    from domain.ai_client.model_router import ModelRoutingDecision

    monkeypatch.setattr(
        "domain.agent.engine.ModelRuntimeSettingsService.get_settings",
        lambda self: {
            "preferred_model": "stub/model",
            "preferred_model_group": "default",
            "auto_route_within_group": True,
        },
    )
    monkeypatch.setattr(
        "domain.agent.engine.get_model_capabilities",
        lambda model: {
            "profile_id": model,
            "supports_tool_calling": True,
            "supports_vision": True,
            "supports_image_input": True,
            "supports_thinking": True,
            "supports_fast": True,
        },
    )

    def fake_route(request):
        return ModelRoutingDecision(
            selected_model=request.preferred_model or "stub/model",
            original_model=request.preferred_model,
            selected_group=request.preferred_group or "default",
            reason_codes=["test_model_routing"],
            warnings=[],
        )

    monkeypatch.setattr("domain.agent.engine.route_model_request", fake_route)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _registry_tool(name: str) -> dict:
    return {
        "tool_id": name,
        "name": name,
        "summary": "registry tool",
        "schema": {
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            }
        },
    }


def _runtime_profile(*, connected_tools: list[str]) -> dict:
    return {
        "version": "rumi.runtime_profile.v1",
        "defaultspack": {
            "agents": {
                "agent": {
                    "tools": connected_tools,
                }
            }
        },
    }


def _compiled_bundle_runtime_profile(*, bundle_tools: list[str] | None = None) -> dict:
    tool_record = {
        "node_instance_id": "tools",
        "node_id": "defaultspack.tool",
    }
    if bundle_tools is not None:
        tool_record["tools"] = bundle_tools
    return {
        "version": "rumi.runtime_profile.v1",
        "defaultspack": {
            "agents": {
                "agent": {
                    "tools": ["tools"],
                }
            },
            "tools": {
                "tools": tool_record,
            },
        },
    }


def _text_response(content: str = "done") -> dict:
    return {"status": "ok", "data": {"content": content}}


def test_adapt_tool_definition_preserves_anthropic_input_schema() -> None:
    adapted = adapt_tool_definition(
        {
            "name": "multiply_exactly",
            "description": "Multiply two integers.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        }
    )

    assert adapted["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
    }


def _tool_call_response(name: str, arguments: str = '{"q": "rumi"}') -> dict:
    return {
        "status": "ok",
        "data": {
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": name, "arguments": arguments},
                }
            ]
        },
    }


def _image_attachment() -> dict:
    return {
        "id": "img-1",
        "name": "tiny.png",
        "type": "image/png",
        "dataUrl": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII=",
        "size": 68,
    }


def test_agent_execute_passes_tools_to_ai_completion() -> None:
    engine = AgentEngine()
    tools = [_tool("search")]
    seen = {}

    def fake_ai(messages, model, context, tools=None):
        seen["tools"] = tools
        return _text_response()

    engine._ai_complete = fake_ai

    result = engine.execute("find docs", tools, "stub/model", None, {})

    assert result["status"] == "completed"
    assert seen["tools"] == tools


def test_agent_delegate_with_image_requires_vision_model_or_bridge(monkeypatch) -> None:
    from domain.ai_client.model_router import ModelRoutingDecision

    engine = AgentEngine()
    seen = {}

    def fake_route(request):
        seen["routing_request"] = request
        return ModelRoutingDecision(
            selected_model="demo/vision",
            original_model=request.preferred_model,
            selected_group="default",
            reason_codes=["requires_vision"],
            warnings=[],
        )

    def fake_caps(model):
        return {
            "supports_vision": model == "demo/vision",
            "supports_image_input": model == "demo/vision",
            "supports_tool_calling": True,
            "supports_thinking": True,
        }

    def fake_ai(messages, model, context, tools=None):
        seen["messages"] = messages
        seen["model"] = model
        seen["context"] = context
        return _text_response()

    monkeypatch.setattr("domain.agent.engine.route_model_request", fake_route)
    monkeypatch.setattr("domain.agent.engine.get_model_capabilities", fake_caps)
    engine._ai_complete = fake_ai

    result = engine.execute(
        "inspect this image",
        [],
        "stub/default",
        None,
        {
            "required_capabilities": ["model.image_input"],
            "attachments": [_image_attachment()],
        },
    )

    assert result["status"] == "completed"
    assert seen["routing_request"].has_images is True
    assert seen["model"] == "demo/vision"
    assert result["result"]["context"]["required_capabilities"] == ["model.image_input"]
    user_message = next(message for message in seen["messages"] if message["role"] == "user")
    assert user_message["content"][1]["type"] == "image_url"


def test_agent_delegate_tools_imply_tool_calling_route(monkeypatch) -> None:
    from domain.ai_client.model_router import ModelRoutingDecision

    engine = AgentEngine()
    seen = {}

    def fake_route(request):
        seen["routing_request"] = request
        return ModelRoutingDecision(
            selected_model="demo/tools",
            original_model=request.preferred_model,
            selected_group="default",
            reason_codes=["requires_tool_calling"],
            warnings=[],
        )

    def fake_caps(model):
        return {
            "supports_tool_calling": model == "demo/tools",
            "supports_vision": False,
            "supports_image_input": False,
            "supports_thinking": False,
        }

    def fake_ai(messages, model, context, tools=None):
        seen["model"] = model
        seen["tools"] = tools
        return _text_response()

    monkeypatch.setattr("domain.agent.engine.route_model_request", fake_route)
    monkeypatch.setattr("domain.agent.engine.get_model_capabilities", fake_caps)
    engine._ai_complete = fake_ai

    result = engine.execute(
        "search docs",
        [_tool("web_search")],
        "stub/default",
        None,
        {},
    )

    assert result["status"] == "completed"
    assert seen["routing_request"].requested_tools == ["web_search"]
    assert seen["routing_request"].requires_tool_calling is True
    assert seen["model"] == "demo/tools"
    assert seen["tools"][0]["function"]["name"] == "web_search"


def test_agent_delegate_attachments_reach_agent_context(monkeypatch) -> None:
    from domain.ai_client.model_router import ModelRoutingDecision

    engine = AgentEngine()
    seen = {}

    def fake_route(request):
        return ModelRoutingDecision(
            selected_model=request.preferred_model,
            original_model=request.preferred_model,
            selected_group="default",
            reason_codes=["test"],
            warnings=[],
        )

    def fake_ai(messages, model, context, tools=None):
        seen["messages"] = messages
        seen["context"] = context
        return _text_response()

    monkeypatch.setattr("domain.agent.engine.route_model_request", fake_route)
    monkeypatch.setattr("domain.agent.engine.get_model_capabilities", lambda model: {"supports_tool_calling": True})
    engine._ai_complete = fake_ai

    result = engine.execute(
        "read attachment",
        [],
        "stub/default",
        None,
        {"attachments": [{"id": "file-1", "name": "notes.md", "type": "text/markdown"}]},
    )

    assert result["status"] == "completed"
    assert result["result"]["context"]["attachments"][0]["name"] == "notes.md"
    user_message = next(message for message in seen["messages"] if message["role"] == "user")
    assert user_message["content"][1]["text"] == "[attachment] notes.md"


def test_agent_plan_does_not_expose_tools_to_ai_completion() -> None:
    engine = AgentEngine()
    seen = {}

    def fake_ai(messages, model, context, tools=None):
        seen["tools"] = tools
        return _text_response("1. Inspect\n2. Report")

    engine._ai_complete = fake_ai

    result = engine.plan(
        "make a plan",
        [_tool("search")],
        "stub/model",
        None,
        {"runtime_profile": _runtime_profile(connected_tools=["search"])},
    )

    assert result["status"] == "planned"
    assert seen["tools"] == []


def test_defaultspack_tool_schema_adapter_normalizes_registry_tool() -> None:
    adapted = adapt_tool_definition(_registry_tool("search"))

    assert adapted == {
        "type": "function",
        "function": {
            "name": "search",
            "description": "registry tool",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }


def test_agent_rejects_unconnected_tool_call() -> None:
    engine = AgentEngine()

    def fake_ai(messages, model, context, tools=None):
        return _tool_call_response("not_connected")

    engine._ai_complete = fake_ai

    result = engine.execute("find docs", [_tool("search")], "stub/model", None, {})

    assert result["status"] == "error"
    assert "not connected" in result["result"]["error"]
    assert result["result"]["pending_tool_call"] is None


def test_runtime_profile_connected_tools_override_supplied_tool_list() -> None:
    engine = AgentEngine()

    def fake_ai(messages, model, context, tools=None):
        return _tool_call_response("loose_tool")

    engine._ai_complete = fake_ai

    result = engine.execute(
        "find docs",
        [_tool("loose_tool")],
        "stub/model",
        None,
        {
            "runtime_profile": _runtime_profile(connected_tools=["search"]),
            "agent_id": "agent",
        },
    )

    assert result["status"] == "error"
    assert result["result"]["steps"][-1]["content"]["enforced_tools"] == ["search"]


def test_compiled_defaultspack_runtime_profile_allows_actual_tool_names() -> None:
    engine = AgentEngine()

    def fake_ai(messages, model, context, tools=None):
        return _tool_call_response("web_search")

    engine._ai_complete = fake_ai

    result = engine.execute(
        "find docs",
        [_tool("web_search"), _tool("calculator")],
        "stub/model",
        None,
        {
            "runtime_profile": _compiled_bundle_runtime_profile(),
            "agent_id": "agent",
        },
    )

    assert result["status"] == "waiting_approval"
    assert result["result"]["pending_tool_call"]["tool_name"] == "web_search"


def test_runtime_profile_tool_bundle_filters_provider_tools_to_bundle_names() -> None:
    engine = AgentEngine()
    seen = {}

    def fake_ai(messages, model, context, tools=None):
        seen["tools"] = tools
        return _text_response()

    engine._ai_complete = fake_ai

    result = engine.execute(
        "find docs",
        [_tool("web_search"), _tool("calculator"), _tool("file_reader")],
        "stub/model",
        None,
        {
            "runtime_profile": _compiled_bundle_runtime_profile(
                bundle_tools=["web_search", "calculator"],
            ),
            "agent_id": "agent",
        },
    )

    assert result["status"] == "completed"
    assert [tool["function"]["name"] for tool in seen["tools"]] == [
        "web_search",
        "calculator",
    ]


def test_runtime_profile_empty_tool_bundle_does_not_fallback_to_supplied_tools() -> None:
    engine = AgentEngine()
    seen = {}
    runtime_profile = _compiled_bundle_runtime_profile(bundle_tools=[])
    tools = [_tool("read"), _tool("write")]

    def fake_ai(messages, model, context, tools=None):
        seen["tools"] = tools
        return _text_response()

    engine._ai_complete = fake_ai

    enforced = runtime_profile_enforced_tool_names(runtime_profile, "agent", tools)
    result = engine.execute(
        "read then write",
        tools,
        "stub/model",
        None,
        {
            "runtime_profile": runtime_profile,
            "agent_id": "agent",
        },
    )

    assert enforced == set()
    assert result["status"] == "completed"
    assert seen["tools"] == []


def test_agent_approve_preserves_tools_for_followup_completion() -> None:
    engine = AgentEngine()
    tools = [_tool("search")]
    seen_followup_tools = []
    seen_followup_messages = []

    def fake_ai(messages, model, context, tools=None):
        seen_followup_tools.append(tools)
        seen_followup_messages.append(list(messages))
        if len(seen_followup_tools) == 1:
            return _tool_call_response("search")
        return _text_response("used tool")

    executed = {}

    def fake_execute_tool(tool_name, tool_args, context):
        executed["tool_name"] = tool_name
        executed["tool_args"] = tool_args
        return {"status": "ok", "data": {"result": "found"}}

    engine._ai_complete = fake_ai
    engine._execute_tool = fake_execute_tool

    started = engine.execute("find docs", tools, "stub/model", None, {"principal": "test"})
    approved = engine.approve(started["execution_id"])

    assert started["status"] == "waiting_approval"
    assert approved["status"] == "completed"
    assert seen_followup_tools == [tools, tools]
    assert executed == {"tool_name": "search", "tool_args": {"q": "rumi"}}
    assert seen_followup_messages[1][-1]["role"] == "tool"
    assert seen_followup_messages[1][-1]["tool_call_id"] == "call_1"


def test_agent_yolo_auto_approves_tool_call_without_waiting() -> None:
    engine = AgentEngine()
    tools = [_tool("search")]
    seen_followup_tools = []
    executed = []

    def fake_ai(messages, model, context, tools=None):
        seen_followup_tools.append(tools)
        if len(seen_followup_tools) == 1:
            return _tool_call_response("search")
        return _text_response("used tool")

    def fake_execute_tool(tool_name, tool_args, context):
        executed.append((tool_name, tool_args, context["profile_policy"].get("yolo_mode")))
        return {"status": "ok", "data": {"result": "found"}}

    engine._ai_complete = fake_ai
    engine._execute_tool = fake_execute_tool

    result = engine.execute(
        "find docs",
        tools,
        "stub/model",
        None,
        {"profile_policy": {"yolo_mode": True}},
    )

    assert result["status"] == "completed"
    assert result["result"]["pending_tool_call"] is None
    assert seen_followup_tools == [tools, tools]
    assert executed == [("search", {"q": "rumi"}, True)]


def test_agent_approve_passes_graph_profile_principal_context_to_tool() -> None:
    engine = AgentEngine()
    seen_context = {}

    def fake_ai(messages, model, context, tools=None):
        if seen_context:
            return _text_response("used tool")
        return _tool_call_response("search")

    def fake_execute_tool(tool_name, tool_args, context):
        seen_context.update(context)
        return {"status": "ok", "data": {"result": "found"}}

    engine._ai_complete = fake_ai
    engine._execute_tool = fake_execute_tool

    started = engine.execute(
        "find docs",
        [_tool("search")],
        "stub/model",
        None,
        {
            "runtime_profile": _runtime_profile(connected_tools=["search"]),
            "agent_id": "agent",
            "graph_id": "defaultspack.coding_workspace",
            "profile_id": "defaultspack.coding",
            "principal_id": "defaultspack",
        },
    )
    approved = engine.approve(started["execution_id"])

    assert approved["status"] == "completed"
    assert seen_context["capability_graph"] == {
        "graph_id": "defaultspack.coding_workspace",
        "profile_id": "defaultspack.coding",
        "principal_id": "defaultspack",
        "tool_name": "search",
        "connected_tools": ["search"],
    }


def test_agent_rejects_tool_call_after_profile_policy_limit() -> None:
    engine = AgentEngine()
    ai_calls = []

    def fake_ai(messages, model, context, tools=None):
        ai_calls.append(True)
        return _tool_call_response("search")

    def fake_execute_tool(tool_name, tool_args, context):
        return {"status": "ok", "data": {"result": "found"}}

    engine._ai_complete = fake_ai
    engine._execute_tool = fake_execute_tool

    started = engine.execute(
        "find docs",
        [_tool("search")],
        "stub/model",
        None,
        {"profile_policy": {"max_tool_calls": 1}},
    )
    approved = engine.approve(started["execution_id"])

    assert approved["status"] == "error"
    assert approved["result"]["error"] == "max tool calls exceeded"


def test_agent_reject_preserves_tools_for_followup_completion() -> None:
    engine = AgentEngine()
    tools = ["search"]
    seen_followup_tools = []

    def fake_ai(messages, model, context, tools=None):
        seen_followup_tools.append(tools)
        if len(seen_followup_tools) == 1:
            return _tool_call_response("search")
        return _text_response("alternate answer")

    engine._ai_complete = fake_ai

    started = engine.execute("find docs", tools, "stub/model", None, {"principal": "test"})
    rejected = engine.reject(started["execution_id"], "not now")

    assert started["status"] == "waiting_approval"
    assert rejected["status"] == "completed"
    assert seen_followup_tools == [tools, tools]
