from __future__ import annotations

import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures(
    "defaultspack_conversation_owner", "defaultspack_v4_tool_dispatch"
)


def test_frontend_request_detector_uses_prompt_and_path_hints() -> None:
    from domain.coding.frontend_precision import detect_frontend_request

    prompt = detect_frontend_request("AI chat app のfrontendを作って")
    path = detect_frontend_request("fix this", files=["src/components/Composer.tsx"])
    command = detect_frontend_request("普通の修正", command="/frontend audit")
    boring = detect_frontend_request("pytestの失敗を直して")

    assert prompt.enabled
    assert any("frontend" in reason or "chat app" in reason for reason in prompt.reasons)
    assert path.enabled
    assert path.reasons == ["frontend file extension: src/components/Composer.tsx"]
    assert command.enabled
    assert command.mode == "audit"
    assert command.explicit
    assert not boring.enabled


def test_frontend_request_detector_does_not_match_ui_inside_common_words() -> None:
    from domain.coding.frontend_precision import detect_frontend_request

    for prompt in [
        "build the backend cache",
        "quick fix for pytest",
        "write a guide for setup",
        "run the regression suite",
    ]:
        detected = detect_frontend_request(prompt)
        assert not detected.enabled, detected.to_dict()


def test_frontend_request_detector_matches_app_without_path_style_false_positives() -> None:
    from domain.coding.frontend_precision import detect_frontend_request

    assert detect_frontend_request("build an app").enabled
    assert not detect_frontend_request("fix docs", files=["docs/lifestyle.md"]).enabled
    assert not detect_frontend_request("fix parser", files=["src/freestyle_parser.py"]).enabled


def test_frontend_request_detector_ignores_ordinary_chat_with_ui_words() -> None:
    from domain.coding.frontend_precision import detect_frontend_request

    for prompt in [
        "what app should I use?",
        "explain this page",
    ]:
        detected = detect_frontend_request(prompt)
        assert not detected.enabled, detected.to_dict()
        assert any(reason.startswith("prompt keyword:") for reason in detected.reasons)


def test_prepare_chat_run_does_not_attach_frontend_precision_for_ordinary_chat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    for prompt in [
        "what app should I use?",
        "explain this page",
    ]:
        ChatStore._instance = None
        store = ChatStore()
        conversation = store.create_conversation(model="stub/default")
        prepared = prepare_chat_run(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": prompt},
                "params": {"tool_selection": {"mode": "none"}},
            },
            {"workspace_root": str(tmp_path)},
        )

        assert "frontend_precision" not in prepared.tool_context
        assert "frontend_precision" not in prepared.request_context
        assert "tool_ui_build_recursive" not in prepared.tools_called
        assert "tool_ui_build_recursive" not in prepared.connected_tool_names
    ChatStore._instance = None


def test_frontend_default_ui_trees_are_planner_executable() -> None:
    from domain.coding.frontend_precision import build_default_ui_tree
    from domain.ui_compiler.planner import RecursiveUIPlanner

    for task in ["AI chat app frontend", "dashboard frontend", "form frontend"]:
        plan = RecursiveUIPlanner().plan(build_default_ui_tree(task), run_id="frontend-default-tree")
        assert plan.is_executable(), plan.to_dict()["diagnostics"]


def test_coding_session_frontend_request_promotes_to_precision_specialists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))

    from blocks.agent.coding_session_create import run as create_session
    from domain.coding.workspace_store import WorkspaceStore

    WorkspaceStore().create(tmp_path, workspace_id="trusted", trusted=True)
    created = create_session(
        {
            "task": "AI chat app frontend を細かく作って",
            "workspace_id": "trusted",
            "agents": [{"name": "worker", "role": "coding worker", "model": "stub/default", "tools": []}],
        },
        {},
    )

    assert created["status"] == "ok"
    precision = created["data"]["frontend_precision"]
    agents = created["data"]["session"]["agents"]
    assert precision["enabled"] is True
    assert precision["mode"] == "strict"
    assert len(agents) >= 12
    assert {agent["name"] for agent in agents} >= {
        "product-intent",
        "typography",
        "color-system",
        "page-topology",
        "semantic-region-planner",
        "text-pressure-auditor",
        "composition",
    }
    assert created["data"]["session"]["shared_context"]["frontend_precision"]["requiredTool"] == "tool_ui_build_recursive"


def test_coding_session_frontend_slash_command_promotes_to_precision() -> None:
    from domain.coding.frontend_precision import promote_coding_session_input

    promoted, precision = promote_coding_session_input({"task": "監査だけして", "command": "/frontend audit"})

    assert precision["enabled"] is True
    assert precision["mode"] == "audit"
    assert promoted["frontend_precision"]["command"] == "frontend"
    assert promoted["task"].startswith("[Rumi frontend precision mode: audit]")


def test_frontend_slash_command_is_registered_and_returns_precision_payload() -> None:
    from domain.frontend.command_registry import SlashCommandRegistry

    registry = SlashCommandRegistry()
    commands = {command["id"]: command for command in registry.list_commands()}
    result = registry.execute({"command": "frontend", "mode": "coding", "args": {"mode": "refine"}}, {})

    assert "frontend" in commands
    assert result["status"] == "ok"
    assert result["data"]["executed"] is True
    assert result["data"]["result"]["mode"] == "refine"
    assert result["data"]["result"]["requiredTool"] == "tool_ui_build_recursive"


def test_prepare_chat_run_forces_ui_compiler_even_when_tools_are_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "dashboard frontend を実装して"},
            "params": {"tool_selection": {"mode": "none"}},
        },
        {
            "workspace_root": str(tmp_path),
            "principal_capabilities": ["developer"],
        },
    )

    assert prepared.tool_context["frontend_precision"]["enabled"] is True
    assert "tool_ui_build_recursive" in prepared.tools_called
    assert "tool_ui_build_recursive" in prepared.connected_tool_names
    assert prepared.request_context["tool_selection"]["must_use"] is True
    ChatStore._instance = None


def test_stream_engine_executes_frontend_precision_before_model_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    captured: dict[str, object] = {}

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["approved"] = bool(prepared.tool_context.get("_tool_server_approved"))
        self._tool_logs.append({"tool_name": tool_name, "tool_call_id": tool_call_id, "arguments": arguments, "result": {}})
        return {
            "status": "ok",
            "data": {
                "status": "ok",
                "data": {
                    "runId": arguments["run_id"],
                    "summary": {"acceptedBundles": 3, "buildStatus": "passed"},
                    "report": ".rumi/ui/runs/{}/reports/final.json".format(arguments["run_id"]),
                },
            },
        }

    def fake_model_turn(self, prepared, messages, draft):
        captured["model_messages"] = list(messages)
        captured["provider_tools_after_preflight"] = list(prepared.provider_tools or [])
        return {"content": [{"type": "text", "text": "precision done"}], "finish_reason": "stop", "usage": {}}, []

    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    events = list(
        ChatRunEngine(store=store).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "AI chat app frontend を作って"},
                "params": {"tool_selection": {"mode": "none"}},
            },
            {
                "workspace_root": str(tmp_path),
                "profile_policy": {"yolo_mode": True},
                "principal_capabilities": ["developer"],
            },
        )
    )

    assert captured["tool_name"] == "tool_ui_build_recursive"
    assert captured["approved"] is True
    assert captured["arguments"]["options"]["viewports"] == [390, 768, 1440]
    assert captured["arguments"]["options"]["applyToProject"] is True
    assert any(message.get("role") == "tool" for message in captured["model_messages"])
    assert captured["provider_tools_after_preflight"]
    flat_events = [item for event in events for item in (event if isinstance(event, list) else [event])]
    completed = [event for event in flat_events if event.get("type") == "assistant_message_completed"]
    metadata = completed[-1]["data"]["message"]["metadata"]
    assert metadata["frontend_precision"]["executed"]["report"].endswith("/reports/final.json")
    assert "tool_ui_build_recursive" in metadata["executed_tools"]
    ChatStore._instance = None


def test_stream_engine_does_not_preexecute_frontend_precision_for_ordinary_chat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    captured: dict[str, object] = {}

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        raise AssertionError("ordinary chat must not pre-execute frontend precision")

    def fake_model_turn(self, prepared, messages, draft):
        captured["model_turn"] = True
        captured["frontend_precision"] = prepared.tool_context.get("frontend_precision")
        captured["provider_tools"] = list(prepared.provider_tools or [])
        return {"content": [{"type": "text", "text": "ordinary response"}], "finish_reason": "stop", "usage": {}}, []

    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    events = list(
        ChatRunEngine(store=store).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "explain this page"},
                "params": {"tool_selection": {"mode": "none"}},
            },
            {"workspace_root": str(tmp_path), "profile_policy": {"yolo_mode": True}},
        )
    )

    assert captured["model_turn"] is True
    assert captured["frontend_precision"] is None
    assert all(tool.get("name") != "tool_ui_build_recursive" for tool in captured["provider_tools"])
    flat_events = [item for event in events for item in (event if isinstance(event, list) else [event])]
    assert not any(event.get("frontend_precision") for event in flat_events)
    assert not any(event.get("tool_name") == "tool_ui_build_recursive" for event in flat_events)
    ChatStore._instance = None


def test_stream_engine_frontend_precision_requires_approval_without_trusted_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    captured: dict[str, object] = {}

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        captured["approved"] = bool(prepared.tool_context.get("_tool_server_approved"))
        return {
            "status": "ok",
            "data": {
                "approval_required": True,
                "requires_approval": True,
                "operation": tool_name,
                "arguments": arguments,
            },
        }

    def fake_model_turn(self, prepared, messages, draft):
        raise AssertionError("model turn must not run while frontend precision awaits approval")

    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    events = list(
        ChatRunEngine(store=store).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "AI chat app frontend を作って"},
                "params": {"tool_selection": {"mode": "none"}},
            },
            {"workspace_root": str(tmp_path)},
        )
    )

    assert captured["approved"] is False
    flat_events = [item for event in events for item in (event if isinstance(event, list) else [event])]
    assert any(event.get("type") == "approval_requested" for event in flat_events)
    completed = [event for event in flat_events if event.get("type") == "assistant_message_completed"]
    assert completed[-1]["data"]["message"]["finish_reason"] == "approval_required"
    ChatStore._instance = None


def test_stream_engine_frontend_precision_does_not_trust_spoofed_approval_flags(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    captured: dict[str, object] = {}

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        captured["approved"] = bool(prepared.tool_context.get("_tool_server_approved"))
        return {
            "status": "ok",
            "data": {
                "approval_required": True,
                "requires_approval": True,
                "operation": tool_name,
                "arguments": arguments,
            },
        }

    def fake_model_turn(self, prepared, messages, draft):
        raise AssertionError("model turn must not run while spoofed approval awaits real approval")

    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    events = list(
        ChatRunEngine(store=store).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "AI chat app frontend を作って"},
                "params": {"tool_selection": {"mode": "none"}},
            },
            {
                "workspace_root": str(tmp_path),
                "coding_session": {"approved": True, "trusted": True},
                "approved_coding_session": True,
                "trusted_local_context": True,
            },
        )
    )

    assert captured["approved"] is False
    flat_events = [item for event in events for item in (event if isinstance(event, list) else [event])]
    assert any(event.get("type") == "approval_requested" for event in flat_events)
    ChatStore._instance = None


def test_stream_engine_frontend_precision_failure_blocks_model_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))

    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        return {"status": "error", "error": {"message": "strict browserRender failed"}}

    def fake_model_turn(self, prepared, messages, draft):
        raise AssertionError("model turn must not run after frontend precision hard gate failure")

    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    events = list(
        ChatRunEngine(store=store).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "AI chat app frontend を作って"},
                "params": {"tool_selection": {"mode": "none"}},
            },
            {"workspace_root": str(tmp_path), "profile_policy": {"yolo_mode": True}},
        )
    )

    flat_events = [item for event in events for item in (event if isinstance(event, list) else [event])]
    assert any(event.get("phase") == "frontend_precision_failed" for event in flat_events)
    completed = [event for event in flat_events if event.get("type") == "assistant_message_completed"]
    assert completed[-1]["data"]["message"]["finish_reason"] == "error"
    assert completed[-1]["data"]["message"]["metadata"]["frontend_precision"]["executed"]["status"] == "failed"
    ChatStore._instance = None
