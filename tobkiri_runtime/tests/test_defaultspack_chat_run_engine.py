from __future__ import annotations

import json
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")


def _write_v2_skill(skill_dir, *, skill_id, display_name, trigger, instruction):
    """Write the smallest valid v2 skill fixture with a trusted SKILL.md."""
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.tobkiri.dev/skill/v2.json",
                "schema_version": "tobkiri.skill/v2",
                "kind": "skill",
                "category": "skill",
                "id": skill_id,
                "version": "2.0.0",
                "enabled": True,
                "display_name": display_name,
                "description": display_name,
                "instructions": {
                    "path": "SKILL.md",
                    "format": "agent-skills",
                    "max_tokens": 800,
                },
                "activation": {
                    "mode": "auto_or_explicit",
                    "aliases": [skill_id.rsplit("/", 1)[-1]],
                    "positive_examples": [trigger],
                    "negative_examples": [],
                },
                "scope": {"activity_ids": [], "tool_ids": []},
                "composition": {
                    "class": "optional",
                    "priority": 100,
                    "requires": [],
                    "conflicts_with": [],
                },
                "tool_policy": {
                    "allowed_tool_ids": [],
                    "denied_tool_ids": [],
                },
                "security": {
                    "minimum_trust": "verified",
                    "may_grant_permissions": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(instruction, encoding="utf-8")


def _inject_test_extension_roots(monkeypatch, *extra_roots: Path) -> None:
    """Inject temporary roots through the explicit test-only builder seam."""
    from domain.extensions import runtime as extension_runtime

    roots = tuple(extra_roots)
    monkeypatch.setattr(
        extension_runtime,
        "get_extensions_roots",
        lambda: extension_runtime.build_extensions_roots(
            DEFAULTSPACK_ROOT,
            extra_roots=roots,
        ),
    )
    extension_runtime.get_extension_registry(force_reload=True)


def test_computer_use_action_suffix_tool_name_is_normalized():
    from domain.chat.stream_engine import _normalize_tool_call_name_and_arguments

    tool_name, arguments = _normalize_tool_call_name_and_arguments(
        "computer_use:open_url",
        {"url": "https://www.google.com", "app": "Google Chrome"},
    )

    assert tool_name == "computer_use"
    assert arguments == {
        "action": "open_url",
        "url": "https://www.google.com",
        "app": "Google Chrome",
    }


def test_display_desktop_frame_tool_name_is_normalized():
    from domain.chat.stream_engine import _normalize_tool_call_name_and_arguments

    tool_name, arguments = _normalize_tool_call_name_and_arguments(
        "Desktop Frame",
        {"seat_id": "seat-1"},
    )

    assert tool_name == "desktop_frame"
    assert arguments == {"seat_id": "seat-1"}


def test_chat_run_engine_has_no_default_four_tool_call_limit(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    model_turns = {"count": 0}
    executed = []

    def fake_model_turn(self, prepared, messages, draft):
        if False:
            yield {}
        model_turns["count"] += 1
        if model_turns["count"] <= 5:
            return (
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"call-{model_turns['count']}",
                            "name": "lookup",
                            "input": {"path": f"file-{model_turns['count']}.txt"},
                        }
                    ],
                    "finish_reason": "tool_calls",
                    "usage": {},
                },
                [
                    {
                        "type": "tool_use",
                        "id": f"call-{model_turns['count']}",
                        "name": "lookup",
                        "input": {"path": f"file-{model_turns['count']}.txt"},
                    }
                ],
            )
        return {"content": [{"type": "text", "text": "done"}], "finish_reason": "stop", "usage": {}}, []

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        executed.append((tool_name, dict(arguments)))
        return {"status": "ok", "data": {"content": arguments.get("path")}}

    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)
    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)

    engine = ChatRunEngine()
    events = list(
        engine.stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "read several files"},
                "tools": [{"name": "lookup", "description": "lookup"}],
                "params": {},
            },
            {},
        )
    )

    assert len(executed) == 5
    assert not any(event.get("phase") == "tool_call_limit" for event in events)
    ChatStore._instance = None


def test_send_and_stream_wrappers_consume_same_engine_final_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run as send_run
    from blocks.chat.stream import run as stream_run
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    final_message = {
        "id": "assistant-1",
        "role": "assistant",
        "content": [{"type": "text", "text": "shared final"}],
        "raw_text": "shared final",
        "created_at": 1,
        "conversation_id": conversation["id"],
    }

    def fake_stream(self, input_data, context, *, stream_mode=True):
        yield {
            "type": "assistant_message_completed",
            "data": {"message": final_message},
        }
        yield {
            "type": "done",
            "data": {"message": final_message},
        }

    monkeypatch.setattr(ChatRunEngine, "stream", fake_stream)

    send_result = send_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello"},
            "tools": [],
        },
        {},
    )
    stream_result = stream_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello"},
            "tools": [],
        },
        {},
    )

    assert send_result["status"] == "ok"
    assert send_result["data"]["raw_text"] == "shared final"
    stream_events = list(stream_result["events"])
    assert stream_events[-2]["type"] == "message"
    assert stream_events[-1]["type"] == "done"
    assert stream_events[-1]["message"]["raw_text"] == "shared final"
    ChatStore._instance = None


def test_send_wrapper_returns_cancelled_final_when_nonstream_run_is_cancelled(tmp_path, monkeypatch):
    from blocks.chat.send import run as send_run
    from domain.chat.store import ChatStore
    import domain.chat.stream_engine as engine_module
    import domain.chat.run_request as run_request_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    class RuntimeSettingsStub:
        def get_settings(self):
            return {"deepthink_enabled": False}

        def get_effective_thinking_level(self, **kwargs):
            return {"level": "medium"}

    class RoutingDecisionStub:
        selected_model = "stub/default"
        bridge_required = False
        bridge_plan = {}

        def to_dict(self):
            return {
                "selected_model": self.selected_model,
                "selected_group": "default",
                "reason_codes": ["test_stub"],
                "warnings": [],
                "bridge_required": False,
                "bridge_plan": {},
                "utility_models": {},
                "explanation": "test stub",
            }

    def fake_execute(self, prepared, draft):
        if False:
            yield {}
        raise engine_module._ChatCancelled()

    monkeypatch.setattr(run_request_module, "ModelRuntimeSettingsService", RuntimeSettingsStub)
    monkeypatch.setattr(run_request_module, "route_model_request", lambda request: RoutingDecisionStub())
    monkeypatch.setattr(run_request_module, "get_model_capabilities", lambda model: {"supports_thinking": True})
    monkeypatch.setattr(engine_module.ChatRunEngine, "_execute", fake_execute)

    result = send_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "stop this run"},
            "tools": [],
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["finish_reason"] == "cancelled"
    assert result["data"]["metadata"]["cancelled"] is True
    assert result["data"]["metadata"]["thinking"]["state"] == "cancelled"

    saved = ChatStore().get_conversation(conversation["id"])
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][-1]["id"] == result["data"]["id"]
    ChatStore._instance = None


def test_chat_send_and_stream_wrappers_write_inspector_logs(tmp_path, monkeypatch):
    from blocks.chat.send import run as send_run
    import blocks.chat.stream as stream_module
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.dev.inspector import Inspector

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    Inspector().clear()

    class FakeClient:
        def complete(self, model, messages, tools=None, params=None):
            return {
                "content": [{"type": "text", "text": "hello"}],
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            yield {"type": "content_delta", "delta": {"type": "text", "text": "hello"}}
            yield {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(engine_module, "AIClient", FakeClient)
    monkeypatch.setattr(stream_module, "AIClient", FakeClient)

    store = ChatStore()
    send_conversation = store.create_conversation(model="stub/default")
    send_result = send_run(
        {
            "conversation_id": send_conversation["id"],
            "message": {"role": "user", "content": "send hello"},
            "tools": [],
        },
        {},
    )
    assert send_result["status"] == "ok"
    send_log = Inspector().get_latest()
    assert send_log["conversation_id"] == send_conversation["id"]
    assert send_log["context_info"]["source"] == "blocks.chat.send"
    assert send_log["context_info"]["knowledge_results"] == []
    assert send_log["context_info"]["memory_results"] == []

    stream_conversation = store.create_conversation(model="stub/default")
    stream_result = stream_module.run(
        {
            "conversation_id": stream_conversation["id"],
            "message": {"role": "user", "content": "stream hello"},
            "tools": [],
        },
        {},
    )
    events = list(stream_result["events"])
    assert events[-1]["type"] == "done"
    stream_log = Inspector().get_latest()
    assert stream_log["conversation_id"] == stream_conversation["id"]
    assert stream_log["context_info"]["source"] == "blocks.chat.stream"
    assert stream_log["context_info"]["message_count"] >= 1
    ChatStore._instance = None


def test_prepare_chat_run_current_turn_history_mode_excludes_old_tool_logs(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    old_user = store.add_message(conversation["id"], {"role": "user", "content": "old external request"})
    store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "parent_id": old_user["id"],
            "content": [{"type": "text", "text": "old failed reply"}],
            "tool_logs": [{"tool_name": "browser_computer", "result": {"large": "x" * 5000}}],
        },
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "fresh external request"},
            "tools": [],
        },
        {"external_chat_history_mode": "current_turn"},
    )
    combined = "\n".join(str(message.get("content") or "") for message in prepared.standard_messages)

    assert "fresh external request" in combined
    assert "old external request" not in combined
    assert "old failed reply" not in combined
    ChatStore._instance = None


def test_prepare_chat_run_allows_explicit_model_override(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="gitlawb-opengateway/mimo-v2-omni")

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "scheduled reminder"},
            "params": {"model": "google/gemini-2.5-flash"},
            "tools": [],
        },
        {"run_source": "scheduler"},
    )

    assert prepared.model == "google/gemini-2.5-flash"
    assert prepared.request_context["model"] == "google/gemini-2.5-flash"
    ChatStore._instance = None


def test_prepare_chat_run_forwards_approval_followup_token_to_tool_context(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "ユーザーが許可しました。承認済みの操作を続行してください。",
                "metadata": {
                    "approval_followup": {
                        "approval_token": "tok_approved",
                        "operation": "tool.coding_file_create",
                        "request_id": "apr_1",
                        "tool_name": "coding_file_create",
                    },
                },
            },
            "tools": [],
        },
        {},
    )

    expected = {
        "coding_file_create": "tok_approved",
        "tool.coding_file_create": "tok_approved",
        "apr_1": "tok_approved",
    }
    assert prepared.request_context["tool_approval_tokens"] == expected
    assert prepared.tool_context["tool_approval_tokens"] == expected
    ChatStore._instance = None

def test_prepare_chat_run_promotes_profile_and_agent_ids_into_tool_context(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "scheduled review",
                "metadata": {
                    "profile_id": "defaultspack.mimo_coding_company",
                    "agent_id": "project_manager",
                },
            },
            "params": {
                "tool_policy": {
                    "profile_id": "defaultspack.mimo_coding_company",
                    "tool_choice": "auto",
                }
            },
            "tools": ["todo"],
        },
        {"run_source": "scheduler"},
    )

    assert prepared.request_context["profile_id"] == "defaults"
    assert prepared.tool_context["profile_id"] == "defaults"
    assert prepared.request_context["agent_id"] == "project_manager"
    assert prepared.tool_context["agent_id"] == "project_manager"
    ChatStore._instance = None


def test_prepare_chat_run_maps_computer_approval_followup_aliases(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "ユーザーが許可しました。承認済みの操作を続行してください。",
                "metadata": {
                    "approval_followup": {
                        "approval_token": "tok_browser",
                        "action": "computer.apps",
                        "operation": "computer.apps",
                        "request_id": "apr_browser_1",
                        "tool_name": "computer_use",
                    },
                },
            },
            "tools": [],
        },
        {},
    )

    expected = {
        "computer_use": "tok_browser",
        "browser_use": "tok_browser",
        "browser_computer": "tok_browser",
        "computer.apps": "tok_browser",
        "apr_browser_1": "tok_browser",
    }
    assert prepared.request_context["tool_approval_tokens"] == expected
    assert prepared.tool_context["tool_approval_tokens"] == expected
    ChatStore._instance = None


def test_approval_followup_executes_exact_payload_before_model_turn(tmp_path, monkeypatch):
    from domain.chat.stream_engine import ChatRunEngine
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    captured = {}

    def fake_execute_tool(self, prepared, tool_name, tool_call_id, arguments):
        captured["tool_name"] = tool_name
        captured["tool_call_id"] = tool_call_id
        captured["arguments"] = dict(arguments)
        captured["approval_tokens"] = dict(prepared.tool_context.get("tool_approval_tokens") or {})
        return {"status": "ok", "data": {"action": arguments.get("action"), "executed": True}}

    def fake_model_turn(self, prepared, messages, draft):
        captured["model_messages"] = list(messages)
        return {"content": [{"type": "text", "text": "done"}], "finish_reason": "stop", "usage": {}}, []

    monkeypatch.setattr(ChatRunEngine, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    engine = ChatRunEngine()
    events = list(engine.stream(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "ユーザーが許可しました。承認済みの操作を続行してください。",
                "metadata": {
                    "approval_followup": {
                        "approval_token": "tok_followup",
                        "action": "computer.click",
                        "operation": "computer.click",
                        "payload": {"action": "click", "x": 10, "y": 20},
                        "request_id": "apr_followup",
                        "tool_call_id": "call_original",
                        "tool_name": "computer_use",
                    },
                },
            },
            "tools": ["computer_use"],
            "params": {"max_tool_calls": 2},
        },
        {},
    ))

    assert captured["tool_name"] == "computer_use"
    assert captured["tool_call_id"] == "call_original"
    assert captured["arguments"] == {"action": "click", "x": 10, "y": 20}
    assert captured["approval_tokens"]["computer.click"] == "tok_followup"
    assert captured["approval_tokens"]["apr_followup"] == "tok_followup"
    assert "tok_followup" not in json.dumps(captured["model_messages"], ensure_ascii=False)
    assert any(message.get("role") == "tool" for message in captured["model_messages"])
    assert any(event.get("type") == "tool_call_completed" for event in events)
    ChatStore._instance = None


def test_approval_request_payload_preserves_original_tool_arguments():
    from domain.chat.stream_engine import _approval_request_from_tool_result

    request = _approval_request_from_tool_result(
        "computer_use",
        "call_1",
        {"action": "click", "x": 10, "y": 10},
        {
            "status": "ok",
            "data": {
                "widget": {
                    "type": "approval_request",
                    "requires_approval": True,
                    "action": "computer.click",
                    "operation": "computer.click",
                    "payload": {"action": "computer.click", "args_hash": "server-bound"},
                    "approval_request_id": "apr_1",
                },
            },
        },
    )

    assert request is not None
    assert request["payload"] == {"action": "click", "x": 10, "y": 10}
    assert request["operation"] == "computer.click"
def test_prepare_chat_run_injects_matched_skill_and_chat_references(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    extensions_root = tmp_path / "extensions"
    skill_dir = extensions_root / "skills" / "line-mention"
    _write_v2_skill(
        skill_dir,
        skill_id="feedback/line-mention",
        display_name="LINE mention skill",
        trigger="LINE",
        instruction="For LINE group chats, respond only when Rumi is mentioned.",
    )
    unrelated_skill_dir = extensions_root / "skills" / "finance-only"
    _write_v2_skill(
        unrelated_skill_dir,
        skill_id="feedback/finance-only",
        display_name="Finance only",
        trigger="portfolio-rebalance",
        instruction="This must not appear in unrelated LINE prompts.",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    _inject_test_extension_roots(monkeypatch, extensions_root)
    ChatStore._instance = None

    store = ChatStore()
    reference = store.create_conversation(model="stub/default")
    store.update_conversation(reference["id"], {"title": "Reference planning chat"})
    store.add_message(reference["id"], {"role": "user", "content": "We decided the rollout should avoid marker-based tests."})
    store.add_message(reference["id"], {"role": "assistant", "content": "Use tool_logs and metadata as evidence instead."})
    conversation = store.create_conversation(model="stub/default")
    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "LINE mention behavior please",
                "metadata": {
                    "dropped_widgets": [
                        {
                            "id": "conversation:" + reference["id"],
                            "type": "conversation",
                            "label": "Reference planning chat",
                            "sourceItemId": reference["id"],
                            "metadata": {
                                "conversation_id": reference["id"],
                                "title": "Reference planning chat",
                            },
                        }
                    ]
                },
            },
            "tools": [],
        },
        {},
    )
    combined = "\n".join(str(message.get("content") or "") for message in prepared.standard_messages)

    assert "active system-level instructions" in combined
    assert "For LINE group chats" in combined
    assert "portfolio-rebalance" not in combined
    assert prepared.matched_skills[0]["id"] == "feedback/line-mention"
    assert prepared.chat_references["history_json_path"].endswith("history.json")
    assert prepared.chat_references["references"][0]["conversation_id"] == reference["id"]
    assert prepared.chat_references["references"][0]["title"] == "Reference planning chat"
    assert "avoid marker-based tests" in prepared.chat_references["references"][0]["summary"]
    assert "Dropped Chat References" in combined
    assert reference["id"] in combined
    assert prepared.request_context["chat_references"] == prepared.chat_references
    assert prepared.tool_context["history_json_path"] == prepared.chat_references["history_json_path"]
    stored_user = ChatStore().get_message(conversation["id"], prepared.user_message["id"])
    assert stored_user["metadata"]["dropped_widgets"][0]["sourceItemId"] == reference["id"]
    assert stored_user["metadata"]["chat_references"]["history_json_path"] == prepared.chat_references["history_json_path"]
    assert stored_user["metadata"]["chat_references"]["references"][0]["conversation_id"] == reference["id"]
    ChatStore._instance = None


def test_prepare_chat_run_leaves_unmatched_skills_out_of_system_context(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    extensions_root = tmp_path / "extensions"
    skill_dir = extensions_root / "skills" / "line-mention"
    _write_v2_skill(
        skill_dir,
        skill_id="feedback/line-mention",
        display_name="LINE mention skill",
        trigger="LINE",
        instruction="For LINE group chats, respond only when Rumi is mentioned.",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    _inject_test_extension_roots(monkeypatch, extensions_root)
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "Summarize the local project notes."},
            "tools": [],
        },
        {},
    )
    combined = "\n".join(str(message.get("content") or "") for message in prepared.standard_messages)

    assert prepared.matched_skills == []
    assert "For LINE group chats" not in combined
    assert "matched_skill_instructions" not in prepared.request_context
    assert "matched_skill_instructions" not in prepared.tool_context
    ChatStore._instance = None


def test_complete_turn_retries_transient_ai_error_after_tool_use():
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def complete(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Google API error 500: Internal error encountered.")
            return {
                "content": [{"type": "text", "text": "continued"}],
                "finish_reason": "stop",
            }

    client = FlakyClient()
    engine = ChatRunEngine(store=object(), client=client)
    engine._tool_logs = [{"tool_name": "browser_computer", "result": {"status": "ok"}}]
    prepared = PreparedChatRun(
        conversation_id="conv-1",
        conversation={"id": "conv-1"},
        input_data={},
        request_id="req-1",
        content=[],
        metadata=None,
        user_message={"id": "user-1"},
        model="google/gemma-4-31b-it",
        params={"retry": {"max_attempts": 2, "delays": [0]}},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="hello",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[],
        tools_called=[],
        connected_tool_names=set(),
        call_handler=None,
        model_routing={},
    )

    response = engine._complete_turn(prepared, [{"role": "user", "content": "hello"}])

    assert client.calls == 2
    assert response["content"] == [{"type": "text", "text": "continued"}]
    assert any(event.get("type") == "ai_retry_scheduled" for event in engine._activity_events)


def test_complete_turn_retries_wrapped_429_after_tool_use():
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def complete(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    'OpenAI API error 400: {"error":{"code":"429","message":"Cluster rate limit exceeded, request queued but not admitted","param":"","type":"router_queue_limitation"}}'
                )
            return {
                "content": [{"type": "text", "text": "continued after wrapped 429"}],
                "finish_reason": "stop",
            }

    client = FlakyClient()
    engine = ChatRunEngine(store=object(), client=client)
    engine._tool_logs = [{"tool_name": "coding_file_read", "result": {"status": "ok"}}]
    prepared = PreparedChatRun(
        conversation_id="conv-1",
        conversation={"id": "conv-1"},
        input_data={},
        request_id="req-1",
        content=[],
        metadata=None,
        user_message={"id": "user-1"},
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={"retry": {"max_attempts": 2, "delays": [0]}},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="hello",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[],
        tools_called=[],
        connected_tool_names=set(),
        call_handler=None,
        model_routing={},
    )

    response = engine._complete_turn(prepared, [{"role": "user", "content": "hello"}])

    assert client.calls == 2
    assert response["content"] == [{"type": "text", "text": "continued after wrapped 429"}]
    assert any(event.get("type") == "ai_retry_scheduled" for event in engine._activity_events)


def test_final_response_reports_unattached_requested_tools():
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "coding_file_read",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    unselected_entry = {
        "tool_name": "coding_terminal_exec",
        "status": "blocked",
        "reason_code": "not_connected_to_profile",
        "reason": "selected tool is not connected to the active runtime profile",
    }
    engine = ChatRunEngine(store=object(), client=object())
    prepared = PreparedChatRun(
        conversation_id="conv-1",
        conversation={"id": "conv-1"},
        input_data={},
        request_id="req-1",
        content=[],
        metadata=None,
        user_message={"id": "user-1"},
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={},
        tool_context={
            "requested_tool_ids": ["coding_file_read", "coding_terminal_exec"],
            "unselected_requested_tools": [unselected_entry],
        },
        standard_messages=[],
        user_text="run pwd",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["coding_file_read"],
        connected_tool_names={"coding_file_read"},
        call_handler=None,
        model_routing={},
    )

    response = engine._final_response(
        prepared,
        {"content": [{"type": "text", "text": "done"}], "finish_reason": "stop"},
    )

    metadata = response["metadata"]
    assert metadata["requested_tools"] == ["coding_file_read", "coding_terminal_exec"]
    assert metadata["attached_tools"] == ["coding_file_read"]
    assert metadata["unattached_requested_tools"] == ["coding_terminal_exec"]
    assert metadata["tool_attachment_diagnostics"]["unselected_requested_tools"] == [unselected_entry]


def test_stream_empty_thinking_retry_preserves_tools_for_tool_calls():
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    class GoogleProvider:
        pass

    class FakeGateway:
        def __init__(self):
            self.complete_requests = []

        def resolve_provider(self, model):
            return GoogleProvider(), model

        def supports_stream(self, model):
            return True

        def stream(self, request):
            yield {"type": "thinking_delta", "delta": {"type": "text", "text": "I should use a tool."}}
            yield {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}

        def complete(self, request):
            self.complete_requests.append(request)
            return {
                "content": [
                    {"type": "text", "text": ""},
                    {
                        "type": "tool_use",
                        "id": "call-browser-1",
                        "name": "browser_computer",
                        "input": {"action": "computer.context", "payload": {}},
                    },
                ],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

    gateway = FakeGateway()
    engine = ChatRunEngine(store=object(), gateway=gateway)
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "browser_computer",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id="conv-1",
        conversation={"id": "conv-1"},
        input_data={},
        request_id="req-1",
        content=[],
        metadata=None,
        user_message={"id": "user-1"},
        model="google/gemma-4-31b-it",
        params={"thinking_level": "high", "reasoning_effort": "high"},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="computer use使ってみて",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["browser_computer"],
        connected_tool_names={"browser_computer"},
        call_handler=None,
        model_routing={},
    )

    generator = engine._model_turn(prepared, [{"role": "user", "content": "computer use使ってみて"}], None)
    events = []
    try:
        while True:
            events.append(next(generator))
    except StopIteration as exc:
        response, tool_uses = exc.value

    assert gateway.complete_requests
    assert gateway.complete_requests[0]["tools"] == provider_tools
    assert "thinking_level" not in gateway.complete_requests[0]["params"]
    assert "reasoning_effort" not in gateway.complete_requests[0]["params"]
    assert response["metadata"]["recovered_from_empty_stream"] is True
    assert response["metadata"]["fallback_kept_tools"] is True
    assert tool_uses[0]["name"] == "browser_computer"
    assert any(event.get("type") == "thinking_delta" for event in events)


def test_final_response_preserves_provider_thinking_metadata():
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    engine = ChatRunEngine(store=object())
    prepared = PreparedChatRun(
        conversation_id="conv-1",
        conversation={"id": "conv-1"},
        input_data={},
        request_id="req-1",
        content=[],
        metadata=None,
        user_message={"id": "user-1"},
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        params={"thinking_level": "high"},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="qa",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[],
        tools_called=[],
        connected_tool_names=set(),
        call_handler=None,
        model_routing={},
    )
    response = {
        "content": [{"type": "text", "text": "visible QA finding"}],
        "finish_reason": "stop",
        "metadata": {
            "thinking": {
                "state": "completed",
                "transcript": "provider diagnostic trace",
                "source": "google_native_thought",
            }
        },
    }

    finalized = engine._final_response(prepared, response)

    assert finalized["metadata"]["thinking"]["transcript"] == "provider diagnostic trace"
    assert finalized["metadata"]["thinking"]["source"] == "google_native_thought"


def test_chat_run_engine_observes_external_cancel_checker():
    from domain.chat.stream_engine import ChatRunEngine

    engine = ChatRunEngine(store=object())
    cancelled = {"value": False}
    engine._external_cancel_checker = lambda: cancelled["value"]

    assert engine._is_cancelled() is False
    cancelled["value"] = True
    assert engine._is_cancelled() is True


def test_complete_with_tools_rejects_unattached_model_tool_call():
    from blocks.chat import send

    ai_calls = 0
    invoked_tools = []

    def call_handler(name, payload):
        nonlocal ai_calls
        if name == "defaults.ai.complete":
            ai_calls += 1
            return {
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-danger",
                            "name": "dangerous_tool",
                            "input": {"payload": "owned"},
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            }
        if name == "defaults.tool.invoke":
            invoked_tools.append(payload["tool_name"])
            return {"status": "ok", "data": {"result": "should not run", "is_error": False}}
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "hello"}],
        [{"name": "allowed_tool"}],
        {},
        call_handler,
        {"max_tool_calls": 3},
    )

    assert ai_calls == 1
    assert invoked_tools == []
    assert response["finish_reason"] == "tool_call_rejected"
    assert response["metadata"]["tool_call_rejected"] is True
    assert response["metadata"]["rejected_tool_name"] == "dangerous_tool"
    assert response["metadata"]["connected_tools"] == ["allowed_tool"]
    assert response["tool_logs"] == []
    assert any(event.get("phase") == "tool_call_rejected" for event in response["events"])


def test_legacy_complete_with_tools_retries_transient_ai_error_after_tool_use():
    from blocks.chat import send

    ai_calls = 0
    tool_calls = 0

    def call_handler(name, payload):
        nonlocal ai_calls, tool_calls
        if name == "defaults.ai.complete":
            ai_calls += 1
            if ai_calls == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "browser_computer",
                                "input": {"action": "computer.context", "payload": {}},
                            }
                        ],
                        "finish_reason": "tool_use",
                    },
                }
            if ai_calls == 2:
                return {
                    "status": "error",
                    "error": {"message": "Google API error 500: Internal error encountered."},
                }
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "continued after retry"}],
                    "finish_reason": "stop",
                },
            }
        if name == "defaults.tool.invoke":
            tool_calls += 1
            return {
                "status": "ok",
                "data": {"result": "ok", "is_error": False, "widget": None},
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "hello"}],
        [{"name": "browser_computer"}],
        {"profile_policy": {"max_tool_calls": 3}},
        call_handler,
        {"retry": {"max_attempts": 2, "delays": [0]}},
    )

    assert ai_calls == 3
    assert tool_calls == 1
    assert response["content"] == [{"type": "text", "text": "continued after retry"}]
    assert any(event.get("type") == "ai_retry_scheduled" for event in response["events"])


class _IRFakeGateway:
    def __init__(self):
        self.complete_requests = []
        self.calls = 0

    def complete(self, request):
        self.complete_requests.append(request)
        self.calls += 1
        if self.calls == 1:
            return {
                "content": [{"type": "tool_use", "id": "call-ir-1", "name": "lookup", "input": {"q": "x"}}],
                "finish_reason": "tool_calls",
            }
        return {"content": [{"type": "text", "text": "done"}], "finish_reason": "stop", "usage": {}}

    def stream(self, request):
        return iter([])

    def supports_stream(self, model):
        return False

    def resolve_provider(self, model):
        class Provider:
            pass

        return Provider(), model.split("/", 1)[1] if "/" in model else model


def _run_ir_tool_loop(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, arguments, context: {"result": "tool ok", "is_error": False})
    gateway = _IRFakeGateway()
    engine = ChatRunEngine(store=store, gateway=gateway)
    events = list(
        engine.stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "use tool"},
                "tools": [{"tool_id": "lookup", "name": "lookup", "summary": "lookup", "schema": {"parameters": {"type": "object"}}}],
            },
            {},
            stream_mode=False,
        )
    )
    stored = store.get_conversation(conversation["id"])["messages"][-1]
    return gateway, events, stored, store


def test_stream_engine_ir_tool_loop_matches_legacy(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    gateway, events, stored, store = _run_ir_tool_loop(tmp_path, monkeypatch)

    assert any(message.get("content") == "use tool" for message in gateway.complete_requests[0]["messages"])
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["id"] == "call-ir-1"
    assert stored["raw_text"] == "done"
    assert any(event.get("type") == "tool_call_completed" for event in events)
    ChatStore._instance = None


def test_stream_engine_ir_preserves_tool_call_ids(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    gateway, events, stored, store = _run_ir_tool_loop(tmp_path, monkeypatch)

    assert any(event.get("data", {}).get("tool_call_id") == "call-ir-1" for event in events)
    assert stored["tool_logs"][0]["tool_call_id"] == "call-ir-1"
    ChatStore._instance = None


def _run_text_tool_call_response(
    tmp_path,
    monkeypatch,
    first_text,
    *,
    metadata=None,
    request_context=None,
    tool_context=None,
    tool_names=("rumi_api",),
):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.tool.executor import ToolExecutor

    class Gateway:
        def __init__(self):
            self.complete_requests = []

        def complete(self, request):
            self.complete_requests.append(request)
            if len(self.complete_requests) == 1:
                return {
                    "content": [{"type": "text", "text": first_text}],
                    "finish_reason": "stop",
                    "usage": {},
                }
            return {
                "content": [{"type": "text", "text": "routes checked"}],
                "finish_reason": "stop",
                "usage": {},
            }

        def stream(self, request):
            return iter([])

        def supports_stream(self, model):
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="opencode-go/mimo-v2.5")
    calls = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "ok", "is_error": False, "routes": ["/api/health"]}

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    gateway = Gateway()
    user_message = {
        "id": "user-1",
        "role": "user",
        "content": "check routes",
        "sequence_number": 1,
    }
    if metadata:
        user_message["metadata"] = metadata
    store.add_message(conversation["id"], user_message)
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": str(tool_name),
                "description": str(tool_name),
                "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
            },
        }
        for tool_name in tool_names
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation["id"],
        conversation={"id": conversation["id"], "messages": [user_message]},
        input_data={},
        request_id="req-text-tool",
        content=[],
        metadata=metadata or {},
        user_message=user_message,
        model="opencode-go/mimo-v2.5",
        params={},
        request_context=request_context or {},
        tool_context=tool_context or {},
        standard_messages=[{"role": "user", "content": "check routes"}],
        user_text="check routes",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=[str(tool_name) for tool_name in tool_names],
        connected_tool_names={str(tool_name) for tool_name in tool_names},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)
    engine = ChatRunEngine(store=store, gateway=gateway)
    events = list(engine.stream({}, {}, stream_mode=False))
    stored = store.get_conversation(conversation["id"])["messages"][-1]
    ChatStore._instance = None
    return calls, gateway, events, stored


def test_stream_engine_recovers_single_text_tool_call(tmp_path, monkeypatch):
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        (
            "<tool_call>\n"
            "<function=rumi_api>\n"
            "<parameter=action>list_routes</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
    )
    assert calls == [("rumi_api", {"action": "list_routes"})]
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "rumi_api"
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_recovers_issue396_scheduled_mimo_rumi_api_text_tool_call(tmp_path, monkeypatch):
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        (
            "<tool_call>\n"
            "<function=rumi_api>\n"
            "<parameter=action>list_routes</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
        metadata={"source": "scheduler", "profile_id": "defaultspack.mimo_coding_company"},
        request_context={"source": "scheduler", "profile_id": "defaultspack.mimo_coding_company"},
        tool_names=("rumi_api",),
    )

    assert calls == [("rumi_api", {"action": "list_routes"})]
    assert len(gateway.complete_requests) == 2
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "rumi_api"
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_recovers_prefaced_text_tool_call_for_mimo_scheduler(tmp_path, monkeypatch):
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        (
            "Got desktops. I will inspect the selected frame.\n\n"
            "<tool_call>\n"
            "<function=rumi_api>\n"
            "<parameter=action>request</parameter>\n"
            "<parameter=method>GET</parameter>\n"
            "<parameter=path>/api/desktops/seat-1/frame</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
        metadata={"source": "scheduler", "profile_id": "defaultspack.mimo_coding_company"},
    )

    assert calls == [
        (
            "rumi_api",
            {
                "action": "request",
                "method": "GET",
                "path": "/api/desktops/seat-1/frame",
            },
        )
    ]
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "rumi_api"
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_recovers_prefaced_text_tool_call_for_mimo_scheduler_followup(tmp_path, monkeypatch):
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        (
            "Got desktops. Using the approved desktop_list result.\n\n"
            "<tool_call>\n"
            "<function=desktop_frame>\n"
            "<parameter=owner_id>local-user</parameter>\n"
            "<parameter=seat_id>seat-1</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
        metadata={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_names=("desktop_frame",),
    )

    assert calls == [("desktop_frame", {"owner_id": "local-user", "seat_id": "seat-1"})]
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "desktop_frame"
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_recovers_next_text_tool_call_after_different_approval_replay(tmp_path, monkeypatch):
    raw_text = (
        "Found 1 running desktop: `QA-Swarm-Browser-1` (seat `90646b09`). Taking screenshot.\n\n"
        "<tool_call>\n"
        "<function=desktop_frame>\n"
        "<parameter=owner_id>local-user</parameter>\n"
        "<parameter=seat_id>90646b09-a548-48e0-8b57-f6f34e7275b7</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        raw_text,
        metadata={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={
            "approval_replayed": {
                "tool_name": "rumi_api",
                "tool_call_id": "call-approved",
                "request_id": "apr-approved",
                "arguments": {
                    "action": "request",
                    "method": "GET",
                    "path": "/api/desktops",
                },
            },
            "tool_approval_tokens": {"rumi_api": "spent-token"},
        },
        tool_names=("rumi_api", "desktop_frame"),
    )

    assert calls == [
        (
            "desktop_frame",
            {
                "owner_id": "local-user",
                "seat_id": "90646b09-a548-48e0-8b57-f6f34e7275b7",
            },
        )
    ]
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "desktop_frame"
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_suppresses_same_text_tool_recovery_after_approval_replay(tmp_path, monkeypatch):
    raw_text = (
        "<tool_call>\n"
        "<function=desktop_frame>\n"
        "<parameter=owner_id>local-user</parameter>\n"
        "<parameter=seat_id>seat-1</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        raw_text,
        metadata={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={
            "approval_replayed": {
                "tool_name": "desktop_frame",
                "tool_call_id": "call-approved",
                "request_id": "apr-approved",
            },
            "tool_approval_tokens": {"desktop_frame": "spent-token"},
        },
        tool_names=("desktop_frame",),
    )

    assert calls == []
    assert len(gateway.complete_requests) == 1
    assert stored["raw_text"] == raw_text
    assert not any(event.get("type") == "tool_call_started" for event in events)


def test_stream_engine_treats_consumed_approval_followup_as_idempotent_duplicate(tmp_path, monkeypatch):
    from domain.safety import approval

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    approved_args = {"owner_id": "local-user", "seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "high",
        approved_args,
        details={
            "tool_name": "desktop_frame",
            "action": "tool.desktop_frame",
            "arguments": approved_args,
        },
    )
    decision = approval.approve(request["request_id"])
    token = decision["token"]
    consumed = approval.verify_execution_token(
        token,
        "tool.desktop_frame",
        approval.hash_arguments(approved_args),
        consume=True,
    )
    assert consumed.valid is True

    raw_text = (
        "<tool_call>\n"
        "<function=desktop_frame>\n"
        "<parameter=owner_id>local-user</parameter>\n"
        "<parameter=seat_id>seat-1</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        raw_text,
        metadata={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
            "approval_followup": {
                "approval_token": token,
                "request_id": request["request_id"],
                "tool_name": "desktop_frame",
            },
        },
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={"tool_approval_tokens": {"desktop_frame": token}},
        tool_names=("desktop_frame",),
    )

    assert calls == []
    assert len(gateway.complete_requests) == 0
    assert stored["raw_text"] != raw_text
    assert "承認済みの操作はすでに処理済みです" in stored["raw_text"]
    assert not any(event.get("type") == "tool_call_started" for event in events)
    replay = approval.verify_execution_token(
        token,
        "tool.desktop_frame",
        approval.hash_arguments(approved_args),
        consume=False,
    )
    assert replay.valid is False
    assert replay.code == "APPROVAL_TOKEN_USED"


def test_nonstream_scheduled_mimo_initial_run_syncs_draft_before_model_turn(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    metadata = {
        "source": "scheduler",
        "profile_id": "defaultspack.mimo_coding_company",
    }
    user_message = store.add_message(
        conversation_id,
        {
            "id": "user-scheduled-initial",
            "role": "user",
            "content": "run scheduled MiMo desktop QA",
            "metadata": metadata,
        },
    )
    assert store.get_conversation(conversation_id)["current_node_id"] == user_message["id"]

    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation=store.get_conversation(conversation_id),
        input_data={},
        request_id="req-scheduled-initial-draft",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={},
        standard_messages=[{"role": "user", "content": "run scheduled MiMo desktop QA"}],
        user_text="run scheduled MiMo desktop QA",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    observed: dict[str, object] = {}

    def fake_model_turn(self, prepared_arg, working_messages, draft):
        del prepared_arg, working_messages, draft
        current = store.get_conversation(conversation_id)
        current_id = current["current_node_id"]
        current_message = next(item for item in current["messages"] if item["id"] == current_id)
        observed["current_role"] = current_message["role"]
        observed["current_parent_id"] = current_message["parent_id"]
        observed["current_metadata"] = dict(current_message.get("metadata") or {})
        observed["current_events"] = list(current_message.get("events") or [])
        observed["current_tool_logs"] = list(current_message.get("tool_logs") or [])
        raise RuntimeError("initial scheduler model blocked")
        yield

    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    engine = ChatRunEngine(store=store)
    events = list(engine.stream({}, {}, stream_mode=False))

    assert observed["current_role"] == "assistant"
    assert observed["current_parent_id"] == user_message["id"]
    assert observed["current_metadata"]["draft"] is True
    assert observed["current_metadata"]["streaming"] is True
    assert any(event.get("phase") == "tools_attached" for event in observed["current_events"])
    assert observed["current_tool_logs"] == []
    assert any(event.get("type") == "assistant_message_started" for event in events)
    assert any(event.get("type") == "task_failed" for event in events)
    ChatStore._instance = None


def test_nonstream_scheduled_mimo_finalizes_when_draft_update_is_stale(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="opencode-go/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    metadata = {
        "source": "scheduler",
        "profile_id": "defaultspack.mimo_coding_company",
    }
    user_message = store.add_message(
        conversation_id,
        {
            "id": "user-scheduled-stale-draft",
            "role": "user",
            "content": "run scheduled MiMo heartbeat",
            "metadata": metadata,
        },
    )
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation=store.get_conversation(conversation_id),
        input_data={},
        request_id="req-scheduled-stale-draft",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="opencode-go/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={},
        standard_messages=[{"role": "user", "content": "run scheduled MiMo heartbeat"}],
        user_text="run scheduled MiMo heartbeat",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[],
        tools_called=[],
        connected_tool_names=set(),
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    original_update_message = store.update_message

    def flaky_update_message(conversation_id_arg, message_id, updates):
        if updates.get("finish_reason") == "stop":
            return None
        return original_update_message(conversation_id_arg, message_id, updates)

    monkeypatch.setattr(store, "update_message", flaky_update_message)

    def fake_model_turn(self, prepared_arg, working_messages, draft):
        del self, prepared_arg, working_messages, draft
        if False:
            yield {}
        return (
            {
                "content": [{"type": "text", "text": "scheduled heartbeat complete"}],
                "finish_reason": "stop",
                "usage": {},
            },
            [],
        )

    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    engine = ChatRunEngine(store=store)
    events = list(engine.stream({}, {}, stream_mode=False))
    done_events = [event for event in events if event.get("type") == "done"]
    error_events = [event for event in events if event.get("type") == "error"]
    stored = store.get_conversation(conversation_id)
    final = stored["messages"][-1]

    assert done_events
    assert error_events == []
    assert final["role"] == "assistant"
    assert final["parent_id"] == user_message["id"]
    assert final["finish_reason"] == "stop"
    assert final["raw_text"] == "scheduled heartbeat complete"
    assert final["metadata"].get("draft") is None
    ChatStore._instance = None


def test_nonstream_scheduled_mimo_followup_syncs_replay_to_draft_before_summary(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="opencode-go/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {"owner_id": "local-user", "seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "high",
        approved_args,
        details={
            "tool_name": "desktop_frame",
            "action": "tool.desktop_frame",
            "arguments": approved_args,
            "conversation_id": conversation_id,
        },
    )
    token = approval.approve(request["request_id"])["token"]
    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": {
            "approval_token": token,
            "request_id": request["request_id"],
            "tool_name": "desktop_frame",
        },
    }
    user_message = store.add_message(
        conversation_id,
        {
            "id": "user-scheduled-followup",
            "role": "user",
            "content": "continue approved scheduled desktop QA",
            "metadata": metadata,
        },
    )
    assert store.get_conversation(conversation_id)["current_node_id"] == user_message["id"]

    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation=store.get_conversation(conversation_id),
        input_data={},
        request_id="req-scheduled-followup-draft",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="opencode-go/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={"tool_approval_tokens": {"desktop_frame": token}},
        standard_messages=[{"role": "user", "content": "continue approved scheduled desktop QA"}],
        user_text="continue approved scheduled desktop QA",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    calls: list[tuple[str, dict]] = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "frame captured", "is_error": False, "widget": {"type": "desktop_frame"}}

    observed: dict[str, object] = {}

    def fake_model_turn(self, prepared_arg, working_messages, draft):
        del prepared_arg, working_messages, draft
        current = store.get_conversation(conversation_id)
        current_id = current["current_node_id"]
        current_message = next(item for item in current["messages"] if item["id"] == current_id)
        observed["current_role"] = current_message["role"]
        observed["current_parent_id"] = current_message["parent_id"]
        observed["current_metadata"] = dict(current_message.get("metadata") or {})
        observed["current_tool_logs"] = list(current_message.get("tool_logs") or [])
        raise RuntimeError("summary model blocked")
        yield

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    monkeypatch.setattr(ChatRunEngine, "_model_turn", fake_model_turn)

    engine = ChatRunEngine(store=store)
    events = list(engine.stream({}, {}, stream_mode=False))

    assert calls == [("desktop_frame", {**approved_args, "approval_token": token})]
    assert observed["current_role"] == "assistant"
    assert observed["current_parent_id"] == user_message["id"]
    assert observed["current_metadata"]["draft"] is True
    assert observed["current_metadata"]["streaming"] is True
    assert observed["current_tool_logs"][0]["tool_name"] == "desktop_frame"
    assert observed["current_tool_logs"][0]["tool_call_id"] == request["request_id"]
    assert any(event.get("type") == "assistant_message_started" for event in events)
    assert any(event.get("type") == "task_failed" for event in events)
    ChatStore._instance = None


def test_stream_engine_scheduled_desktop_frame_approval_replay_consumes_approval(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {"owner_id": "local-user", "seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "medium",
        approved_args,
        details={
            "tool_name": "desktop_frame",
            "action": "tool.desktop_frame",
            "function_id": "tool.desktop_frame",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approved_args,
        },
    )
    token = approval.approve(request["request_id"])["token"]
    assert approval.get_approval_request(request["request_id"])["status"] == "approved"

    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": {
            "approval_token": token,
            "request_id": request["request_id"],
            "tool_name": "desktop_frame",
        },
    }
    user_message = {
        "id": "user-approved-frame",
        "role": "user",
        "content": "continue approved scheduled desktop frame",
        "metadata": metadata,
    }
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": [user_message]},
        input_data={},
        request_id="req-approved-frame",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={
            "tool_approval_tokens": {"desktop_frame": token},
            "owner_pack": "defaultspack",
            "source": "scheduler_approval_followup",
            "conversation_id": conversation_id,
        },
        standard_messages=[{"role": "user", "content": "continue approved scheduled desktop frame"}],
        user_text="continue approved scheduled desktop frame",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    calls: list[tuple[str, dict]] = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "frame captured", "is_error": False, "widget": {"type": "desktop_frame"}}

    class Gateway:
        def __init__(self):
            self.complete_requests = []

        def complete(self, request_data):
            self.complete_requests.append(request_data)
            return {
                "content": [{"type": "text", "text": "frame replay summarized"}],
                "finish_reason": "stop",
                "usage": {},
            }

        def stream(self, request_data):
            del request_data
            return iter([])

        def supports_stream(self, model):
            del model
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    gateway = Gateway()
    engine = ChatRunEngine(store=store, gateway=gateway)
    events = list(engine.stream({}, {}, stream_mode=False))
    stored = store.get_conversation(conversation_id)["messages"][-1]
    ChatStore._instance = None

    assert calls == [("desktop_frame", {**approved_args, "approval_token": token})]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"
    assert stored["raw_text"] == "frame replay summarized"
    assert len(gateway.complete_requests) == 1
    assert any(
        message.get("role") == "tool"
        and message.get("name") == "desktop_frame"
        and message.get("tool_call_id") == request["request_id"]
        for message in gateway.complete_requests[0]["messages"]
    )
    assert any(event.get("type") == "tool_call_completed" for event in events)
    assert not any(event.get("type") == "approval_requested" for event in events)


def test_stream_engine_scheduled_desktop_frame_replay_canonicalizes_display_tool_name(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {"owner_id": "local-user", "seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.Desktop Frame",
        "medium",
        approved_args,
        details={
            "tool_name": "Desktop Frame",
            "action": "tool.Desktop Frame",
            "function_id": "tool.Desktop Frame",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approved_args,
        },
    )
    token = approval.approve(request["request_id"])["token"]

    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": {
            "approval_token": token,
            "request_id": request["request_id"],
            "tool_name": "Desktop Frame",
        },
    }
    user_message = {
        "id": "user-approved-display-frame",
        "role": "user",
        "content": "continue approved scheduled desktop frame",
        "metadata": metadata,
    }
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": [user_message]},
        input_data={},
        request_id="req-approved-display-frame",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={
            "tool_approval_tokens": {"Desktop Frame": token},
            "owner_pack": "defaultspack",
            "source": "scheduler_approval_followup",
            "conversation_id": conversation_id,
        },
        standard_messages=[{"role": "user", "content": "continue approved scheduled desktop frame"}],
        user_text="continue approved scheduled desktop frame",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    calls: list[tuple[str, dict]] = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "frame captured", "is_error": False, "widget": {"type": "desktop_frame"}}

    class Gateway:
        def complete(self, request_data):
            return {
                "content": [{"type": "text", "text": "frame replay summarized"}],
                "finish_reason": "stop",
                "usage": {},
            }

        def stream(self, request_data):
            del request_data
            return iter([])

        def supports_stream(self, model):
            del model
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    events = list(ChatRunEngine(store=store, gateway=Gateway()).stream({}, {}, stream_mode=False))
    stored = store.get_conversation(conversation_id)["messages"][-1]
    ChatStore._instance = None

    assert calls == [("desktop_frame", {**approved_args, "approval_token": token})]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"
    assert stored["raw_text"] == "frame replay summarized"
    assert any(event.get("type") == "tool_call_completed" for event in events)
    assert not any(event.get("type") == "approval_requested" for event in events)


def test_stream_engine_scheduled_desktop_frame_approval_replay_suppresses_duplicate_native_tool_use(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {"owner_id": "local-user", "seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "medium",
        approved_args,
        details={
            "tool_name": "desktop_frame",
            "action": "tool.desktop_frame",
            "function_id": "tool.desktop_frame",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approved_args,
        },
    )
    token = approval.approve(request["request_id"])["token"]

    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": {
            "approval_token": token,
            "request_id": request["request_id"],
            "tool_name": "desktop_frame",
        },
    }
    user_message = {
        "id": "user-approved-frame-duplicate-native",
        "role": "user",
        "content": "continue approved scheduled desktop frame",
        "metadata": metadata,
    }
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": [user_message]},
        input_data={},
        request_id="req-approved-frame-duplicate-native",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={
            "tool_approval_tokens": {"desktop_frame": token},
            "owner_pack": "defaultspack",
            "source": "scheduler_approval_followup",
            "conversation_id": conversation_id,
        },
        standard_messages=[{"role": "user", "content": "continue approved scheduled desktop frame"}],
        user_text="continue approved scheduled desktop frame",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    calls: list[tuple[str, dict]] = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "frame captured", "is_error": False, "widget": {"type": "desktop_frame"}}

    class Gateway:
        def __init__(self):
            self.complete_requests = []

        def complete(self, request_data):
            self.complete_requests.append(request_data)
            return {
                "content": [
                    {"type": "text", "text": "frame already captured"},
                    {
                        "type": "tool_use",
                        "id": "call-frame-duplicate",
                        "name": "desktop_frame",
                        "input": json.dumps(approved_args),
                    },
                ],
                "finish_reason": "tool_calls",
                "usage": {},
            }

        def stream(self, request_data):
            del request_data
            return iter([])

        def supports_stream(self, model):
            del model
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    gateway = Gateway()
    engine = ChatRunEngine(store=store, gateway=gateway)
    events = list(engine.stream({}, {}, stream_mode=False))
    stored = store.get_conversation(conversation_id)["messages"][-1]
    ChatStore._instance = None

    assert calls == [("desktop_frame", {**approved_args, "approval_token": token})]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"
    assert stored["raw_text"] == "frame already captured"
    assert stored["metadata"]["executed_tools"] == ["desktop_frame"]
    assert len(gateway.complete_requests) == 1
    assert sum(1 for event in events if event.get("type") == "tool_call_completed") == 1
    assert not any(event.get("type") == "approval_requested" for event in events)


def test_stream_engine_scheduled_replay_duplicate_ignores_echoed_approval_token():
    from domain.chat.stream_engine import (
        _approval_replay_duplicate_tool_use,
        _text_tool_call_blocks_for_prepared,
    )

    prepared = SimpleNamespace(
        user_message={
            "metadata": {
                "source": "scheduler_approval_followup",
                "profile_id": "defaultspack.mimo_coding_company",
            }
        },
        request_context={},
        connected_tool_names={"desktop_frame"},
        tool_context={
            "approval_replayed": {
                "tool_name": "desktop_frame",
                "request_id": "apr-approved-frame",
                "arguments": {"owner_id": "local-user", "seat_id": "seat-1"},
            }
        },
    )

    duplicate_native = {
        "type": "tool_use",
        "id": "call-frame-duplicate",
        "name": "desktop_frame",
        "input": {
            "owner_id": "local-user",
            "seat_id": "seat-1",
            "approval_token": "spent-token",
        },
    }
    assert _approval_replay_duplicate_tool_use(prepared, duplicate_native) is True

    duplicate_text_response = {
        "content": [
            {
                "type": "text",
                "text": (
                    "<tool_call>\n"
                    "<function=desktop_frame>\n"
                    "<parameter=owner_id>local-user</parameter>\n"
                    "<parameter=seat_id>seat-1</parameter>\n"
                    "<parameter=approval_token>spent-token</parameter>\n"
                    "</function>\n"
                    "</tool_call>"
                ),
            }
        ]
    }
    assert _text_tool_call_blocks_for_prepared(duplicate_text_response, prepared) == []


def test_stream_engine_scheduled_desktop_frame_replay_uses_defaultspack_local_owner(
    tmp_path,
    monkeypatch,
    defaultspack_capability_plan_context,
):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool import desktop_tools
    from domain.tool_policy.internal_context import seal_tool_context

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {"seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "medium",
        approved_args,
        details={
            "tool_name": "desktop_frame",
            "action": "tool.desktop_frame",
            "function_id": "tool.desktop_frame",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approved_args,
        },
    )
    token = approval.approve(request["request_id"])["token"]

    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": {
            "approval_token": token,
            "request_id": request["request_id"],
            "tool_name": "desktop_frame",
        },
    }
    user_message = {
        "id": "user-approved-frame-local-owner",
        "role": "user",
        "content": "continue approved scheduled desktop frame",
        "metadata": metadata,
    }
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    plan_context = defaultspack_capability_plan_context("desktop_frame")
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": [user_message]},
        input_data={},
        request_id="req-approved-frame-local-owner",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context=seal_tool_context(
            {
                **plan_context,
                "tool_approval_tokens": {"desktop_frame": token},
                "owner_pack": "defaultspack",
                "source": "scheduler_approval_followup",
                "conversation_id": conversation_id,
                "principal_id": "profile:defaultspack.mimo_coding_company",
                "authority_principal_id": "profile:defaultspack.mimo_coding_company",
                "agent_id": "browser_qa",
            },
            {"action": "allow", "allowed": True},
        ),
        standard_messages=[{"role": "user", "content": "continue approved scheduled desktop frame"}],
        user_text="continue approved scheduled desktop frame",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    class FakeSandboxApi:
        def __init__(self):
            self.calls = []

        def run(self, payload, context):
            self.calls.append((dict(payload), dict(context)))
            return {
                "_binary": True,
                "content_type": "image/png",
                "body": b"fake-png",
                "headers": {},
            }

    class Gateway:
        def complete(self, request_data):
            return {
                "content": [{"type": "text", "text": "frame replay summarized"}],
                "finish_reason": "stop",
                "usage": {},
            }

        def stream(self, request_data):
            del request_data
            return iter([])

        def supports_stream(self, model):
            del model
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    fake_api = FakeSandboxApi()
    monkeypatch.setattr(desktop_tools, "_sandbox_api", lambda: fake_api)

    events = list(ChatRunEngine(store=store, gateway=Gateway()).stream({}, {}, stream_mode=False))
    stored = store.get_conversation(conversation_id)["messages"][-1]
    ChatStore._instance = None

    assert stored["raw_text"] == "frame replay summarized"
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"
    assert not any(event.get("type") == "approval_requested" for event in events)
    assert len(fake_api.calls) == 1
    payload, context = fake_api.calls[0]
    assert payload["seat_id"] == "seat-1"
    assert payload["owner_id"] == "local-user"
    assert payload["access_owner_id"] == "local-user"
    assert context["principal_id"] == "local-user"
    assert context["agent_id"] == "browser_qa"


def test_stream_engine_scheduled_desktop_frame_replay_consumes_legacy_inline_args(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {"owner_id": "local-user", "seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "medium",
        approved_args,
        details={
            "tool_name": "desktop_frame",
            "action": "tool.desktop_frame",
            "function_id": "tool.desktop_frame",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
        },
    )
    token = approval.approve(request["request_id"])["token"]
    assert approval.get_approval_request(request["request_id"])["status"] == "approved"

    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": {
            "approval_token": token,
            "request_id": request["request_id"],
            "tool_name": "desktop_frame",
            "arguments": approved_args,
        },
    }
    user_message = {
        "id": "user-approved-frame-legacy-inline",
        "role": "user",
        "content": "continue approved scheduled desktop frame",
        "metadata": metadata,
    }
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_frame",
                "description": "desktop_frame",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": [user_message]},
        input_data={},
        request_id="req-approved-frame-legacy-inline",
        content=[],
        metadata=metadata,
        user_message=user_message,
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={
            "tool_approval_tokens": {"desktop_frame": token},
            "owner_pack": "defaultspack",
            "source": "scheduler_approval_followup",
            "conversation_id": conversation_id,
        },
        standard_messages=[{"role": "user", "content": "continue approved scheduled desktop frame"}],
        user_text="continue approved scheduled desktop frame",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_frame"],
        connected_tool_names={"desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    calls: list[tuple[str, dict]] = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "frame captured", "is_error": False, "widget": {"type": "desktop_frame"}}

    class Gateway:
        def __init__(self):
            self.complete_requests = []

        def complete(self, request_data):
            self.complete_requests.append(request_data)
            return {
                "content": [{"type": "text", "text": "frame replay summarized"}],
                "finish_reason": "stop",
                "usage": {},
            }

        def stream(self, request_data):
            del request_data
            return iter([])

        def supports_stream(self, model):
            del model
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    engine = ChatRunEngine(store=store, gateway=Gateway())
    events = list(engine.stream({}, {}, stream_mode=False))
    ChatStore._instance = None

    assert calls == [("desktop_frame", {**approved_args, "approval_token": token})]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"
    assert any(event.get("type") == "tool_call_completed" for event in events)
    assert not any(event.get("type") == "approval_requested" for event in events)


def test_stream_engine_scheduled_mimo_approval_replay_keeps_tools_for_distinct_followup(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    approval.reset_approval_state_for_tests()

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    conversation_id = conversation["id"]

    approved_args = {
        "owner_id": "local-user",
        "seat_id": "seat-1",
        "action": "click",
        "x": 12,
        "y": 34,
    }
    request = approval.create_approval_request(
        "tool.desktop_input",
        "high",
        approved_args,
        details={
            "tool_name": "desktop_input",
            "action": "tool.desktop_input",
            "arguments": approved_args,
            "conversation_id": conversation_id,
        },
    )
    token = approval.approve(request["request_id"])["token"]

    class Gateway:
        def __init__(self):
            self.complete_requests = []

        def complete(self, request):
            self.complete_requests.append(request)
            if len(self.complete_requests) == 1:
                tool_names = {
                    tool.get("function", {}).get("name")
                    for tool in request.get("tools", [])
                    if isinstance(tool, dict)
                }
                assert "desktop_frame" in tool_names
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-frame",
                            "name": "desktop_frame",
                            "input": {"owner_id": "local-user", "seat_id": "seat-1"},
                        }
                    ],
                    "finish_reason": "tool_calls",
                    "usage": {},
                }
            return {
                "content": [{"type": "text", "text": "desktop input and frame completed"}],
                "finish_reason": "stop",
                "usage": {},
            }

        def stream(self, request):
            return iter([])

        def supports_stream(self, model):
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    calls = []

    def fake_execute(self, name, arguments, context):
        calls.append((name, dict(arguments)))
        return {"result": "ok", "is_error": False, "widget": None}

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for tool_name in ("desktop_input", "desktop_frame")
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": []},
        input_data={},
        request_id="req-approved-input",
        content=[],
        metadata={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
            "approval_followup": {
                "approval_token": token,
                "request_id": request["request_id"],
                "tool_name": "desktop_input",
            },
        },
        user_message={
            "id": "user-approved-input",
            "role": "user",
            "content": "continue",
            "metadata": {
                "source": "scheduler_approval_followup",
                "profile_id": "defaultspack.mimo_coding_company",
                "approval_followup": {
                    "approval_token": token,
                    "request_id": request["request_id"],
                    "tool_name": "desktop_input",
                },
            },
        },
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        params={},
        request_context={
            "source": "scheduler_approval_followup",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        tool_context={"tool_approval_tokens": {"desktop_input": token}},
        standard_messages=[{"role": "user", "content": "continue"}],
        user_text="continue",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_input", "desktop_frame"],
        connected_tool_names={"desktop_input", "desktop_frame"},
        call_handler=None,
        model_routing={},
    )
    monkeypatch.setattr(engine_module, "prepare_chat_run", lambda input_data, context: prepared)

    gateway = Gateway()
    engine = ChatRunEngine(store=store, gateway=gateway)
    events = list(engine.stream({}, {}, stream_mode=False))
    stored = store.get_conversation(conversation_id)["messages"][-1]
    ChatStore._instance = None

    assert calls == [
        ("desktop_input", {**approved_args, "approval_token": token}),
        ("desktop_frame", {"owner_id": "local-user", "seat_id": "seat-1"}),
    ]
    assert len(gateway.complete_requests) == 2
    assert stored["raw_text"] == "desktop input and frame completed"
    assert sum(1 for event in events if event.get("type") == "tool_call_completed") == 2


def test_stream_engine_recovers_multiple_text_tool_calls_for_mimo_scheduler(tmp_path, monkeypatch):
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        (
            "Check company status and desktops.\n\n"
            "<tool_call>\n"
            "<function=rumi_api>\n"
            "<parameter=action>request</parameter>\n"
            "<parameter=method>GET</parameter>\n"
            "<parameter=path>/api/company/status</parameter>\n"
            "</function>\n"
            "</tool_call>"
            "<tool_call>\n"
            "<function=desktop_frame>\n"
            "<parameter=owner_id>local-user</parameter>\n"
            "<parameter=seat_id>seat-1</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
        metadata={"source": "scheduler", "profile_id": "defaultspack.mimo_coding_company"},
        tool_names=("rumi_api", "desktop_frame"),
    )

    assert calls == [
        (
            "rumi_api",
            {"action": "request", "method": "GET", "path": "/api/company/status"},
        ),
        ("desktop_frame", {"owner_id": "local-user", "seat_id": "seat-1"}),
    ]
    tool_call_messages = [
        message
        for message in gateway.complete_requests[1]["messages"]
        if isinstance(message.get("tool_calls"), list)
    ]
    assert len(tool_call_messages[-1]["tool_calls"]) == 2
    assert stored["raw_text"] == "routes checked"
    assert sum(1 for event in events if event.get("type") == "tool_call_completed") == 2


def test_stream_engine_recovers_text_assistant_progress_without_connected_tool(tmp_path, monkeypatch):
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        (
            "<tool_call>\n"
            "<function=assistant_progress>\n"
            "<parameter=summary>Worker-1 target: http://127.0.0.1:8766/chat</parameter>\n"
            "<parameter=next_action>Take first screenshot of chat UI</parameter>\n"
            "</function>\n"
            "</tool_call>"
        ),
        metadata={"source": "scheduler", "profile_id": "defaultspack.mimo_coding_company"},
        tool_names=(),
    )

    assert calls == []
    assert len(gateway.complete_requests) == 2
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "assistant_progress" for event in events)


def test_stream_engine_recovers_text_tool_invocation_for_mimo_scheduler(tmp_path, monkeypatch):
    raw_text = (
        "Desktop input accepted! Let me take a screenshot to see the result.\n\n"
        '<tool_invocation name="rumi_api" arguments={"action":"request","method":"GET","path":"/api/desktops/seat-1/frame"} />'
    )
    calls, gateway, events, stored = _run_text_tool_call_response(
        tmp_path,
        monkeypatch,
        raw_text,
        metadata={"source": "scheduler", "profile_id": "defaultspack.mimo_coding_company"},
        tool_names=("rumi_api",),
    )

    assert calls == [
        (
            "rumi_api",
            {"action": "request", "method": "GET", "path": "/api/desktops/seat-1/frame"},
        )
    ]
    assert gateway.complete_requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "rumi_api"
    assert stored["raw_text"] == "routes checked"
    assert any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_ignores_prefaced_text_tool_call_outside_mimo_scheduler(tmp_path, monkeypatch):
    raw_text = (
        "For example, a model might write this instead of calling the tool.\n\n"
        "<tool_call>\n"
        "<function=rumi_api>\n"
        "<parameter=action>list_routes</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls, gateway, events, stored = _run_text_tool_call_response(tmp_path, monkeypatch, raw_text)

    assert calls == []
    assert len(gateway.complete_requests) == 1
    assert stored["raw_text"] == raw_text
    assert not any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_ignores_text_tool_invocation_outside_mimo_scheduler(tmp_path, monkeypatch):
    raw_text = (
        "For example, a model might write this instead of calling the tool.\n\n"
        '<tool_invocation name="rumi_api" arguments={"action":"list_routes"} />'
    )
    calls, gateway, events, stored = _run_text_tool_call_response(tmp_path, monkeypatch, raw_text)

    assert calls == []
    assert len(gateway.complete_requests) == 1
    assert stored["raw_text"] == raw_text
    assert not any(event.get("type") == "tool_call_completed" for event in events)


def test_stream_engine_provider_trace_metadata(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from pathlib import Path

    gateway, events, stored, store = _run_ir_tool_loop(tmp_path, monkeypatch)

    trace = stored["metadata"]["provider_trace"]
    assert trace["request_id"]
    assert Path(trace["trace_path"]).exists()
    assert stored["metadata"]["ir"]["schema_version"] == "rumi.chat.ir.v2"
    assert "provider_planning" in stored["metadata"]
    planning = stored["metadata"]["provider_planning"]
    assert planning["provider_tool_count"] == 1
    assert planning["provider_tools"][0]["name"] == "lookup"
    assert "parameters" not in str(planning["provider_tools"][0])
    ChatStore._instance = None


def test_stream_engine_legacy_flag_uses_legacy_messages(monkeypatch):
    from domain.chat.stream_engine import ChatRunEngine
    from domain.chat.run_request import PreparedChatRun

    monkeypatch.setenv("RUMI_DEFAULTSPACK_PROVIDER_COMPILER_V2", "1")
    monkeypatch.setenv("RUMI_DEFAULTSPACK_PROVIDER_LEGACY_MESSAGES", "1")

    assert ChatRunEngine._use_provider_compiler(PreparedChatRun(conversation_id="c", conversation={}, input_data={}, request_id="r", content=[], metadata={}, user_message={}, model="m", params={}, request_context={}, tool_context={}, standard_messages=[], user_text="", system_prompt="", enrich_info={}, raw_tools=[], provider_tools=[], tools_called=[], connected_tool_names=set(), call_handler=None, model_routing={})) is False


def test_stream_engine_ir_handles_streaming_tool_delta():
    from domain.chat.stream_engine import ChatRunEngine
    from domain.chat.run_request import PreparedChatRun

    class Gateway:
        def supports_stream(self, model):
            return True

        def stream(self, request):
            return iter(
                [
                    {"type": "tool_call_start", "id": "tc", "name": "lookup"},
                    {"type": "tool_call_delta", "id": "tc", "name": "lookup", "arguments_chunk": "{\"q\""},
                    {"type": "tool_call_delta", "id": "tc", "name": "lookup", "arguments_chunk": ":\"x\"}"},
                    {"type": "tool_call_end", "id": "tc", "name": "lookup"},
                    {"type": "stream_end", "finish_reason": "tool_calls", "usage": {}},
                ]
            )

        def complete(self, request):
            raise AssertionError("complete should not be called")

        def resolve_provider(self, model):
            class OpenAIProvider:
                pass

            return OpenAIProvider(), model

    engine = ChatRunEngine(gateway=Gateway())
    prepared = PreparedChatRun(conversation_id="c", conversation={}, input_data={}, request_id="r", content=[], metadata={}, user_message={"id": "u"}, model="openai/gpt", params={}, request_context={}, tool_context={}, standard_messages=[], user_text="", system_prompt="", enrich_info={}, raw_tools=[], provider_tools=[{"type": "function", "function": {"name": "lookup"}}], tools_called=["lookup"], connected_tool_names={"lookup"}, call_handler=None, model_routing={})
    generator = engine._model_turn(prepared, [{"role": "user", "content": "hi"}], None)
    events = []
    try:
        while True:
            events.append(next(generator))
    except StopIteration as exc:
        response, tool_uses = exc.value

    assert tool_uses[0]["id"] == "tc"
    assert tool_uses[0]["input"] == {"q": "x"}
    assert any(event.get("type") == "tool_call_delta" for event in events)


def test_stream_engine_ir_finalizes_assistant_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    gateway, events, stored, store = _run_ir_tool_loop(tmp_path, monkeypatch)

    assert stored["role"] == "assistant"
    assert stored["finish_reason"] == "stop"
    assert stored["metadata"]["provider_capabilities"]["provider_id"] == "openai"
    ChatStore._instance = None


def test_compact_tool_log_value_truncates_large_outputs():
    from blocks.chat.send import _compact_tool_log_value

    compact = _compact_tool_log_value({
        "status": "ok",
        "data": {
            "content": "x" * 5_000,
            "stdout": "y" * 5_000,
            "items": list(range(25)),
        },
    })

    assert len(compact["data"]["content"]) < 2_000
    assert "tool log truncated" in compact["data"]["content"]
    assert len(compact["data"]["stdout"]) < 2_000
    assert compact["data"]["items"][-1]["omitted_items"] == 9
