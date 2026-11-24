from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
FLOW_PATH = DEFAULTSPACK_ROOT / "flows" / "chat_turn.flow.yaml"
STREAM_FLOW_PATH = DEFAULTSPACK_ROOT / "flows" / "chat_stream_turn.flow.yaml"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))
pytestmark = pytest.mark.contract


def _flow():
    return yaml.safe_load(FLOW_PATH.read_text(encoding="utf-8"))


def _stream_flow():
    return yaml.safe_load(STREAM_FLOW_PATH.read_text(encoding="utf-8"))


def test_chat_turn_flow_has_profile_workspace_steps():
    steps = _flow()["steps"]
    ids = [step["id"] for step in steps]
    assert ids[:3] == ["load_conversation", "load_active_profile", "load_profile_workspace"]
    functions = {step["id"]: step["function"] for step in steps if step.get("type") == "function"}
    assert functions["load_conversation"] == "defaults.chat.get_conversation"
    assert functions["load_active_profile"] == "defaults.profile.load_active"
    assert functions["load_profile_workspace"] == "defaults.profile.workspace"


def test_chat_turn_flow_uses_conversation_model_and_prompt_metadata():
    steps = {step["id"]: step for step in _flow()["steps"]}

    assert steps["load_active_profile"]["input"]["profile_id"] == "{{input.profile_id || conversation.metadata.profile_id}}"
    assert steps["load_system_prompt"]["input"]["system_prompt_id"] == "{{conversation.system_prompt_id}}"
    assert steps["route_model"]["input"]["preferred_model"] == "{{conversation.model}}"


def test_chat_turn_flow_has_permission_filter_before_call_ai():
    ids = [step["id"] for step in _flow()["steps"]]
    assert ids.index("apply_permissions") < ids.index("route_model") < ids.index("call_ai")


def test_chat_turn_flow_has_persist_and_audit_steps():
    ids = [step["id"] for step in _flow()["steps"]]
    assert "persist_turn" in ids
    assert "audit" in ids
    assert "post_turn" in ids
    assert ids.index("persist_turn") < ids.index("audit")
    assert ids.index("audit") < ids.index("post_turn")


def test_chat_turn_flow_is_discoverable_by_flow_engine():
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    flow = engine.get_flow("defaultspack.chat_turn")

    assert flow is not None
    assert flow["flow_id"] == "defaultspack.chat_turn"
    assert engine.validate_flow("defaultspack.chat_turn") == []
    discovered = {item["flow_id"]: item for item in engine.list_flows()}
    assert discovered["defaultspack.chat_turn"]["declarative"] is True


def test_chat_stream_turn_flow_is_declared_for_stream_endpoint():
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    flow = engine.get_flow("defaultspack.chat_stream_turn")
    route = _stream_flow()["transport"]["http"]["routes"][0]

    assert flow is not None
    assert flow["flow_id"] == "defaultspack.chat_stream_turn"
    assert engine.validate_flow("defaultspack.chat_stream_turn") == []
    assert route["path"] == "/api/chat/conversations/{id}/stream"
    assert route["fallback_block"] == "blocks.chat.stream"
    assert _stream_flow()["result"]["value"] == "{{stream_result}}"
    assert _stream_flow()["steps"][0]["function"] == "defaults.chat.stream"


def test_chat_turn_declarative_runner_executes_function_steps(monkeypatch):
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    calls = []

    def fake_invoke(function_name, step_input, flow_context):
        calls.append((function_name, step_input))
        data_by_function = {
            "defaults.chat.get_conversation": {
                "id": "conversation-1",
                "model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "system_prompt_id": "mimo_coding_company",
                "metadata": {"profile_id": "profile-1"},
            },
            "defaults.profile.load_active": {"profile_id": "profile-1", "policy": {}},
            "defaults.profile.workspace": {"root": "/tmp/work"},
            "defaults.chat.detect_modalities": {"text": True},
            "defaults.prompt.load_effective": "system prompt",
            "defaults.tools.select_relevant": {"tools": ["search"]},
            "defaults.permissions.filter_tools": {"tools": ["search"]},
            "defaults.ai.route_model": {"bridge_required": False, "bridge_plan": {}},
            "defaults.prompt.compact_prompt": {"prompt": "compact prompt"},
            "defaults.ai.build_request": {"messages": []},
            "defaults.ai.complete": {"id": "assistant-1"},
            "defaults.chat.persist_turn": {
                "id": "turn-1",
                "assistant_message": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                },
            },
            "defaults.audit.record_event": {"id": "audit-1"},
        }
        return {"status": "ok", "data": data_by_function[function_name]}

    monkeypatch.setattr(engine, "_invoke_function_step", fake_invoke)

    result = engine.execute(
        "defaultspack.chat_turn",
        {
            "conversation_id": "conversation-1",
            "profile_id": "profile-1",
            "message": {"content": "hi"},
        },
    )

    assert result.is_success()
    assert result.metadata["runner"] == "declarative_flow_engine"
    assert calls[0] == (
        "defaults.chat.get_conversation",
        {"conversation_id": "conversation-1"},
    )
    assert calls[1] == (
        "defaults.profile.load_active",
        {"profile_id": "profile-1"},
    )
    prompt_call = next(step_input for function_name, step_input in calls if function_name == "defaults.prompt.load_effective")
    assert prompt_call["profile_id"] == "profile-1"
    assert prompt_call["conversation_id"] == "conversation-1"
    assert prompt_call["system_prompt_id"] == "mimo_coding_company"
    assert prompt_call["workspace"] == {"root": "/tmp/work"}

    route_call = next(step_input for function_name, step_input in calls if function_name == "defaults.ai.route_model")
    assert route_call["conversation_id"] == "conversation-1"
    assert route_call["profile_id"] == "profile-1"
    assert route_call["message"] == {"content": "hi"}
    assert route_call["modalities"] == {"text": True}
    assert route_call["tools"] == ["search"]
    assert route_call["preferred_model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert "defaults.vision.describe_images" not in [call[0] for call in calls]
    outputs = result.metadata["outputs"]
    assert outputs["ai_response"] == {"id": "assistant-1"}
    assert outputs["audit_event"] == {"id": "audit-1"}
    assert outputs["selected_tools"] == ["search"]
    assert result.output["data"]["role"] == "assistant"


def test_chat_turn_runs_optional_post_turn_subflow(monkeypatch):
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    engine._flows["test.post_turn"] = {
        "flow_id": "test.post_turn",
        "_declarative": True,
        "inputs": {"conversation_id": "string"},
        "outputs": {"forwarded": "object"},
        "steps": [
            {
                "id": "forward",
                "type": "function",
                "function": "test.webhook.forward",
                "input": {
                    "conversation_id": "{{input.conversation_id}}",
                    "assistant": "{{input.persisted_turn.assistant_message}}",
                },
                "output": "forwarded",
            }
        ],
    }
    calls = []

    def fake_invoke(function_name, step_input, flow_context):
        calls.append((function_name, step_input))
        if function_name == "test.webhook.forward":
            return {"status": "ok", "data": {"sent": True, "to": "line"}}
        data_by_function = {
            "defaults.chat.get_conversation": {
                "id": "conversation-1",
                "model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "system_prompt_id": "mimo_coding_company",
                "metadata": {"profile_id": "profile-1"},
            },
            "defaults.profile.load_active": {"profile_id": "profile-1", "policy": {"post_turn_flow": "test.post_turn"}},
            "defaults.profile.workspace": {"root": "/tmp/work"},
            "defaults.chat.detect_modalities": {"text": True},
            "defaults.prompt.load_effective": "system prompt",
            "defaults.tools.select_relevant": {"tools": []},
            "defaults.permissions.filter_tools": {"tools": []},
            "defaults.ai.route_model": {"bridge_required": False, "bridge_plan": {}},
            "defaults.prompt.compact_prompt": {"prompt": "system prompt"},
            "defaults.ai.build_request": {"messages": []},
            "defaults.ai.complete": {"content": [{"type": "text", "text": "hello"}]},
            "defaults.chat.persist_turn": {
                "assistant_message": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                }
            },
            "defaults.audit.record_event": {"id": "audit-1"},
        }
        return {"status": "ok", "data": data_by_function[function_name]}

    monkeypatch.setattr(engine, "_invoke_function_step", fake_invoke)

    result = engine.execute(
        "defaultspack.chat_turn",
        {
            "conversation_id": "conversation-1",
            "profile_id": "profile-1",
            "message": {"content": "hi"},
        },
    )

    assert result.is_success()
    assert calls[-1][0] == "test.webhook.forward"
    assert calls[-1][1]["assistant"]["id"] == "assistant-1"
    assert result.metadata["step_outputs"]["post_turn"]["forwarded"] == {"sent": True, "to": "line"}


def test_chat_turn_ignores_request_controlled_post_turn_subflow(monkeypatch):
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    engine._flows["test.untrusted_request_flow"] = {
        "flow_id": "test.untrusted_request_flow",
        "_declarative": True,
        "steps": [
            {
                "id": "forward",
                "type": "function",
                "function": "test.webhook.forward",
                "input": {},
                "output": "forwarded",
            }
        ],
    }
    calls = []

    def fake_invoke(function_name, step_input, flow_context):
        calls.append((function_name, step_input))
        if function_name == "test.webhook.forward":
            return {"status": "ok", "data": {"sent": True}}
        data_by_function = {
            "defaults.chat.get_conversation": {
                "id": "conversation-1",
                "model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "system_prompt_id": "mimo_coding_company",
                "metadata": {"profile_id": "profile-1"},
            },
            "defaults.profile.load_active": {"profile_id": "profile-1", "policy": {}},
            "defaults.profile.workspace": {"root": "/tmp/work"},
            "defaults.chat.detect_modalities": {"text": True},
            "defaults.prompt.load_effective": "system prompt",
            "defaults.tools.select_relevant": {"tools": []},
            "defaults.permissions.filter_tools": {"tools": []},
            "defaults.ai.route_model": {"bridge_required": False, "bridge_plan": {}},
            "defaults.prompt.compact_prompt": {"prompt": "system prompt"},
            "defaults.ai.build_request": {"messages": []},
            "defaults.ai.complete": {"content": [{"type": "text", "text": "hello"}]},
            "defaults.chat.persist_turn": {
                "assistant_message": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                }
            },
            "defaults.audit.record_event": {"id": "audit-1"},
        }
        return {"status": "ok", "data": data_by_function[function_name]}

    monkeypatch.setattr(engine, "_invoke_function_step", fake_invoke)

    result = engine.execute(
        "defaultspack.chat_turn",
        {
            "conversation_id": "conversation-1",
            "profile_id": "profile-1",
            "message": {"content": "hi"},
            "post_turn_flow": "test.untrusted_request_flow",
        },
    )

    assert result.is_success()
    assert "test.webhook.forward" not in [call[0] for call in calls]


def test_declarative_flow_rejects_legacy_persist_step_type():
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    engine._flows["test.bad_persist"] = {
        "flow_id": "test.bad_persist",
        "_declarative": True,
        "steps": [{"id": "persist", "type": "persist"}],
    }

    errors = engine.validate_flow("test.bad_persist")

    assert any("unsupported type 'persist'" in item for item in errors)


def test_declarative_flow_condition_treats_falsey_strings_as_false():
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    values = {
        "input": {
            "false_string": "false",
            "zero_string": "0",
            "off_string": "off",
            "none_string": "none",
            "true_string": "yes",
        }
    }

    assert engine._condition_matches("false", values) is False
    assert engine._condition_matches("0", values) is False
    assert engine._condition_matches("no", values) is False
    assert engine._condition_matches("off", values) is False
    assert engine._condition_matches("null", values) is False
    assert engine._condition_matches("none", values) is False
    assert engine._condition_matches("{{input.false_string}}", values) is False
    assert engine._condition_matches("{{input.zero_string}}", values) is False
    assert engine._condition_matches("{{input.off_string}}", values) is False
    assert engine._condition_matches("{{input.none_string}}", values) is False
    assert engine._condition_matches("{{input.true_string}}", values) is True
    assert engine._condition_matches(None, values) is True


def test_declarative_flow_executes_branch_and_parallel_steps(monkeypatch):
    from ecosystem.defaultspack.domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    engine._flows["test.branch_parallel"] = {
        "flow_id": "test.branch_parallel",
        "_declarative": True,
        "steps": [
            {
                "id": "choose",
                "type": "branch",
                "branches": [
                    {
                        "when": "{{input.enabled}}",
                        "steps": [
                            {
                                "id": "branch_fn",
                                "type": "function",
                                "function": "test.branch",
                                "input": {"value": "{{input.value}}"},
                                "output": "branch_value",
                            }
                        ],
                    }
                ],
                "output": "branch_result",
            },
            {
                "id": "fanout",
                "type": "parallel",
                "steps": [
                    {
                        "id": "left",
                        "type": "function",
                        "function": "test.left",
                        "input": {"value": "{{input.value}}"},
                        "output": "left_value",
                    },
                    {
                        "id": "right",
                        "type": "function",
                        "function": "test.right",
                        "input": {"value": "{{input.value}}"},
                        "output": "right_value",
                    },
                ],
                "output": "parallel_result",
            },
        ],
    }

    def fake_invoke(function_name, step_input, flow_context):
        return {"status": "ok", "data": {"function": function_name, "input": step_input}}

    monkeypatch.setattr(engine, "_invoke_function_step", fake_invoke)

    assert engine.validate_flow("test.branch_parallel") == []
    result = engine.execute(
        "test.branch_parallel",
        {"enabled": True, "value": "ok"},
    )

    assert result.is_success()
    outputs = result.output["data"]["outputs"]
    assert outputs["branch_result"]["outputs"]["branch_value"]["function"] == "test.branch"
    assert outputs["parallel_result"]["left"]["left_value"]["function"] == "test.left"
    assert outputs["parallel_result"]["right"]["right_value"]["function"] == "test.right"


def test_persist_turn_writes_canonical_chat_store_messages(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CHAT_STORE_PATH",
        str(tmp_path / "chat" / "conversations.json"),
    )
    from domain.chat import store as facade
    from domain.chat.store import ChatStore
    from ecosystem.defaultspack.blocks.chat.persist_turn import run
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )

    owner = ConversationStore("default", user_data_root=tmp_path)

    def invoke(contract_id: str, operation: str, payload: dict[str, Any]) -> Any:
        if contract_id == facade.CONVERSATION:
            if operation == "list":
                return owner.snapshot()
            if operation == "get":
                return owner.get(str(payload.get("conversation_id") or ""))
        if contract_id == facade.CONVERSATION_MANAGE and operation == "create":
            return owner.create(
                payload["conversation"],
                expected_revision=int(payload["expected_revision"]),
            )
        if contract_id == facade.MESSAGE_MANAGE and operation == "append":
            return owner.append_message(
                str(payload["conversation_id"]),
                payload["message"],
                expected_conversation_revision=int(
                    payload["expected_conversation_revision"]
                ),
            )
        raise AssertionError(f"unexpected contract call: {contract_id}/{operation}")

    monkeypatch.setattr(facade, "_invoke", invoke)
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello"},
            "ai_response": {
                "content": [{"type": "text", "text": "hi back"}],
                "finish_reason": "stop",
                "usage": {"total_tokens": 3},
            },
            "route_model": {"selected_model": "stub/default"},
            "workspace": {"user_data_dir": str(tmp_path / "audit")},
        },
        {},
    )

    assert result["status"] == "ok"
    persisted = result["data"]
    assert persisted["user_message"]["role"] == "user"
    assert persisted["assistant_message"]["role"] == "assistant"
    assert persisted["assistant_message"]["raw_text"] == "hi back"
    stored = ChatStore().get_conversation(conversation["id"])
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    assert (tmp_path / "audit" / "chat_turns.jsonl").is_file()
