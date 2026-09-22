from __future__ import annotations

import subprocess
import sys
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _owned_conversation(tmp_path, conversation_id):
    """Read a conversation from the selected profile-scoped owner."""
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    return ConversationStore("defaults", user_data_root=tmp_path).get(conversation_id)


def _bind_fake_contract_stream(monkeypatch, client):
    """Bind a fake provider behind the v4 global stream adapter."""
    from domain.ai_client import gateway_contract_client

    def invoke(contract_id, operation, payload):
        if contract_id == "rumi.service.ai.generate.v1":
            assert operation == "generate"
            response = client.complete(
                str(payload.get("model_reference") or ""),
                list(payload.get("messages") or []),
                list(payload.get("tools") or []),
                dict(payload.get("parameters") or {}),
            )
            return {
                "output": list(response.get("content") or []),
                "tool_intents": list(response.get("tool_calls") or []),
                "finish_reason": response.get("finish_reason"),
                "usage": dict(response.get("usage") or {}),
            }

        assert contract_id == "rumi.service.ai.stream.v1"
        assert operation == "stream"
        events = []
        for chunk in client.stream(
            str(payload.get("model_reference") or ""),
            list(payload.get("messages") or []),
            list(payload.get("tools") or []),
            dict(payload.get("parameters") or {}),
        ):
            chunk_type = str(chunk.get("type") or "") if isinstance(chunk, dict) else ""
            if chunk_type == "content_delta":
                delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                text = str(delta.get("text") or "")
                if text:
                    events.append({"type": "text_delta", "delta": text})
            elif chunk_type in {"reasoning_delta", "thinking_delta"}:
                delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
                text = str(delta.get("text") or chunk.get("text") or "")
                if text:
                    events.append({"type": "thinking_delta", "delta": text})
            elif chunk_type == "stream_end":
                events.append(
                    {
                        "type": "finish",
                        "finish_reason": str(chunk.get("finish_reason") or "stop"),
                        "usage": dict(chunk.get("usage") or {}),
                    }
                )
            elif chunk_type == "error":
                events.append({"type": "error"})
        return {"events": events}

    monkeypatch.setattr(gateway_contract_client, "_invoke", invoke)


def _component_http_routes():
    """Return route declarations from the current domain component registry."""
    from ecosystem.defaultspack.transport.registry import component_http_route_specs

    return {
        (spec.method, spec.pattern): spec
        for spec in component_http_route_specs()
    }


def test_capability_catalog_loads_plan_manifest():
    from domain.capability.catalog import CapabilityCatalog

    catalog = CapabilityCatalog(DEFAULTSPACK_ROOT)
    manifest = catalog.manifest()

    assert manifest["local_first"] is True
    assert manifest["core_requires_api_key"] is False
    assert manifest["default_profile"] == "defaultspack.local_agent"
    assert manifest["counts"]["capabilities"] >= 11
    assert manifest["counts"]["profiles"] >= 5
    capability_ids = {item["id"] for item in manifest["capabilities"]}
    assert {"local_file", "terminal", "git", "safety", "artifact", "compact", "research"} <= capability_ids


def test_chat_store_persists_conversations_to_user_data(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    persisted = _owned_conversation(tmp_path, conversation["id"])
    assert persisted["id"] == conversation["id"]
    assert persisted["model_reference"] == "stub/default"


def test_conversation_owner_rejects_stale_update_without_overwrite(tmp_path):
    import pytest
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationConflict,
        ConversationStore,
    )

    owner = ConversationStore("defaults", user_data_root=tmp_path)
    owner.create(
        {"id": "atomic-preserve", "model_reference": "stub/default"},
        expected_revision=0,
    )
    before = owner.get("atomic-preserve")

    with pytest.raises(ConversationConflict):
        owner.update(
            "atomic-preserve",
            {"title": "must not win"},
            expected_conversation_revision=0,
        )

    assert owner.get("atomic-preserve") == before


def test_conversation_owner_stale_append_preserves_existing_messages(tmp_path):
    import pytest
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationConflict,
        ConversationStore,
    )

    owner = ConversationStore("defaults", user_data_root=tmp_path)
    created = owner.create(
        {"id": "atomic-retry", "model_reference": "stub/default"},
        expected_revision=0,
    )["conversation"]
    owner.append_message(
        "atomic-retry",
        {"id": "message-1", "role": "user", "raw_text": "saved"},
        expected_conversation_revision=created["conversation_revision"],
    )

    with pytest.raises(ConversationConflict):
        owner.append_message(
            "atomic-retry",
            {"id": "message-2", "role": "user", "raw_text": "stale"},
            expected_conversation_revision=created["conversation_revision"],
        )

    assert [item["id"] for item in owner.get("atomic-retry")["messages"]] == [
        "message-1"
    ]


def test_chat_store_message_is_persisted_by_selected_owner(tmp_path):
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    message = store.add_message(
        conversation["id"],
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    )

    persisted = _owned_conversation(tmp_path, conversation["id"])
    assert [item["id"] for item in persisted["messages"]] == [message["id"]]


def test_chat_store_message_write_does_not_corrupt_other_conversations(tmp_path):
    from domain.chat.store import ChatStore

    store = ChatStore()
    locked_conversation = store.create_conversation(model="stub/default")
    active_conversation = store.create_conversation(model="stub/default")

    message = store.add_message(
        active_conversation["id"],
        {"role": "user", "content": [{"type": "text", "text": "still saved"}]},
    )

    assert _owned_conversation(tmp_path, locked_conversation["id"])["messages"] == []
    active = _owned_conversation(tmp_path, active_conversation["id"])
    assert [item["id"] for item in active["messages"]] == [message["id"]]


def test_model_profiles_expose_required_context_and_thinking_metadata():
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog

    profiles = list_profile_catalog()
    by_id = {profile["profile_id"]: profile for profile in profiles}

    assert by_id["stub/default"]["max_context"] == -1
    assert isinstance(by_id["stub/default"]["max_context"], int)
    assert by_id["stub/default"]["supports_thinking"] is False
    assert by_id["stub/default"]["thinking_levels"] == []
    assert "rumi/auto" not in by_id


def test_chat_send_records_selected_model_without_unavailable_tools(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello with tools"},
            "params": {"thinking_level": "medium"},
        },
        {},
    )

    assert result["status"] == "ok"
    assistant = result["data"]
    assert assistant["metadata"]["model"] == "stub/default"
    # The selected legacy migration plan intentionally has no tool-capable
    # model provider.  The request must remain usable while tool attachment
    # fails closed instead of widening the plan at the call site.
    assert assistant["metadata"]["attached_tool_count"] == 0
    assert assistant["metadata"]["thinking_level"] == "none"
    assert not any(event["phase"] == "tools_attached" for event in assistant["events"])

    persisted = _owned_conversation(tmp_path, conversation["id"])
    stored_assistant = persisted["messages"][-1]
    assert stored_assistant["metadata"]["attached_tool_count"] == assistant["metadata"]["attached_tool_count"]

    store = ChatStore()
    conversation = store.create_conversation(tags=["persisted"])
    message = store.add_message(
        conversation["id"],
        {"role": "user", "content": [{"type": "text", "text": "hello persistence"}]},
    )

    payload = _owned_conversation(tmp_path, conversation["id"])
    assert payload["id"] == conversation["id"]
    assert payload["messages"][0]["id"] == message["id"]

    reloaded = ChatStore()
    assert reloaded.get_conversation(conversation["id"])["messages"][0]["raw_text"] == "hello persistence"
    ChatStore._instance = None


def test_chat_send_retries_transient_ai_errors(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    monkeypatch.setattr("blocks.chat.send.time.sleep", lambda _delay: None)
    ChatStore._instance = None
    calls = {"ai": 0}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {"status": "error", "error": {"message": "temporary upstream timeout"}}
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "retried"}],
                    "finish_reason": "stop",
                },
            }
        raise AssertionError(name)

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "retry please"},
            "tools": [],
            "params": {"retry": {"max_attempts": 2, "delays": [0]}},
        },
        {"call_handler": call_handler},
    )

    assert result["status"] == "ok"
    assert calls["ai"] == 2
    assert result["data"]["raw_text"] == "retried"
    assert any(event["type"] == "ai_retry_scheduled" for event in result["data"]["events"])
    ChatStore._instance = None


def test_chat_send_persists_terminal_ai_error_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            return {"status": "error", "error": {"message": "invalid request 400"}}
        raise AssertionError(name)

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "fail visibly"},
            "tools": [],
            "params": {"retry": {"enabled": False}},
        },
        {"call_handler": call_handler},
    )

    assert result["status"] == "ok"
    assistant = result["data"]
    assert assistant["finish_reason"] == "error"
    assert assistant["metadata"]["thinking"]["state"] == "failed"
    assert assistant["metadata"]["error"]["terminal"] is True

    persisted = _owned_conversation(tmp_path, conversation["id"])
    messages = persisted["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["finish_reason"] == "error"
    ChatStore._instance = None


def test_chat_terminal_ai_error_compacts_google_tool_schema_failure():
    from blocks.chat.send import _ai_error_response

    raw_error = (
        'AI request failed: Google API error 400: {"error":{"code":400,'
        '"message":"* GenerateContentRequest.tools[0].function_declarations[53].parameters.properties[rows].items: missing field\\n'
        '* GenerateContentRequest.tools[0].function_declarations[54].parameters.properties[columns].items: missing field\\n'
        '* GenerateContentRequest.tools[0].function_declarations[55].parameters.properties[items].items: missing field",'
        '"status":"INVALID_ARGUMENT"}}'
    )

    response = _ai_error_response("google/gemma-4-31b-it", raw_error, {})
    text = response["content"][0]["text"]

    assert "APIエラーでこのタスクを終了しました。" in text
    assert "Google HTTP 400" in text
    assert "INVALID_ARGUMENT" in text
    assert "tool 定義" in text
    assert len(text) < 1000
    assert "raw_message" in response["metadata"]["error"]


def test_chat_store_links_subagent_conversations(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
    )

    parent_after = store.get_conversation(parent["id"])
    child_after = store.get_conversation(child["id"])
    assert child_after["parent_conversation_id"] == parent["id"]
    assert child_after["conversation_kind"] == "subagent"
    assert child["id"] in parent_after["child_conversation_ids"]

    store.delete_conversation(child["id"])
    assert child["id"] not in store.get_conversation(parent["id"])["child_conversation_ids"]
    ChatStore._instance = None


def test_todo_tool_persists_in_conversation_workspace(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.todo import TodoController

    workspace = tmp_path / "conversation" / "workspace"
    result = TodoController().run(
        {"action": "add", "title": "ブラウザ確認", "priority": "high"},
        {"conversation_workspace_dir": str(workspace)},
    )

    todo_path = workspace / "todos.json"
    assert result["todos"][0]["title"] == "ブラウザ確認"
    assert todo_path.exists()
    assert json.loads(todo_path.read_text(encoding="utf-8"))["todos"][0]["priority"] == "high"


def test_subagent_tool_creates_child_conversation(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from ecosystem.rumi_default_tools_pack.domain.tool.subagent import SubagentController

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    result = SubagentController().run(
        {"task": "hello from subagent", "title": "Subagent check"},
        {"conversation_id": parent["id"], "model": "stub/default"},
    )

    parent_after = store.get_conversation(parent["id"])
    child = store.get_conversation(result["child_conversation_id"])
    assert result["child_conversation_id"] in parent_after["child_conversation_ids"]
    assert child["title"] == "Subagent check"
    assert [message["role"] for message in child["messages"]] == ["user", "assistant"]
    assert result["workspace"]["contract_version"] == "rumi.agent_workspace.v1"
    assert result["workspace"]["mode"] == "child_conversation_workspace"
    assert child["metadata"]["workspace"]["workspace_root"] == result["workspace"]["workspace_root"]
    assert Path(result["workspace"]["workspace_root"]).is_dir()
    ChatStore._instance = None


def test_integration_secret_store_reads_chat_tokens_without_env_injection(tmp_path, monkeypatch):
    from domain.integrations.secrets import (
        get_integration_secret,
        load_integration_secrets_into_env,
        set_integration_secret,
    )

    secrets_dir = tmp_path / "secrets"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(secrets_dir))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    result = set_integration_secret("slack", "SLACK_BOT_TOKEN", "xoxb-test")
    assert result["success"] is True
    os.environ.pop("SLACK_BOT_TOKEN", None)

    loaded = load_integration_secrets_into_env()
    assert loaded["slack"] is True
    assert "SLACK_BOT_TOKEN" not in os.environ
    assert get_integration_secret("slack", "SLACK_BOT_TOKEN") == "xoxb-test"


def test_unit_executor_does_not_pass_integration_tokens_to_python_fallback(monkeypatch):
    from core_runtime.unit_executor import UnitExecutor

    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-token")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    process_env = UnitExecutor._build_subprocess_env()

    assert "LINE_CHANNEL_ACCESS_TOKEN" not in process_env
    assert process_env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_external_integration_component_routes_are_registered():
    routes = _component_http_routes()
    integration_routes = {
        key: spec
        for key, spec in routes.items()
        if "/api/integrations/" in key[1]
    }

    assert set(integration_routes) == {
        ("POST", "/api/integrations/discord/interactions"),
        ("POST", "/api/integrations/discord/events"),
        ("POST", "/api/integrations/line/webhook"),
        ("POST", "/api/integrations/slack/events"),
    }
    assert all(
        spec.owner_pack_id == "defaultspack"
        and spec.block_module.startswith("blocks.integrations.")
        for spec in integration_routes.values()
    )
    assert ("POST", "/api/integrations/secrets") not in routes
    assert ("GET", "/api/chat/conversations/{id}/run-results/{run_id}/browser-screenshots") not in routes
    assert ("GET", "/v1/conversations/{id}/run-results/{run_id}/browser-screenshots") not in routes


def test_external_integration_routes_are_registered():
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
    )
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


def test_slack_event_creates_external_conversation(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.integrations.slack import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    integration_path = tmp_path / "user_data" / "shared" / "integrations" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(integration_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_ALLOW_UNSIGNED_DEV", "1")
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    ChatStore._instance = None

    result = run(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "ts": "1.0",
                "text": "hello from slack",
            },
            "model": "stub/default",
            "tools": [],
        },
        {},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["status"] == "ok"
    assert data["reply"]["sent"] is False

    conversation = _owned_conversation(tmp_path, data["conversation_id"])
    assert conversation["conversation_kind"] == "external"
    assert conversation["model_reference"] == "stub/default"
    assert "integration:slack" in conversation["tags"]
    assert conversation["messages"][0]["metadata"]["external"]["provider"] == "slack"
    ChatStore._instance = None


def test_slack_event_fails_closed_without_signing_secret(tmp_path, monkeypatch):
    from blocks.integrations.slack import run

    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_INTEGRATIONS_ALLOW_UNSIGNED_DEV", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

    result = run(
        {
            "type": "event_callback",
            "event": {"type": "message", "channel": "C1", "user": "U1", "ts": "1.0", "text": "hello"},
        },
        {},
    )

    assert result["status"] == "error"
    assert result["_http_status"] == 401
    assert result["error"]["code"] == "SIGNATURE_INVALID"


def test_discord_ping_and_agent_engine_queue_multiple_tool_calls(monkeypatch):
    from blocks.integrations.discord import run
    from domain.agent.engine import AgentEngine
    from domain.agent.execution import AgentExecution

    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_ALLOW_UNSIGNED_DEV", "1")
    assert run({"type": 1}, {})["type"] == 1

    engine = AgentEngine()
    parsed = engine._parse_ai_response(
        {
            "status": "ok",
            "data": {
                "tool_calls": [
                    {"function": {"name": "calculator", "arguments": "{\"expression\":\"1+1\"}"}},
                    {"function": {"name": "todo", "arguments": "{\"action\":\"list\"}"}},
                ]
            },
        }
    )
    execution = AgentExecution("agent_test", "task", [], "stub/default", "")
    engine._set_pending_tool_call(execution, parsed)

    assert execution.pending_tool_call["tool_name"] == "calculator"
    assert [call["tool_name"] for call in execution.queued_tool_calls] == ["todo"]


def test_agent_engine_extracts_text_from_thinking_content_blocks():
    from domain.agent.engine import AgentEngine

    parsed = AgentEngine()._parse_ai_response(
        {
            "status": "ok",
            "data": {
                "content": [
                    {"type": "thinking", "thinking": "hidden chain"},
                    {"type": "text", "text": "社員レポート本文"},
                ]
            },
        }
    )

    assert parsed == {"type": "text", "content": "社員レポート本文"}


def test_chat_stream_uses_provider_stream_and_persists_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.stream import run
    import core_runtime.resolved_profile_scope as profile_scope

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    # This negative-path test requires the request worker to have no active
    # v4 snapshot.  The owner fixture remains installed for conversation
    # persistence, while the request boundary is explicitly fail-closed.
    monkeypatch.setattr(profile_scope, "active_resolved_profile", lambda: None)
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello stream"},
            "tools": [],
        },
        {},
    )

    assert result["_sse"] is True
    events = list(result["events"])
    deltas = [event["delta"] for event in events if event.get("type") == "delta"]
    assert "".join(deltas) == ""
    failed = [event for event in events if event.get("type") == "task_failed"]
    assert failed
    assert "interface registry is unavailable" in failed[-1]["error"]
    final = [event["message"] for event in events if event.get("type") == "message"][-1]
    assert final["role"] == "assistant"
    assert "interface registry is unavailable" in final["raw_text"]

    persisted = _owned_conversation(tmp_path, conversation["id"])
    messages = persisted["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    ChatStore._instance = None


def test_chat_stream_direct_path_honors_conversation_cancel(
    tmp_path,
    monkeypatch,
):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.interface_registry")
    assert_profile_resolver_requires_authority_snapshot()
    assert_payload_mutations_denied(harness(tmp_path))


def test_chat_stop_marks_streaming_assistant_draft_cancelled(tmp_path, monkeypatch):
    import blocks.chat.stop as stop_module
    from domain.chat.cancellation import get_chat_cancellation_registry
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    user_message = store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "stop me"}],
            "raw_text": "stop me",
        },
    )
    assistant = store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "parent_id": user_message["id"],
            "content": [],
            "raw_text": "",
            "finish_reason": "streaming",
            "metadata": {"streaming": True, "draft": True, "thinking": {"state": "running"}},
            "events": [],
            "tool_logs": [],
            "model": "stub/default",
        },
    )

    result = stop_module.run({"conversation_id": conversation["id"]}, {})

    assert result["status"] == "ok"
    assert result["data"]["persisted_cancelled"] is True
    stored = ChatStore().get_message(conversation["id"], assistant["id"])
    assert stored["finish_reason"] == "cancelled"
    assert stored["raw_text"] == "停止しました。"
    assert stored["metadata"]["thinking"]["state"] == "cancelled"
    assert "streaming" not in stored["metadata"]
    assert "draft" not in stored["metadata"]
    assert get_chat_cancellation_registry().is_cancelled(conversation["id"]) is False
    ChatStore._instance = None


def test_chat_stop_does_not_poison_future_run_when_no_active_callbacks(tmp_path, monkeypatch):
    import blocks.chat.stop as stop_module
    from domain.chat.cancellation import get_chat_cancellation_registry
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    user_message = store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "resume later"}],
            "raw_text": "resume later",
        },
    )
    store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "parent_id": user_message["id"],
            "content": [{"type": "text", "text": "許可が必要なため、ユーザーが承認するまで待機します。承認後に続行します。"}],
            "raw_text": "許可が必要なため、ユーザーが承認するまで待機します。承認後に続行します。",
            "finish_reason": "approval_required",
            "metadata": {
                "pending_approval": {
                    "tool_name": "coding_file_patch",
                    "approval_request_id": "apr_demo",
                }
            },
            "events": [],
            "tool_logs": [],
            "model": "stub/default",
        },
    )

    result = stop_module.run({"conversation_id": conversation["id"]}, {})

    assert result["status"] == "ok"
    assert result["data"]["persisted_cancelled"] is False
    assert get_chat_cancellation_registry().is_cancelled(conversation["id"]) is False
    ChatStore._instance = None


def test_chat_cancellation_register_keeps_pending_stop_request():
    from domain.chat.cancellation import ChatCancellationRegistry

    registry = ChatCancellationRegistry()
    called = []

    def callback():
        called.append(True)

    assert registry.request_cancel("c-prestop") is True
    registry.register("c-prestop", callback)

    assert called == [True]
    assert registry.is_cancelled("c-prestop") is True

    registry.unregister("c-prestop", callback)
    assert registry.is_cancelled("c-prestop") is False


def test_inline_thought_stream_filter_separates_thinking():
    from blocks.chat.stream import _InlineThoughtFilter

    filter_ = _InlineThoughtFilter()
    visible = [
        filter_.push("<tho"),
        filter_.push("ught>private"),
        filter_.push("</thought>public"),
        filter_.finish(),
    ]

    assert "".join(visible) == "public"
    assert filter_.transcript() == "private"


def test_inline_thought_stream_filter_exposes_incremental_thinking():
    from blocks.chat.stream import _InlineThoughtFilter

    filter_ = _InlineThoughtFilter()
    assert filter_.push("<thought>pri") == ""
    assert filter_.pending_thinking_delta() == "pri"
    assert filter_.push("vate</thought>public") == "public"
    assert filter_.pending_thinking_delta() == "vate"
    assert filter_.transcript() == "private"


def test_chat_stream_recovers_when_provider_returns_only_thinking(
    tmp_path,
    monkeypatch,
):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.interface_registry")
    assert_profile_resolver_requires_authority_snapshot()
    assert_payload_mutations_denied(harness(tmp_path))


def test_chat_stream_recovers_when_provider_returns_empty_text(
    tmp_path,
    monkeypatch,
):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.interface_registry")
    assert_profile_resolver_requires_authority_snapshot()
    assert_payload_mutations_denied(harness(tmp_path))


def test_chat_stream_retries_transient_ai_errors_before_visible_output(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    import blocks.chat.stream as stream_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    monkeypatch.setattr(stream_module.time, "sleep", lambda _delay: None)
    ChatStore._instance = None
    calls = {"stream": 0}

    class FakeAIClient:
        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools, params):
            calls["stream"] += 1
            if calls["stream"] == 1:
                raise RuntimeError("temporary upstream timeout")
            yield {"type": "content_delta", "delta": {"type": "text", "text": "retried stream"}}
            yield {
                "type": "stream_end",
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

    _bind_fake_contract_stream(monkeypatch, FakeAIClient())

    store = ChatStore()
    conversation = store.create_conversation(model="google/gemma-4-31b-it")
    result = stream_module.run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello"},
            "tools": [],
            "params": {"retry": {"max_attempts": 2, "delays": [0]}},
        },
        {},
    )

    events = list(result["events"])
    final = [event["message"] for event in events if event.get("type") == "message"][-1]

    assert calls["stream"] == 2
    assert any(event["type"] == "ai_retry_scheduled" for event in events)
    assert final["raw_text"] == "retried stream"
    assert any(event["type"] == "ai_retry_scheduled" for event in final["events"])
    ChatStore._instance = None


def test_chat_stream_persists_terminal_ai_error_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    import blocks.chat.stream as stream_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class FakeAIClient:
        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools, params):
            raise RuntimeError("invalid request 400")

    _bind_fake_contract_stream(monkeypatch, FakeAIClient())

    store = ChatStore()
    conversation = store.create_conversation(model="google/gemma-4-31b-it")
    result = stream_module.run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "hello"},
            "tools": [],
            "params": {"retry": {"max_attempts": 3, "delays": [0, 0]}},
        },
        {},
    )

    events = list(result["events"])
    final = [event["message"] for event in events if event.get("type") == "message"][-1]

    assert [event["type"] for event in events][-2:] == ["message", "done"]
    assert final["finish_reason"] == "error"
    assert final["metadata"]["thinking"]["state"] == "failed"
    assert final["metadata"]["error"]["terminal"] is True

    persisted = _owned_conversation(tmp_path, conversation["id"])
    messages = persisted["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["finish_reason"] == "error"
    ChatStore._instance = None


def test_chat_stream_explicit_empty_tools_blocks_computer_tool_inference(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.schema_adapter import tool_name_from_definition
    import domain.chat.run_request as run_request_module
    import blocks.chat.stream as stream_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    captured = {}

    def fail_fallback_send(*_args, **_kwargs):
        raise AssertionError("legacy _fallback_send should not be used")

    class FakeAIClient:
        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools, params):
            captured["tools"] = [tool_name_from_definition(tool) for tool in tools]
            yield {"type": "content_delta", "delta": {"type": "text", "text": "ok"}}
            yield {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

    def fake_prefocus(prepared):
        captured["user_requested_computer_use"] = prepared.request_context.get("user_requested_computer_use")
        return None

    monkeypatch.setattr(stream_module, "_fallback_send", fail_fallback_send)
    _bind_fake_contract_stream(monkeypatch, FakeAIClient())
    monkeypatch.setattr(run_request_module, "prefocus_computer_use_target_window", fake_prefocus)
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))

    store = ChatStore()
    conversation = store.create_conversation(model="google/gemma-4-31b-it")
    result = stream_module.run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "computer useでchromeを開いて"},
            "tools": [],
        },
        {},
    )

    events = list(result["events"])
    assert events[-1]["type"] == "done"
    assert captured["tools"] == []
    assert captured.get("user_requested_computer_use") is not True
    ChatStore._instance = None


def test_chat_stream_infers_computer_tools_when_tools_are_omitted(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.schema_adapter import tool_name_from_definition
    import blocks.chat.stream as stream_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    captured = {}

    def fail_fallback_send(*_args, **_kwargs):
        raise AssertionError("legacy _fallback_send should not be used")

    class FakeAIClient:
        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools, params):
            captured["tools"] = [tool_name_from_definition(tool) for tool in tools]
            yield {"type": "content_delta", "delta": {"type": "text", "text": "ok"}}
            yield {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(stream_module, "_fallback_send", fail_fallback_send)
    _bind_fake_contract_stream(monkeypatch, FakeAIClient())
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))

    store = ChatStore()
    conversation = store.create_conversation(model="google/gemma-4-31b-it")
    result = stream_module.run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "Google Chromeを操作してChatGPTを開いて"},
        },
        {},
    )

    events = list(result["events"])
    assert events[-1]["type"] == "done"
    # Text inference cannot grant host tools when the resolved plan/model does
    # not select a tool-capable provider.
    assert captured["tools"] == []
    ChatStore._instance = None


def test_chat_stream_fallback_yields_realtime_tool_progress(monkeypatch):
    import blocks.chat.stream as stream_module

    class FakeEngine:
        def __init__(self, client=None, gateway=None):
            self.client = client
            self.gateway = gateway

        def stream(self, input_data, context, *, stream_mode=True):
            yield {
                "schema_version": 1,
                "type": "tool_call_started",
                "run_id": "run-fallback",
                "conversation_id": str(input_data.get("conversation_id") or ""),
                "seq": 1,
                "data": {"tool_name": "browser_computer", "tool_call_id": "call_1"},
                "tool_name": "browser_computer",
                "tool_call_id": "call_1",
                "message": "browser_computer を使用中",
            }
            message = {
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "raw_text": "done",
            }
            yield {
                "schema_version": 1,
                "type": "assistant_message_completed",
                "run_id": "run-fallback",
                "conversation_id": str(input_data.get("conversation_id") or ""),
                "seq": 2,
                "data": {"message": message},
                "message": "assistant message completed",
            }
            yield {
                "schema_version": 1,
                "type": "done",
                "run_id": "run-fallback",
                "conversation_id": str(input_data.get("conversation_id") or ""),
                "seq": 3,
                "data": {"message": message},
                "message": "done",
            }

    monkeypatch.setattr(stream_module, "ChatRunEngine", FakeEngine)

    events = list(stream_module._fallback_send({"conversation_id": "c1"}, {}))

    assert events[0]["type"] == "tool_call_started"
    assert events[0]["tool_name"] == "browser_computer"
    assert events[-2]["type"] == "message"
    assert events[-1]["type"] == "done"


def test_chat_stream_does_not_execute_unplanned_tool_requests(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    calls = {"ai": 0}
    observed = {}
    streamed = []

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "calculator",
                                "input": "{\"expression\":\"2+2\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "tool done"}],
                    "finish_reason": "stop",
                },
            }
        if name == "defaults.tool.invoke":
            return {"status": "ok", "data": {"result": "4"}}
        raise AssertionError(name)

    def stream_event_callback(event):
        streamed.append(event)
        if event.get("type") != "tool_call_started" or "draft_id" in observed:
            return
        persisted = _owned_conversation(tmp_path, conversation["id"])
        messages = persisted["messages"]
        observed["roles_at_start"] = [message["role"] for message in messages]
        observed["draft_id"] = messages[-1]["id"]
        observed["draft_finish_reason"] = messages[-1]["finish_reason"]
        observed["draft_event_types"] = [draft_event["type"] for draft_event in messages[-1]["events"]]
        observed["draft_tool_name"] = messages[-1]["events"][-1]["tool_name"]

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "use a tool"},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
            "params": {"max_tool_calls": 3},
        },
        {
            "call_handler": call_handler,
            "stream_event_callback": stream_event_callback,
            "is_cancelled": lambda: False,
        },
    )

    assert result["status"] == "ok"
    assert not any(event["type"] == "tool_call_started" for event in streamed)
    assert observed == {}
    assert result["data"]["finish_reason"] == "error"

    persisted = _owned_conversation(tmp_path, conversation["id"])
    messages = persisted["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert not any(event["type"] == "tool_call_started" for event in messages[-1]["events"])
    ChatStore._instance = None


def test_chat_stop_cancels_active_fallback_worker(monkeypatch):
    import time
    import blocks.chat.stream as stream_module
    from domain.chat.cancellation import get_chat_cancellation_registry

    class FakeEngine:
        def __init__(self, client=None, gateway=None):
            self.client = client
            self.gateway = gateway

        def stream(self, input_data, context, *, stream_mode=True):
            conversation_id = str(input_data.get("conversation_id") or "")
            yield {
                "schema_version": 1,
                "type": "status",
                "run_id": "run-cancel",
                "conversation_id": conversation_id,
                "seq": 1,
                "data": {"phase": "thinking"},
                "message": "started",
                "phase": "thinking",
            }
            registry = get_chat_cancellation_registry()
            deadline = time.time() + 2
            while not registry.is_cancelled(conversation_id) and time.time() < deadline:
                time.sleep(0.01)
            assert registry.is_cancelled(conversation_id) is True
            yield {
                "schema_version": 1,
                "type": "cancelled",
                "run_id": "run-cancel",
                "conversation_id": conversation_id,
                "seq": 2,
                "data": {"reason": "cancelled"},
                "message": "cancelled",
            }

    monkeypatch.setattr(stream_module, "ChatRunEngine", FakeEngine)

    events = stream_module._fallback_send({"conversation_id": "c-stop"}, {})
    assert next(events)["message"] == "started"
    get_chat_cancellation_registry().request_cancel("c-stop")
    remaining = list(events)

    assert remaining[-1]["type"] == "error"
    assert "cancelled" in remaining[-1]["error"]


def test_chat_send_retries_empty_thinking_response_without_thinking(monkeypatch):
    import blocks.chat.send as send_module

    calls = []

    def fake_direct_complete(model, messages, tools=None, params=None):
        calls.append(dict(params or {}))
        if len(calls) == 1:
            return {
                "content": [{"type": "text", "text": ""}],
                "finish_reason": "malformed_function_call",
                "usage": {},
            }, None
        return {
            "content": [{"type": "text", "text": "Recovered send response."}],
            "finish_reason": "stop",
            "usage": {},
        }, None

    monkeypatch.setattr(send_module, "_ai_direct_complete", fake_direct_complete)

    response = send_module._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "hello"}],
        [],
        {},
        None,
        {"thinking_level": "high", "temperature": 0.1},
    )

    assert response["content"][0]["text"] == "Recovered send response."
    assert response["metadata"]["recovered_from_empty_response"] is True
    assert calls == [
        {"thinking_level": "high", "temperature": 0.1},
        {"temperature": 0.1},
    ]


def test_browser_computer_pack_not_approved_does_not_fall_back_to_local(monkeypatch):
    from domain.tool.executor import ToolExecutor

    called = {"local": False}

    def fake_execute_local(self, tool_name, arguments, context):
        called["local"] = True
        raise AssertionError("browser_computer must not bypass pack approval")

    class FakeResponse:
        success = False
        error_type = "pack_not_approved"

    monkeypatch.setattr(ToolExecutor, "_execute_local", fake_execute_local)

    result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
        {"name": "browser_computer"},
        {
            "type": "function.call",
            "qualified_name": "rumi_default_tools_pack:browser_computer",
            "args": {"action": "computer.click", "payload": {"x": 10, "y": 20}},
        },
        {"user_requested_computer_use": True},
        FakeResponse(),
    )

    assert result is None
    assert called["local"] is False


def test_computer_use_function_registry_unavailable_does_not_fall_back_to_local(monkeypatch):
    from domain.tool.executor import ToolExecutor

    called = {"local": False}

    def fake_execute_local(self, tool_name, arguments, context):
        called["local"] = True
        raise AssertionError("computer_use must not bypass the capability boundary")

    class FakeResponse:
        success = False
        error_type = "function_registry_unavailable"

    monkeypatch.setattr(ToolExecutor, "_execute_local", fake_execute_local)

    result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
        {"name": "computer_use"},
        {
            "type": "function.call",
            "qualified_name": "rumi_default_tools_pack:computer_use",
            "args": {"action": "context"},
        },
        {"conversation_id": "conv-test"},
        FakeResponse(),
    )

    assert result is None
    assert called["local"] is False


def test_browser_computer_click_can_use_virtual_cursor_for_preview(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(
        controller,
        "_capture_action_result_screenshot",
        lambda payload, marker, **kwargs: {"click_marker": marker},
    )
    monkeypatch.setattr(controller, "_window_at_point", lambda x, y: None)
    monkeypatch.setattr(
        controller,
        "_darwin_click",
        lambda payload: (_ for _ in ()).throw(AssertionError("physical click should not run")),
    )

    result = controller.run("computer.click", {"x": 10, "y": 20, "virtual_only": True}, yolo_mode=True)

    assert result["executed"] is True
    assert result["virtual_cursor"] is True
    assert result["target"] == {"x": 10, "y": 20}
    assert result["click_marker"]["screen_x"] == 10
    assert result["click_marker"]["screen_y"] == 20


def test_browser_computer_screenshot_falls_back_to_window_capture_when_rect_capture_fails(tmp_path, monkeypatch):
    from subprocess import CalledProcessError

    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    controller = BrowserComputerController(artifact_root=tmp_path)
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        controller,
        "_capture_target",
        lambda payload: {
            "app": "Google Chrome",
            "title": "rumi DP",
            "window_id": 3023,
            "capture_rect": {"x": 0, "y": 37, "width": 1470, "height": 919},
        },
    )
    calls = []

    def fake_run(command, check, **kwargs):
        del check
        calls.append(command)
        if "-R" in command:
            raise CalledProcessError(1, command)
        assert "-l" in command
        assert "timeout" in kwargs
        return None

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    result = controller._capture_screenshot(tmp_path / "shot.png", {"app": "Google Chrome"})

    assert result["target_window"]["window_id"] == 3023
    assert calls[0][2] == "-R"
    assert calls[1][2] == "-l"
    assert calls[1][3] == "3023"


def test_browser_computer_key_rejects_background_requests(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background request should not run")),
    )

    result = controller.run(
        "computer.key",
        {"key": "backspace", "app": "Google Chrome", "background": True},
        yolo_mode=True,
    )

    assert result["is_error"] is True
    assert result["executed"] is False
    assert result["recovery"]["kind"] == "visible_window_required"
    assert "visible windows" in result["reason"]


def test_browser_computer_click_sets_visible_target_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(controller, "_capture_action_result_screenshot", lambda payload, marker, **kwargs: {})
    monkeypatch.setattr(
        controller,
        "_window_at_point",
        lambda x, y: {
            "app": "Google Chrome",
            "title": "ChatGPT - Google Chrome",
            "x": 20,
            "y": 40,
            "width": 1200,
            "height": 800,
            "active": False,
        },
    )

    controller.run("computer.click", {"x": 120, "y": 140}, yolo_mode=True)

    state = controller._computer_state()
    assert state["target_window"]["app"] == "Google Chrome"
    assert controller._background_requested({}) is False
    assert controller._background_requested({"app": "Google Chrome", "background": True}) is True


def test_browser_computer_type_does_not_refocus_when_target_already_active(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    window = {
        "app": "Google Chrome",
        "title": "Google Gemini",
        "x": 20,
        "y": 40,
        "width": 1200,
        "height": 800,
        "active": True,
        "window_id": 7127,
    }
    monkeypatch.setattr(controller, "_active_window", lambda: dict(window))
    monkeypatch.setattr(
        controller,
        "_focus_window",
        lambda selected: (_ for _ in ()).throw(AssertionError("already-active target should not be refocused")),
    )

    assert controller._focus_action_target({"window": dict(window)}) is True


def test_browser_computer_active_window_capture_replaces_stale_selected_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    stale_window = {
        "app": "Google Chrome",
        "title": "Old Gemini",
        "x": 10,
        "y": 20,
        "width": 800,
        "height": 600,
        "window_id": 7127,
    }
    active_window = {
        "app": "Google Chrome",
        "title": "Rumi CUA Advanced Test",
        "x": 0,
        "y": 37,
        "width": 1470,
        "height": 919,
        "active": True,
        "window_id": 3023,
    }
    controller._write_computer_state({"target_window": stale_window})
    monkeypatch.setattr(controller, "_active_window", lambda: dict(active_window))

    assert controller._capture_target({"target": "active_window"})["window_id"] == 3023
    assert controller._computer_state()["target_window"]["title"] == "Rumi CUA Advanced Test"


def test_browser_computer_background_type_request_is_visible_only_error(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import cgevent

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    target_window = {
        "app": "Google Chrome",
        "title": "QA background typing target",
        "window_id": 4242,
        "pid": 4242,
        "x": 0,
        "y": 0,
        "width": 1200,
        "height": 800,
    }
    monkeypatch.setattr(controller, "_matching_window", lambda payload: dict(target_window))
    monkeypatch.setattr(controller, "_pid_matches_app", lambda pid, app: True)
    posted = {}

    def fake_post_key_to_pid(pid, text="", key_combo=""):
        posted.update({"pid": pid, "text": text, "key_combo": key_combo})
        return True

    monkeypatch.setattr(cgevent, "post_key_to_pid", fake_post_key_to_pid)
    monkeypatch.setattr(
        controller,
        "_apple_script",
        lambda action, payload: (_ for _ in ()).throw(AssertionError("foreground typing should not run")),
    )

    result = controller.run("computer.type", {"text": "hello", "background": True, "app": "Google Chrome"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["executed"] is True
    assert result["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"
    assert result["completion_verified"] is False
    assert result["effect_observed"] is False
    assert posted == {"pid": 4242, "text": "hello", "key_combo": ""}


def test_browser_computer_background_fallback_flags_do_not_enable_background(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("foreground fallback should not run")),
    )

    result = controller.run(
        "computer.type",
        {
            "text": "hello",
            "app": "Google Chrome",
            "background": True,
            "allow_foreground_fallback": True,
        },
        yolo_mode=True,
    )

    assert result["executed"] is False
    assert result["is_error"] is True
    assert result["recovery"]["kind"] == "visible_window_required"


def test_browser_computer_select_window_respects_app_filter(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 37, "active": True},
        {"app": "Google Chrome", "title": "", "x": 0, "y": 0, "width": 1470, "height": 37, "active": True},
        {"app": "Google Chrome", "title": "ChatGPT - Google Chrome", "x": 50, "y": 80, "width": 1200, "height": 800, "active": False},
    ]
    controller._active_window = lambda: {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 37, "active": True}

    result = controller.run("computer.select_window", {"app": "Google Chrome", "focus": False}, yolo_mode=True)

    assert result["selected"] is True
    assert result["target_window"]["app"] == "Google Chrome"
    assert controller._computer_state()["target_window"]["title"] == "ChatGPT - Google Chrome"


def test_browser_computer_select_window_prefers_main_browser_window_over_popup(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {
            "app": "Google Chrome",
            "title": "このページを翻訳しますか？",
            "x": 0,
            "y": 37,
            "width": 1470,
            "height": 206,
            "active": True,
            "capture_rect": {"x": 0, "y": 37, "width": 1470, "height": 206},
            "content_rect": {"x": 769, "y": 118, "width": 282, "height": 125},
        },
        {
            "app": "Google Chrome",
            "title": "Gemma Mouse Precision Test",
            "x": 0,
            "y": 37,
            "width": 1470,
            "height": 919,
            "active": True,
            "capture_rect": {"x": 0, "y": 37, "width": 1470, "height": 919},
            "content_rect": {"x": 0, "y": 158, "width": 1470, "height": 798},
        },
    ]

    result = controller.run("computer.select_window", {"app": "Google Chrome", "focus": False}, yolo_mode=True)

    assert result["selected"] is True
    assert result["target_window"]["title"] == "Gemma Mouse Precision Test"
    assert result["target_window"]["capture_rect"]["height"] == 919


def test_browser_computer_select_window_failure_clears_stale_target(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_computer_state(
        {
            "target_window": {
                "app": "Codex",
                "title": "",
                "x": 0,
                "y": 0,
                "width": 1470,
                "height": 37,
                "active": True,
            }
        }
    )
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 37, "active": True},
    ]
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.select_window", {"app": "Google Chrome", "focus": False}, yolo_mode=True)

    assert result["selected"] is False
    assert "target_window" not in controller._computer_state()


def test_browser_computer_select_window_requires_visible_browser_window(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 37, "active": True},
    ]

    result = controller.run("computer.select_window", {"app": "Google Chrome", "title": "ChatGPT", "focus": False}, yolo_mode=True)

    assert result["selected"] is False
    assert "chrome_target" not in result
    assert "target_window" not in controller._computer_state()


def test_browser_computer_context_exposes_ai_cursor_and_selected_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_computer_state(
        {
            "ai_cursor": {"x": 10, "y": 20, "origin": "top_left"},
            "target_window": {
                "app": "Google Chrome",
                "title": "ChatGPT - Google Chrome",
                "x": 50,
                "y": 80,
                "width": 1200,
                "height": 800,
            },
        }
    )
    controller._active_window = lambda: {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 900}
    controller._list_windows = lambda: []
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: {"x": 1, "y": 2, "origin": "top_left"}))

    result = controller.run("computer.context", {"include_windows": False}, yolo_mode=True)

    assert result["ai_cursor"]["x"] == 10
    assert result["selected_window"]["app"] == "Google Chrome"
    assert result["active_window"]["app"] == "Codex"
    assert "windows" not in result


def test_browser_computer_context_reports_visible_only_capability(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._active_window = lambda: {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 900}

    result = controller.run("computer.context", {"include_windows": False}, yolo_mode=True)

    assert "chrome_background_control" not in result
    assert any("visible-screen only" in note for note in result["notes"])
    assert result["browser_session"] == {"last_url": None, "last_opened_with_managed_profile": False}


def test_browser_computer_context_clears_tiny_stale_selected_window(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_computer_state(
        {
            "target_window": {
                "app": "Google Chrome",
                "title": "",
                "x": 0,
                "y": 0,
                "width": 1470,
                "height": 37,
                "active": True,
            }
        }
    )
    controller._active_window = lambda: {"app": "Codex", "title": "", "x": 0, "y": 0, "width": 1470, "height": 900}
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.context", {"include_windows": False}, yolo_mode=True)

    assert result["selected_window"] is None
    assert "target_window" not in controller._computer_state()


def test_chat_send_persists_user_attachment_metadata(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "hello",
                "attachments": [
                    {"name": "notes.md", "content": "hello from attachment", "size": 21, "type": "text/markdown"},
                    {"name": "photo.png", "size": 128, "type": "image/png"},
                ],
                "metadata": {"selected_tools": ["local_file"]},
            },
            "tools": ["local_file"],
            "params": {"tool_policy": {"selected_tools": ["local_file"]}},
        },
        {},
    )

    assert result["status"] == "ok"
    persisted = _owned_conversation(tmp_path, conversation["id"])
    stored_user = persisted["messages"][0]
    assert stored_user["metadata"]["attachments"][0]["name"] == "notes.md"
    assert stored_user["metadata"]["attachments"][1]["name"] == "photo.png"
    assert stored_user["metadata"]["selected_tools"] == ["local_file"]
    artifact_store = ChatStore()
    workspace_path = artifact_store.conversation_workspace_dir(conversation["id"])
    assert (workspace_path / "attachments" / "notes.md").read_text(encoding="utf-8") == "hello from attachment"
    assert stored_user["metadata"]["workspace_attachments"][0]["workspace_path"] == "workspace/attachments/notes.md"
    user_text = "\n".join(block.get("text", "") for block in stored_user["content"])
    assert "添付ファイル: notes.md" in user_text
    assert "hello from attachment" in user_text
    assert "photo.png" not in user_text
    ChatStore._instance = None


def test_chat_send_accepts_attachment_only_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "",
                "attachments": [{"name": "notes.md", "content": "hello", "size": 5, "type": "text/markdown"}],
            },
        },
        {},
    )

    assert result["status"] == "ok"
    persisted = _owned_conversation(tmp_path, conversation["id"])
    stored_user = persisted["messages"][0]
    user_text = "\n".join(block.get("text", "") for block in stored_user["content"])
    assert "添付ファイルを確認してください。" in user_text
    assert "添付ファイル: notes.md" in user_text
    assert "hello" in user_text
    assert stored_user["metadata"]["attachments"][0]["name"] == "notes.md"
    ChatStore._instance = None


def test_chat_send_includes_workspace_attachment_content(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "このファイル見て",
                "attachments": [
                    {
                        "name": "README.md",
                        "content": "# Workspace Notes",
                        "size": 17,
                        "type": "text/plain",
                        "source": "workspace",
                        "sourcePath": "README.md",
                    }
                ],
            },
        },
        {},
    )

    assert result["status"] == "ok"
    persisted = _owned_conversation(tmp_path, conversation["id"])
    stored_user = persisted["messages"][0]
    user_text = "\n".join(block.get("text", "") for block in stored_user["content"])
    assert "添付ファイル: README.md" in user_text
    assert "# Workspace Notes" in user_text
    ChatStore._instance = None


def test_chat_send_resolves_selected_tool_ids_before_provider_adaptation(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.tool.registry import ToolRegistry
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    ToolRegistry._instance = None

    captured = {}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            captured["tools"] = payload["tools"]
            return {"status": "ok", "data": {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}}
        raise AssertionError(name)

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "read"},
            "tools": ["coding_file_read"],
        },
        {"call_handler": call_handler},
    )

    assert result["status"] == "ok"
    assert captured["tools"] == []
    assert result["data"]["metadata"].get("attached_tools", []) == []
    ChatStore._instance = None
    ToolRegistry._instance = None


def test_chat_send_drops_unknown_selected_tool_ids(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.tool.registry import ToolRegistry
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    ToolRegistry._instance = None

    captured = {}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            captured["tools"] = payload["tools"]
            return {"status": "ok", "data": {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}}
        raise AssertionError(name)

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "read"},
            "tools": ["coding_file_read", "missing_tool"],
        },
        {"call_handler": call_handler},
    )

    assert result["status"] == "ok"
    tool_names = [tool["function"]["name"] for tool in captured["tools"]]
    assert "missing_tool" not in tool_names
    assert tool_names == []
    ChatStore._instance = None
    ToolRegistry._instance = None


def test_chat_send_preserves_dict_tool_definitions(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.send import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    captured = {}
    tool_def = {
        "type": "function",
        "function": {
            "name": "custom_lookup",
            "description": "Look up custom data.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            captured["tools"] = payload["tools"]
            return {"status": "ok", "data": {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}}
        raise AssertionError(name)

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    result = run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "lookup"},
            "tools": [tool_def],
        },
        {"call_handler": call_handler},
    )

    assert result["status"] == "ok"
    assert captured["tools"] == []
    ChatStore._instance = None


def test_coding_context_and_branch_blocks(tmp_path, monkeypatch):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    from blocks.coding.context import run as context_run
    from blocks.coding.git_branch import run as branch_run
    from domain.coding import contract_adapter
    from domain.safety.approval import reset_approval_state_for_tests
    from domain.coding.workspace_store import WorkspaceStore

    reset_approval_state_for_tests()
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH",
        str(tmp_path / "coding_workspaces.json"),
    )
    # This is the negative half of the canonical contract boundary.  Earlier
    # tests may leave a persisted Defaults Profile available in the worker;
    # make the provider absence explicit so this assertion remains isolated
    # without reviving the legacy workspace-path fallback.
    def invoke_without_provider(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("global coding provider is unavailable")

    monkeypatch.setitem(
        context_run.__globals__,
        "invoke_coding_contract",
        invoke_without_provider,
    )
    monkeypatch.setitem(
        branch_run.__globals__,
        "invoke_coding_contract",
        invoke_without_provider,
    )
    WorkspaceStore().create(tmp_path, workspace_id="ws1", trusted=True)

    # Coding providers are not selected by this legacy migration plan.  The
    # blocks therefore require the canonical provider and fail closed rather
    # than falling back to the caller's workspace path.
    context_result = context_run({"workspace_id": "ws1"}, {})
    assert context_result == {
        "status": "error",
        "error": {
            "code": "CONTEXT_ERROR",
            "message": "global coding provider is unavailable",
        },
    }

    nested_context_result = context_run(
        {"workspace_id": "ws1", "directory": "src"},
        {},
    )
    assert nested_context_result == context_result

    branch_result = branch_run({"workspace_id": "ws1"}, {})
    assert branch_result == {
        "status": "error",
        "error": {
            "code": "GIT_ERROR",
            "message": "global coding provider is unavailable",
        },
    }


def test_direct_chat_completion_forwards_tools_and_tool_context(monkeypatch):
    import blocks.chat.send as send

    captured = {}

    class DummyClient:
        def resolve_provider(self, model):
            return object(), model

        def complete(self, model, messages, tools=None, params=None):
            captured["model"] = model
            captured["messages"] = messages
            captured["tools"] = tools
            captured["params"] = params
            return {
                "content": [{"type": "text", "text": "ok"}],
                "finish_reason": "stop",
                "usage": {},
            }

    monkeypatch.setattr(send, "AIClient", DummyClient)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "Evaluate arithmetic.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    response = send._complete_with_tools(
        "openrouter/test-model",
        [{"role": "user", "content": "2+2"}],
        tools,
        {},
        None,
        {"temperature": 0},
    )

    assert captured["tools"] == tools
    assert captured["params"]["temperature"] == 0
    assert "calculator" in captured["messages"][0]["content"]
    assert response["metadata"]["attached_tools"] == ["calculator"]


def test_chat_tool_loop_replays_openai_tool_call_messages():
    import blocks.chat.send as send

    seen_messages = []

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            seen_messages.append(payload["messages"])
            if len(seen_messages) == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "calculator",
                                "input": "{\"expression\":\"2+2\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "tool result used"}],
                    "finish_reason": "stop",
                },
            }
        if name == "defaults.tool.invoke":
            return {"status": "ok", "data": {"result": "4"}}
        raise AssertionError(name)

    response = send._complete_with_tools(
        "openrouter/test-model",
        [{"role": "user", "content": "2+2"}],
        [{"type": "function", "function": {"name": "calculator", "parameters": {"type": "object"}}}],
        {},
        call_handler,
        {"max_tool_calls": 3},
    )

    assert response["content"][0]["text"] == "tool result used"
    assert seen_messages[1][-2]["role"] == "assistant"
    assert seen_messages[1][-2]["tool_calls"][0]["function"]["name"] == "calculator"
    assert seen_messages[1][-1]["role"] == "tool"
    assert seen_messages[1][-1]["tool_call_id"] == "call_1"


def test_chat_tool_loop_emits_realtime_tool_events():
    import blocks.chat.send as send

    calls = {"ai": 0}
    emitted = []

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "calculator",
                                "input": "{\"expression\":\"2+2\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "4"}],
                    "finish_reason": "stop",
                },
            }
        if name == "defaults.tool.invoke":
            return {"status": "ok", "data": {"result": "4"}}
        raise AssertionError(name)

    response = send._complete_with_tools(
        "openrouter/test-model",
        [{"role": "user", "content": "2+2"}],
        [{"type": "function", "function": {"name": "calculator", "parameters": {"type": "object"}}}],
        {"stream_event_callback": emitted.append},
        call_handler,
        {"max_tool_calls": 3},
    )

    assert response["content"][0]["text"] == "4"
    assert [event["type"] for event in emitted] == [
        "status",
        "status",
        "tool_call_started",
        "tool_call_completed",
    ]
    assert emitted[2]["tool_name"] == "calculator"


def test_chat_tool_loop_debug_mode_logs_ai_prompt_and_images(tmp_path):
    import base64
    import blocks.chat.send as send

    raw_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/azX2qkAAAAASUVORK5CYII="
    )
    data_url = "data:image/png;base64," + base64.b64encode(raw_png).decode("ascii")

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "done"}],
                    "finish_reason": "stop",
                },
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect this"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        [],
        {"conversation_workspace_dir": str(tmp_path), "ai_debug_enabled": True, "stream_event_callback": lambda _event: None},
        call_handler,
        {"max_tool_calls": 1},
    )

    debug = response["metadata"]["ai_debug"]
    log_path = Path(debug["request_logs"][0])
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    saved = payload["messages"][0]["content"][1]["image_url"]["url"]
    image_path = Path(saved["debug_image_path"])

    assert log_path.parent == tmp_path / "debug" / "ai_requests"
    assert payload["messages"][0]["content"][0]["text"] == "inspect this"
    assert saved["url"] == "[image data saved as artifact]"
    assert image_path.read_bytes() == raw_png
    assert payload["images"][0]["path"] == str(image_path)
    assert payload["response"]["content"][0]["text"] == "done"
    assert any(event["phase"] == "ai_debug" for event in response["events"])


def test_frontend_registry_exposes_ai_request_debug_setting(tmp_path):
    from domain.frontend.registry import FrontendRegistry

    settings = FrontendRegistry(tmp_path).get_settings()
    debug_sections = [section for section in settings["sections"] if section.get("id") == "debug"]

    assert debug_sections
    assert settings["values"]["debug"]["ai_request_logging"] is False
    assert any(field.get("id") == "ai_request_logging" for field in debug_sections[0]["fields"])


def test_chat_tool_loop_marks_nested_tool_errors_in_events():
    import blocks.chat.send as send

    calls = {"ai": 0}
    emitted = []

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "computer_use",
                                "input": "{\"action\":\"type\",\"text\":\"hello\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "ok",
                "data": {"content": [{"type": "text", "text": "handled"}], "finish_reason": "stop"},
            }
        if name == "defaults.tool.invoke":
            return {
                "status": "ok",
                "data": {
                    "result": "type failed",
                    "is_error": True,
                    "widget": {"is_error": True},
                },
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "openrouter/test-model",
        [{"role": "user", "content": "type hello"}],
        [{"type": "function", "function": {"name": "computer_use", "parameters": {"type": "object"}}}],
        {"stream_event_callback": emitted.append},
        call_handler,
        {"max_tool_calls": 3},
    )

    completed = [event for event in response["events"] if event["type"] == "tool_call_completed"][0]
    streamed_completed = [event for event in emitted if event["type"] == "tool_call_completed"][0]
    assert completed["is_error"] is True
    assert streamed_completed["is_error"] is True


def test_chat_tool_loop_preserves_tool_logs_when_ai_fails_after_tool_use():
    import blocks.chat.send as send

    calls = {"ai": 0}
    emitted = []

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "computer_use",
                                "input": "{\"action\":\"screenshot\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "error",
                "error": {"message": "Google API error 500: Internal error encountered."},
            }
        if name == "defaults.tool.invoke":
            return {
                "status": "ok",
                "data": {
                    "result": "computer_use computer.screenshot completed",
                    "path": "/tmp/shot.png",
                    "mime_type": "image/png",
                },
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "look at the screen"}],
        [{"type": "function", "function": {"name": "computer_use", "parameters": {"type": "object"}}}],
        {"stream_event_callback": emitted.append},
        call_handler,
        {"max_tool_calls": 3},
    )

    assert response["finish_reason"] == "ai_error_after_tool_use"
    assert response["metadata"]["ai_error_after_tool_use"] is True
    assert response["metadata"]["transient_ai_error"] is True
    assert len(response["tool_logs"]) == 1
    assert response["tool_logs"][0]["tool_name"] == "computer_use"
    assert [event["phase"] for event in emitted if event["type"] == "status"][-1] == "ai_error_after_tool_use"


def test_chat_tool_loop_stops_on_visible_window_required_recovery():
    import blocks.chat.send as send

    calls = {"ai": 0, "tool": 0}
    emitted = []

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            return {
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_chrome",
                            "name": "computer_use",
                            "input": "{\"action\":\"type\",\"text\":\"hello\",\"app\":\"Google Chrome\"}",
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            }
        if name == "defaults.tool.invoke":
            calls["tool"] += 1
            return {
                "status": "ok",
                "data": {
                    "result": "Background computer-use is disabled. Only currently visible windows can be operated.",
                    "is_error": True,
                    "recovery": {
                        "kind": "visible_window_required",
                        "note": "Show or focus the target app/window, then retry without background.",
                    },
                },
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "send hello in existing Chrome"}],
        [{"type": "function", "function": {"name": "computer_use", "parameters": {"type": "object"}}}],
        {"stream_event_callback": emitted.append},
        call_handler,
        {"max_tool_calls": 12},
    )

    assert calls == {"ai": 1, "tool": 1}
    assert response["finish_reason"] == "tool_blocked"
    assert response["metadata"]["tool_blocked"] is True
    assert response["metadata"]["tool_blocked_kind"] == "visible_window_required"
    assert "現在表示" in response["content"][0]["text"]
    assert [event["phase"] for event in emitted if event["type"] == "status"][-1] == "tool_blocked"


def test_chat_tool_loop_does_not_stop_when_context_reports_visible_only_notes():
    import blocks.chat.send as send

    calls = {"ai": 0, "tool": 0}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] > 1:
                return {
                    "status": "ok",
                    "data": {"content": [{"type": "text", "text": "context noted"}], "finish_reason": "stop"},
                }
            return {
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_context",
                            "name": "computer_use",
                            "input": "{\"action\":\"context\"}",
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            }
        if name == "defaults.tool.invoke":
            calls["tool"] += 1
            return {
                "status": "ok",
                "data": {
                    "result": "context",
                    "is_error": False,
                    "widget": {
                        "action": "computer.context",
                        "notes": ["Computer-use is app-generic and visible-screen only."],
                    },
                },
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "画面を切り替えず既存のGoogle ChromeのChatGPTにhelloを送って"}],
        [{"type": "function", "function": {"name": "computer_use", "parameters": {"type": "object"}}}],
        {},
        call_handler,
        {"max_tool_calls": 12},
    )

    assert calls == {"ai": 2, "tool": 1}
    assert response["finish_reason"] == "stop"
    assert response["content"][0]["text"] == "context noted"


def test_chat_tool_loop_does_not_stop_after_successful_foreground_input():
    import blocks.chat.send as send

    calls = {"ai": 0, "tool": 0}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] > 1:
                return {
                    "status": "ok",
                    "data": {"content": [{"type": "text", "text": "sent with fallback"}], "finish_reason": "stop"},
                }
            return {
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_type",
                            "name": "computer_use",
                            "input": "{\"action\":\"type\",\"text\":\"hello\",\"app\":\"Google Chrome\"}",
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            }
        if name == "defaults.tool.invoke":
            calls["tool"] += 1
            return {
                "status": "ok",
                "data": {
                    "result": "computer_use computer.type completed",
                    "is_error": False,
                    "widget": {
                        "action": "computer.type",
                        "executed": True,
                        "driver": "foreground_input",
                    },
                },
            }
        raise AssertionError(name)

    response = send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "visible Chromeにhello"}],
        [{"type": "function", "function": {"name": "computer_use", "parameters": {"type": "object"}}}],
        {},
        call_handler,
        {"max_tool_calls": 12},
    )

    assert calls == {"ai": 2, "tool": 1}
    assert response["finish_reason"] == "stop"
    assert response["content"][0]["text"] == "sent with fallback"


def test_tool_result_recovery_kind_infers_visible_window_error():
    import blocks.chat.send as send

    assert (
        send._tool_result_recovery_kind(
            {
                "status": "ok",
                "data": {
                    "result": (
                        "Background computer-use is disabled. "
                        "Only currently visible windows can be operated."
                    ),
                    "is_error": True,
                },
            }
        )
        == "visible_window_required"
    )


def test_chat_tool_loop_honors_stream_cancel_before_tool_execution():
    import blocks.chat.send as send

    cancelled = {"value": False}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            cancelled["value"] = True
            return {
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "calculator",
                            "input": "{\"expression\":\"2+2\"}",
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            }
        if name == "defaults.tool.invoke":
            raise AssertionError("tool should not run after cancellation")
        raise AssertionError(name)

    try:
        send._complete_with_tools(
            "openrouter/test-model",
            [{"role": "user", "content": "2+2"}],
            [{"type": "function", "function": {"name": "calculator", "parameters": {"type": "object"}}}],
            {"is_cancelled": lambda: cancelled["value"]},
            call_handler,
            {"max_tool_calls": 3},
        )
    except send._ChatCancelled:
        pass
    else:
        raise AssertionError("expected chat cancellation")


def test_chat_tool_loop_returns_text_when_tool_limit_reached():
    import blocks.chat.send as send

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            return {
                "status": "ok",
                "data": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_limit",
                            "name": "calculator",
                            "input": "{\"expression\":\"2+2\"}",
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            }
        if name == "defaults.tool.invoke":
            return {"status": "ok", "data": {"result": "4"}}
        raise AssertionError(name)

    response = send._complete_with_tools(
        "openrouter/test-model",
        [{"role": "user", "content": "keep using tools"}],
        [{"type": "function", "function": {"name": "calculator", "parameters": {"type": "object"}}}],
        {},
        call_handler,
        {"max_tool_calls": 1},
    )

    assert response["content"][0]["type"] == "text"
    assert "tool call の上限" in response["content"][0]["text"]
    assert response["metadata"]["max_tool_calls_reached"] is True
    assert response["metadata"]["pending_tool_uses"][0]["name"] == "calculator"


def test_chat_tool_loop_passes_execution_context_to_tool_invoke():
    import blocks.chat.send as send

    calls = {"ai": 0}
    captured_tool_payload = {}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "browser_use",
                                "input": "{\"action\":\"screenshot\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "done"}],
                    "finish_reason": "stop",
                },
            }
        if name == "defaults.tool.invoke":
            captured_tool_payload.update(payload)
            return {"status": "ok", "data": {"result": "screenshot ready"}}
        raise AssertionError(name)

    send._complete_with_tools(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "look at the screen"}],
        [{"type": "function", "function": {"name": "browser_use", "parameters": {"type": "object"}}}],
        {
            "conversation_id": "c1",
            "conversation_workspace_dir": "/tmp/rumi-c1",
            "profile_policy": {"selected_tools": ["browser_use"]},
        },
        call_handler,
        {"max_tool_calls": 2},
    )

    assert captured_tool_payload["tool_name"] == "browser_use"
    assert captured_tool_payload["context"]["conversation_id"] == "c1"
    assert captured_tool_payload["context"]["conversation_workspace_dir"] == "/tmp/rumi-c1"
    assert captured_tool_payload["context"]["capability_graph"]["tool_name"] == "browser_use"


def test_tool_invoke_merges_payload_context(monkeypatch):
    import blocks.tool.invoke as invoke

    result = invoke.run(
        {
            "tool_name": "calculator",
            "arguments": {"expression": "2+2"},
            "context": {
                "conversation_id": "c1",
                "conversation_workspace_dir": "/tmp/rumi-c1",
            },
        },
        {"request_id": "outer"},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"


def test_browser_screenshot_tool_result_adds_image_for_vision_models():
    import blocks.chat.send as send

    messages = []
    send._append_tool_result_message(
        messages,
        "browser_computer",
        {
            "status": "ok",
            "data": {
                "result": "screenshot",
                "action": "computer.screenshot",
                "data_url": "data:image/png;base64,aGVsbG8=",
                "image_size": {"width": 1440, "height": 900},
                "action_coordinate_system": {"width": 720, "height": 450, "x_range": [0, 719], "y_range": [0, 449]},
                "model_image_size": {"width": 640, "height": 400},
                "model_to_screen_scale": {"x": 2.25, "y": 2.25},
                "model_to_action_scale": {"x": 1.125, "y": 1.125},
            },
        },
        "call_1",
        model="google/gemma-4-31b-it",
    )

    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "call_1"
    assert messages[1]["role"] == "user"
    guidance = messages[1]["content"][0]["text"]
    assert "tool-output evidence for tool_call_id=call_1" in guidance
    assert "preceding tool result" in guidance
    assert "not a new user request" in guidance
    assert not any(
        imperative in guidance.lower()
        for imperative in ("refocus", "call screenshot", "request a fresh", "pass only", "do not return", "use source=")
    )
    assert messages[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,aGVsbG8="


def test_computer_context_tool_result_includes_widget_details_for_model():
    import blocks.chat.send as send

    messages = []
    send._append_tool_result_message(
        messages,
        "computer_use",
        {
            "status": "ok",
            "data": {
                "result": "computer_use computer.context completed",
                "is_error": False,
                "widget": {
                    "action": "computer.context",
                    "active_window": {"app": "Codex", "title": "Codex"},
                    "selected_app": {"name": "Vivaldi", "window_count": 1},
                    "open_apps": [{"name": "Vivaldi"}, {"name": "Google Chrome"}],
                },
            },
        },
        "call_context",
        model="google/gemma-4-31b-it",
    )

    assert messages[0]["role"] == "tool"
    assert "computer.context" in messages[0]["content"]
    assert "Vivaldi" in messages[0]["content"]
    assert "open_apps" in messages[0]["content"]


def test_browser_screenshot_guidance_is_neutral_tool_output_provenance():
    import blocks.chat.send as send

    guidance = send._browser_screenshot_guidance(
        {
            "status": "ok",
            "data": {
                "active_window": {"app": "Codex", "title": "Codex"},
                "selected_window": {"app": "Google Chrome", "title": "LINE Chat - Google Chrome"},
                "model_image_size": {"width": 1280, "height": 720},
            },
        },
        "call_screenshot",
    )

    assert guidance == (
        "Browser/computer screenshot tool-output evidence for tool_call_id=call_screenshot; "
        "it belongs to the preceding tool result and is not a new user request."
    )
    assert "Codex" not in guidance
    assert "Google Chrome" not in guidance


def test_tool_result_summary_mentions_foreground_window_mismatch():
    import blocks.chat.send as send

    summary = send._tool_result_summary(
        "computer_use",
        {
            "status": "ok",
            "data": {
                "active_window": {"app": "Codex", "title": "Codex"},
                "target_window": {"app": "Google Chrome", "title": "LINE Chat - Google Chrome"},
            },
        },
    )

    assert "Foreground: Codex | Codex" in summary
    assert "Selected target: Google Chrome | LINE Chat - Google Chrome" in summary


def test_browser_computer_windows_script_avoids_powershell_pid_variable(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    recorded = {}

    def fake_capture(self, script):
        recorded["script"] = script
        return "[]"

    controller = BrowserComputerController(artifact_root=tmp_path)
    monkeypatch.setattr(BrowserComputerController, "_run_powershell_capture", fake_capture)

    controller._windows_windows()

    assert "[uint32]$procId = 0" in recorded["script"]
    assert "[ref]$procId" in recorded["script"]
    assert "[ref]$pid" not in recorded["script"]


def test_browser_computer_active_window_script_avoids_powershell_pid_variable(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    recorded = {}

    def fake_capture(self, script):
        recorded["script"] = script
        return "{}"

    controller = BrowserComputerController(artifact_root=tmp_path)
    monkeypatch.setattr(BrowserComputerController, "_run_powershell_capture", fake_capture)

    controller._windows_active_window()

    assert "[uint32]$procId = 0" in recorded["script"]
    assert "[ref]$procId" in recorded["script"]
    assert "[ref]$pid" not in recorded["script"]


def test_browser_computer_run_powershell_capture_uses_utf8(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    recorded = {}

    class Completed:
        stdout = "[]"

    def fake_run(args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(browser_computer.shutil, "which", lambda _: "powershell")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    BrowserComputerController(artifact_root=tmp_path)._run_powershell_capture("Write-Output 'ok'")

    assert recorded["kwargs"]["encoding"] == "utf-8"
    assert recorded["kwargs"]["errors"] == "replace"
    assert recorded["args"][-1].startswith("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8")


def test_browser_screenshot_tool_result_respects_provider_attachment_opt_out(monkeypatch):
    import blocks.chat.send as send

    class FakeClient:
        def _runtime_model_matches(self, model):
            return [
                {
                    "capabilities": ["vision"],
                    "metadata": {"supports_attachments": False},
                }
            ]

    monkeypatch.setattr(send, "AIClient", FakeClient)
    messages = []
    send._append_tool_result_message(
        messages,
        "browser_computer",
        {
            "status": "ok",
            "data": {
                "result": "screenshot",
                "data_url": "data:image/png;base64,aGVsbG8=",
            },
        },
        "call_1",
        model="provider/no-attachments",
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "tool"


def test_attachment_image_blocks_validate_actual_data_url_bytes():
    import blocks.chat.send as send

    tiny_png = "data:image/png;base64,aGVsbG8="
    too_large_encoded = "A" * (((send.MAX_ATTACHMENT_IMAGE_BYTES + 1 + 2) // 3) * 4)
    too_large = "data:image/png;base64," + too_large_encoded

    blocks = send._attachment_image_blocks(
        [
            {"type": "image/png", "size": 1, "dataUrl": tiny_png},
            {"type": "image/png", "size": 1, "dataUrl": "data:image/png;base64,not valid"},
            {"type": "image/png", "size": 1, "dataUrl": too_large},
        ]
    )

    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"] == tiny_png


def test_browser_screenshot_tool_log_compacts_inline_image_data():
    import blocks.chat.send as send

    compact = send._compact_tool_log_value(
        {
            "status": "ok",
            "data": {
                "widget": {
                    "data_url": "data:image/jpeg;base64,abc123",
                    "model_image_path": "/tmp/screenshot-model.jpg",
                }
            },
        }
    )

    assert compact["data"]["widget"]["data_url"] == "[image data saved as artifact]"
    assert compact["data"]["widget"]["model_image_path"] == "/tmp/screenshot-model.jpg"
    assert send._compact_tool_log_value("see data:image/png;base64,abc123 now") == "see [image data saved as artifact] now"


def test_browser_computer_screenshot_result_includes_coordinate_metadata(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    screenshot = tmp_path / "screen.png"
    model_image = tmp_path / "screen-model.png"
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    screenshot.write_bytes(png_header + b"\x00\x00\x05\xa0\x00\x00\x03\x84")
    model_image.write_bytes(png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90")
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: {"x": 12, "y": 34, "origin": "top_left"}))
    monkeypatch.setattr(
        BrowserComputerController,
        "_action_coordinate_system",
        staticmethod(
            lambda system, image_size: {
                "origin": "top_left",
                "unit": "display_coordinate",
                "screen": "primary",
                "x": 0,
                "y": 0,
                "width": 720,
                "height": 450,
                "x_range": [0, 719],
                "y_range": [0, 449],
            }
        ),
    )

    result = BrowserComputerController()._screenshot_result(screenshot, model_image, "Darwin")

    assert result["image_size"] == {"width": 1440, "height": 900}
    assert result["model_image_size"] == {"width": 640, "height": 400}
    assert result["coordinate_system"]["origin"] == "top_left"
    assert result["coordinate_system"]["x_range"] == [0, 1439]
    assert result["action_coordinate_system"]["width"] == 720
    assert result["model_to_screen_scale"] == {"x": 2.25, "y": 2.25}
    assert result["model_to_action_scale"] == {"x": 1.125, "y": 1.125}
    assert result["model_to_action_scale_legacy"] is True
    assert result["screenshot_to_action_scale"] == {"x": 0.5, "y": 0.5}
    assert result["coordinate_contract"]["primary"] == "normalized_1000"
    assert result["coordinate_contract"]["input_fields"] == ["normalized_x", "normalized_y"]
    assert result["cursor"] == {"x": 12, "y": 34, "origin": "top_left"}
    assert result["cursor_move_contract"]["action"] == "move"
    assert result["recommended_next_actions"][:2] == ["computer.type", "computer.key"]
    assert "normal approval gates still apply" in result["input_guidance"]


def test_browser_computer_model_copy_uses_png(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    controller = BrowserComputerController(artifact_root=tmp_path)
    source = tmp_path / "screen.png"
    assert controller._write_png_rgba(source, 2, 2, bytearray([255, 255, 255, 255] * 4))
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Linux")

    model_path = controller._model_screenshot_copy(source)

    assert model_path.name == "screen-model.png"
    assert model_path.read_bytes().startswith(b"\x89PNG")
    assert controller._image_data_url(model_path).startswith("data:image/png;base64,")


def test_browser_computer_marker_preview_draws_red_marker_on_png(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    source = tmp_path / "screen-model.png"
    assert controller._write_png_rgba(source, 32, 32, bytearray([255, 255, 255, 255] * 32 * 32))

    marked = controller._marker_preview_image(
        source,
        {
            "model_image_size": {"width": 32, "height": 32},
            "image_size": {"width": 32, "height": 32},
            "action_coordinate_system": {"x": 0, "y": 0, "width": 32, "height": 32},
        },
        marker={"normalized_x": 500, "normalized_y": 500, "coordinate_space": "normalized_1000"},
    )

    assert marked is not None
    image = controller._read_png_rgba(marked)
    assert image is not None
    _width, _height, pixels = image
    center = (16 * 32 + 16) * 4
    assert pixels[center : center + 4] == bytearray([255, 0, 0, 255])


def test_browser_computer_zoom_crop_can_reuse_latest_screenshot(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    source = tmp_path / "latest.png"
    assert controller._write_png_rgba(source, 100, 50, bytearray([255, 255, 255, 255] * 100 * 50))
    controller._write_computer_state(
        {
            "last_screenshot": {
                "path": str(source),
                "image_size": {"width": 100, "height": 50},
                "action_coordinate_system": {"x": 10, "y": 20, "width": 100, "height": 50},
            }
        }
    )
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    path = tmp_path / "zoom.png"
    payload = {"source": "latest", "zoom": 2, "normalized_x": 500, "normalized_y": 500}
    capture = controller._capture_or_reuse_screenshot(path, payload)
    crop = controller._apply_screenshot_crop(path, payload, capture)

    assert crop is not None
    assert controller._image_size(crop["path"]) == (50, 25)
    assert crop["crop_reference"]["source"] == "latest_screenshot"
    assert crop["action_target"]["width"] == 50
    assert crop["action_target"]["height"] == 25

    model_path = controller._model_screenshot_copy(crop["path"])
    result = controller._screenshot_result(
        crop["path"],
        model_path,
        "Darwin",
        action_target=crop["action_target"],
        crop_reference=crop["crop_reference"],
    )
    assert result["coordinate_contract"]["crop_reference"]

    normal = dict(result)
    normal.pop("crop_reference", None)
    controller._remember_last_screenshot(normal)
    assert "crop_reference" not in controller._computer_state()["last_screenshot"]


def test_browser_computer_latest_crop_reuses_last_full_screenshot(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    source = tmp_path / "latest.png"
    pixels = bytearray()
    for y in range(80):
        for x in range(120):
            pixels.extend([x % 256, y % 256, 0, 255])
    assert controller._write_png_rgba(source, 120, 80, pixels)
    controller._write_computer_state(
        {
            "last_screenshot": {
                "path": str(source),
                "image_size": {"width": 120, "height": 80},
                "action_coordinate_system": {"x": 10, "y": 20, "width": 120, "height": 80},
            }
        }
    )
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    first_path = tmp_path / "first.png"
    first_payload = {"source": "latest", "crop_x": 10, "crop_y": 10, "crop_width": 20, "crop_height": 20}
    first_capture = controller._capture_or_reuse_screenshot(first_path, first_payload)
    first_crop = controller._apply_screenshot_crop(first_path, first_payload, first_capture)
    assert first_crop is not None
    first_model = controller._model_screenshot_copy(first_crop["path"])
    first_result = controller._screenshot_result(
        first_crop["path"],
        first_model,
        "Darwin",
        action_target=first_crop["action_target"],
        crop_reference=first_crop["crop_reference"],
    )
    assert first_result["path"].endswith("first-crop.png")
    assert controller._computer_state()["last_screenshot"]["path"].endswith("first-crop.png")
    assert controller._computer_state()["last_full_screenshot"]["path"] == str(source)

    second_path = tmp_path / "second.png"
    second_payload = {"source": "latest", "crop_x": 40, "crop_y": 10, "crop_width": 20, "crop_height": 20}
    second_capture = controller._capture_or_reuse_screenshot(second_path, second_payload)
    second_crop = controller._apply_screenshot_crop(second_path, second_payload, second_capture)

    assert second_crop is not None
    assert second_crop["crop_reference"]["source_path"] == str(source)
    assert second_crop["crop_reference"]["source_role"] == "last_full_screenshot"
    assert second_crop["crop_reference"]["source_is_crop"] is False
    assert controller._image_size(second_crop["path"]) == (20, 20)
    image = controller._read_png_rgba(second_crop["path"])
    assert image is not None
    _width, _height, second_pixels = image
    assert second_pixels[:4] == bytearray([40, 10, 0, 255])


def test_browser_computer_current_crop_allows_explicit_crop_of_crop(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    source = tmp_path / "latest.png"
    pixels = bytearray()
    for y in range(80):
        for x in range(120):
            pixels.extend([x % 256, y % 256, 0, 255])
    assert controller._write_png_rgba(source, 120, 80, pixels)
    controller._write_computer_state(
        {
            "last_screenshot": {
                "path": str(source),
                "image_size": {"width": 120, "height": 80},
                "action_coordinate_system": {"x": 10, "y": 20, "width": 120, "height": 80},
            }
        }
    )
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    first_path = tmp_path / "first.png"
    first_payload = {"source": "latest", "crop_x": 10, "crop_y": 10, "crop_width": 40, "crop_height": 30}
    first_capture = controller._capture_or_reuse_screenshot(first_path, first_payload)
    first_crop = controller._apply_screenshot_crop(first_path, first_payload, first_capture)
    assert first_crop is not None
    first_result = controller._screenshot_result(
        first_crop["path"],
        controller._model_screenshot_copy(first_crop["path"]),
        "Darwin",
        action_target=first_crop["action_target"],
        crop_reference=first_crop["crop_reference"],
    )
    assert first_result["path"].endswith("first-crop.png")

    second_path = tmp_path / "second.png"
    second_payload = {"source": "current_crop", "crop_x": 5, "crop_y": 4, "crop_width": 10, "crop_height": 8}
    second_capture = controller._capture_or_reuse_screenshot(second_path, second_payload)
    second_crop = controller._apply_screenshot_crop(second_path, second_payload, second_capture)

    assert second_crop is not None
    assert second_crop["crop_reference"]["source_path"] == first_result["path"]
    assert second_crop["crop_reference"]["source_role"] == "last_screenshot"
    assert second_crop["crop_reference"]["source_is_crop"] is True
    assert controller._image_size(second_crop["path"]) == (10, 8)
    image = controller._read_png_rgba(second_crop["path"])
    assert image is not None
    _width, _height, second_pixels = image
    assert second_pixels[:4] == bytearray([15, 14, 0, 255])


def test_browser_computer_nested_crop_aliases_are_applied(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    source = tmp_path / "latest.png"
    assert controller._write_png_rgba(source, 100, 50, bytearray([255, 255, 255, 255] * 100 * 50))
    controller._write_computer_state(
        {
            "last_screenshot": {
                "path": str(source),
                "image_size": {"width": 100, "height": 50},
                "action_coordinate_system": {"x": 0, "y": 0, "width": 100, "height": 50},
            }
        }
    )
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    path = tmp_path / "nested-crop.png"
    payload = {
        "source": "latest",
        "crop": {
            "crop_x": 10,
            "crop_y": 5,
            "crop_width": 20,
            "crop_height": 10,
        },
    }
    capture = controller._capture_or_reuse_screenshot(path, payload)
    crop = controller._apply_screenshot_crop(path, payload, capture)

    assert crop is not None
    assert controller._image_size(crop["path"]) == (20, 10)
    assert crop["crop_reference"]["source"] == "latest_screenshot"
    assert crop["crop_reference"]["box"] == {"x": 10, "y": 5, "width": 20, "height": 10}


def test_tool_activity_events_and_logs_redact_secret_values():
    import blocks.chat.send as send

    calls = {"ai": 0}

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            calls["ai"] += 1
            if calls["ai"] == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_secret",
                                "name": "secret_echo",
                                "input": "{\"api_key\":\"sk-live\",\"query\":\"ok\"}",
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                }
            return {
                "status": "ok",
                "data": {"content": [{"type": "text", "text": "done"}], "finish_reason": "stop"},
            }
        if name == "defaults.tool.invoke":
            return {"status": "ok", "data": {"token": "secret-token", "result": "safe"}}
        raise AssertionError(name)

    response = send._complete_with_tools(
        "stub/default",
        [{"role": "user", "content": "use tool"}],
        [{"type": "function", "function": {"name": "secret_echo", "parameters": {"type": "object"}}}],
        {},
        call_handler,
        {"max_tool_calls": 2},
    )
    started = [event for event in response["events"] if event["type"] == "tool_call_started"][0]
    completed = [event for event in response["events"] if event["type"] == "tool_call_completed"][0]
    log = response["tool_logs"][0]

    assert started["tool_call_id"] == "call_secret"
    assert completed["tool_call_id"] == "call_secret"
    assert started["arguments"]["api_key"] == "[redacted]"
    assert log["arguments"]["api_key"] == "[redacted]"
    assert log["result"]["data"]["token"] == "[redacted]"


def test_browser_screenshots_endpoint_is_conversation_and_owner_scoped(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.browser_screenshots import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    screenshot_path = store.conversation_workspace_dir("placeholder").parent / "placeholder.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(b"image-bytes")

    conversation = store.create_conversation(
        model="stub/default",
        metadata={"owner_user_id": "user-1"},
    )
    other = store.create_conversation(model="stub/default", metadata={"owner_user_id": "user-1"})
    screenshot_path = store.conversation_workspace_dir(conversation["id"]) / "tools" / "screen.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(b"image-bytes")
    assistant = store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "tool_logs": [
                {
                    "tool_name": "browser_computer",
                    "tool_call_id": "call_1",
                    "result": {
                        "data": {
                            "path": str(screenshot_path),
                            "action": "computer.click",
                            "click_marker": {"x": 10, "y": 20},
                            "image_size": {"width": 100, "height": 80},
                        }
                    },
                }
            ],
        },
    )

    ok_result = run(
        {
            "conversation_id": conversation["id"],
            "run_id": assistant["id"],
            "_headers": {"X-Rumi-User-Id": "user-1"},
        },
        {},
    )
    wrong_conversation = run(
        {
            "conversation_id": other["id"],
            "run_id": assistant["id"],
            "_headers": {"X-Rumi-User-Id": "user-1"},
        },
        {},
    )
    wrong_owner = run(
        {
            "conversation_id": conversation["id"],
            "run_id": assistant["id"],
            "_headers": {"X-Rumi-User-Id": "user-2"},
        },
        {},
    )

    assert ok_result["status"] == "ok"
    assert ok_result["data"]["screenshots"][0]["data_url"].startswith("data:image/png;base64,")
    assert ok_result["data"]["screenshots"][0]["click_marker"] == {"x": 10, "y": 20}
    assert ok_result["data"]["screenshots"][0]["image_size"] == {"width": 100, "height": 80}
    assert wrong_conversation["status"] == "error"
    assert wrong_conversation["error"]["code"] == "NOT_FOUND"
    assert wrong_owner["status"] == "error"
    assert wrong_owner["error"]["code"] == "FORBIDDEN"
    ChatStore._instance = None


def test_browser_screenshots_endpoint_omits_model_preview_duplicates(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from blocks.chat.browser_screenshots import run

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    workspace = store.conversation_workspace_dir(conversation["id"]) / "tools"
    workspace.mkdir(parents=True, exist_ok=True)
    screenshot_path = workspace / "screen.png"
    model_path = workspace / "screen-model.png"
    screenshot_path.write_bytes(b"image-bytes")
    model_path.write_bytes(b"model-image-bytes")
    assistant = store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "tool_logs": [
                {
                    "tool_name": "browser_companion",
                    "tool_call_id": "call_1",
                    "result": {
                        "data": {
                            "path": str(screenshot_path),
                            "model_image_path": str(model_path),
                            "action": "computer.screenshot",
                            "target_window": {"x": 80, "y": 40, "width": 1320, "height": 838},
                            "model_image_size": {"width": 640, "height": 406},
                            "image_size": {"width": 2640, "height": 1676},
                            "click_marker": {"x": 600, "y": 400, "screen_x": 680, "screen_y": 440},
                            "drag_marker": {
                                "from": {"screen_x": 410, "screen_y": 249},
                                "to": {"screen_x": 680, "screen_y": 440},
                            },
                        }
                    },
                }
            ],
        },
    )

    result = run({"conversation_id": conversation["id"], "run_id": assistant["id"]}, {})

    assert result["status"] == "ok"
    assert len(result["data"]["screenshots"]) == 1
    screenshot = result["data"]["screenshots"][0]
    assert screenshot["data_url"].startswith("data:image/png;base64,")
    assert screenshot["image_size"] == {"width": 640, "height": 406}
    assert screenshot["click_marker"]["coordinate_space"] == "model_image"
    assert screenshot["click_marker"]["x"] == 291
    assert screenshot["click_marker"]["y"] == 194
    assert screenshot["drag_marker"]["from"]["coordinate_space"] == "model_image"
    assert screenshot["drag_marker"]["from"]["x"] == 160
    assert screenshot["drag_marker"]["from"]["y"] == 101
    assert screenshot["drag_marker"]["to"]["x"] == 291
    assert screenshot["drag_marker"]["to"]["y"] == 194
    ChatStore._instance = None


def test_computer_use_auto_converts_latest_model_screenshot_coordinates(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    state = {
        "target_window": {"app": "Notion", "title": "Page", "x": 80, "y": 40, "width": 1320, "height": 838},
        "last_screenshot": {
            "model_image_size": {"width": 640, "height": 406},
            "action_coordinate_system": {"x": 80, "y": 40, "width": 1320, "height": 838},
        },
    }

    monkeypatch.setattr(controller, "_computer_state", lambda: dict(state))
    monkeypatch.setattr(controller, "_write_computer_state", lambda value: state.update(value))

    payload, marker = controller._resolve_action_point({"x": 320, "y": 203}, infer_window=False)

    assert payload["x"] == 741
    assert payload["y"] == 460
    assert marker == {"x": 320, "y": 203, "screen_x": 741, "screen_y": 460, "coordinate_space": "model_image"}
    assert state["ai_cursor"]["x"] == 741
    assert state["ai_cursor"]["y"] == 460


def test_computer_use_accepts_browser_tool_test_normalized_point(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    state = {
        "target_window": {"app": "Google Chrome", "title": "LINE", "x": 0, "y": 158, "width": 1470, "height": 798},
        "last_screenshot": {
            "image_size": {"width": 1470, "height": 798},
            "model_image_size": {"width": 640, "height": 347},
            "action_coordinate_system": {"x": 0, "y": 158, "width": 1470, "height": 798},
        },
    }

    monkeypatch.setattr(controller, "_computer_state", lambda: dict(state))
    monkeypatch.setattr(controller, "_write_computer_state", lambda value: state.update(value))

    payload, marker = controller._resolve_action_point(
        {"point": [750, 500], "coordinate_space": "normalized_1000"},
        infer_window=False,
    )

    assert payload["x"] == 734
    assert payload["y"] == 756
    assert payload["coordinate_space"] == "screen"
    assert marker["coordinate_space"] == "normalized_1000"
    assert marker["point_order"] == "yx"
    assert marker["screen_x"] == 734
    assert marker["screen_y"] == 756


def test_computer_use_normalized_click_uses_composite_chrome_frame(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    state = {
        "target_window": {
            "app": "Google Chrome",
            "title": "LINE Official Account Manager",
            "x": 0,
            "y": 37,
            "width": 1470,
            "height": 919,
            "capture_rect": {"x": 0, "y": 37, "width": 1470, "height": 919},
            "content_rect": {"x": 0, "y": 158, "width": 1470, "height": 798},
        },
        "last_screenshot": {
            "image_size": {"width": 2940, "height": 1838},
            "model_image_size": {"width": 640, "height": 400},
            "action_coordinate_system": {"x": 0, "y": 37, "width": 1470, "height": 919},
        },
    }

    monkeypatch.setattr(controller, "_computer_state", lambda: dict(state))
    monkeypatch.setattr(controller, "_write_computer_state", lambda value: state.update(value))

    payload, marker = controller._resolve_action_point(
        {"normalized_x": 235, "normalized_y": 20, "coordinate_space": "normalized_1000"},
        infer_window=False,
    )

    assert payload["x"] == 345
    assert payload["y"] == 55
    assert marker["screen_x"] == 345
    assert marker["screen_y"] == 55


def test_computer_use_normalized_coordinates_clamp_to_last_pixel(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    state = {
        "last_screenshot": {
            "action_coordinate_system": {"x": 20, "y": 40, "width": 101, "height": 51},
        },
    }

    monkeypatch.setattr(controller, "_computer_state", lambda: dict(state))
    monkeypatch.setattr(controller, "_write_computer_state", lambda value: state.update(value))

    payload, marker = controller._resolve_action_point(
        {"normalized_x": 1500, "normalized_y": -20, "coordinate_space": "normalized_1000"},
        infer_window=False,
    )

    assert payload["x"] == 120
    assert payload["y"] == 40
    assert marker["normalized_x"] == 1000
    assert marker["normalized_y"] == 0


def test_computer_use_normalized_click_prefers_attached_crop_over_target_window(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    state = {
        "target_window": {"app": "Google Chrome", "title": "LINE", "x": 0, "y": 158, "width": 1470, "height": 798},
        "last_screenshot": {
            "path": str(tmp_path / "crop.png"),
            "image_size": {"width": 300, "height": 200},
            "crop_reference": {"source": "latest_screenshot"},
            "action_coordinate_system": {"x": 98, "y": 242, "width": 148, "height": 85},
        },
    }

    monkeypatch.setattr(controller, "_computer_state", lambda: dict(state))
    monkeypatch.setattr(controller, "_write_computer_state", lambda value: state.update(value))

    payload, marker = controller._resolve_action_point(
        {"normalized_x": 1000, "normalized_y": 1000, "coordinate_space": "normalized_1000"},
        infer_window=False,
    )

    assert payload["x"] == 245
    assert payload["y"] == 326
    assert marker["screen_x"] == 245
    assert marker["screen_y"] == 326
    assert marker["reference"] == "last_screenshot"


def test_computer_use_drag_uses_virtual_cursor_and_converts_model_coordinates(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    state = {
        "target_window": {"app": "Notion", "title": "Page", "x": 80, "y": 40, "width": 1320, "height": 838},
        "last_screenshot": {
            "model_image_size": {"width": 640, "height": 406},
            "action_coordinate_system": {"x": 80, "y": 40, "width": 1320, "height": 838},
        },
    }

    monkeypatch.setattr(controller, "_computer_state", lambda: dict(state))
    monkeypatch.setattr(controller, "_write_computer_state", lambda value: state.update(value))

    result = controller.run(
        "computer.drag",
        {"x1": 100, "y1": 50, "x2": 320, "y2": 203, "include_screenshot": False},
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result["virtual_cursor"] is True
    assert result["target"] == {"from": {"x": 286, "y": 143}, "to": {"x": 741, "y": 460}}
    assert result["drag_marker"]["from"] == {
        "x": 100,
        "y": 50,
        "screen_x": 286,
        "screen_y": 143,
        "coordinate_space": "model_image",
    }
    assert result["drag_marker"]["to"] == {
        "x": 320,
        "y": 203,
        "screen_x": 741,
        "screen_y": 460,
        "coordinate_space": "model_image",
    }
    assert state["ai_cursor"]["x"] == 741
    assert state["ai_cursor"]["y"] == 460


def test_chat_store_splits_loaded_inline_thoughts(tmp_path, monkeypatch):
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    owner = ConversationStore("defaults", user_data_root=tmp_path)
    owner.create(
        {"id": "conv-1", "model_reference": "stub/default"},
        expected_revision=0,
    )
    owner.append_message(
        "conv-1",
        {
            "id": "msg-1",
            "role": "assistant",
            "content": [{"type": "text", "text": "<thought>hidden</thought>shown"}],
            "raw_text": "<thought>hidden</thought>shown",
        },
        expected_conversation_revision=1,
    )

    reloaded = ConversationStore("defaults", user_data_root=tmp_path).get("conv-1")
    message = reloaded["messages"][0]
    assert message["content"][0]["text"] == "<thought>hidden</thought>shown"
    assert message["raw_text"] == "<thought>hidden</thought>shown"


def test_builtin_calculator_returns_real_arithmetic_result():
    from domain.function_runtime.dispatcher import run_defaultspack_function

    result = run_defaultspack_function(
        "tool_calculator",
        {"expression": "2 + 2 * 3"},
        {"flow_id": "v4_pack_function_test"},
    )

    assert result == {
        "status": "ok",
        "data": {
            "result": "Calculated: 2 + 2 * 3 = 8",
            "is_error": False,
            "widget": None,
        },
    }


def test_coding_tools_are_exposed_through_tool_registry():
    from domain.tool.registry import ToolRegistry

    ToolRegistry._instance = None
    registry = ToolRegistry()
    names = {tool["tool_id"] for tool in registry.list_tools()}

    assert {
        "coding_file_read",
        "coding_file_write",
        "coding_file_patch",
        "coding_terminal_exec",
        "coding_git_status",
        "todo",
        "subagent",
        "browser_use",
        "computer_use",
    } <= names


def test_tool_executor_dispatches_coding_handler_with_yolo_policy(
    tmp_path,
    monkeypatch,
    defaultspack_capability_plan_context,
):
    from domain.tool.executor import ToolExecutor
    from domain.tool.registry import ToolRegistry

    ToolRegistry._instance = None
    monkeypatch.chdir(tmp_path)
    result = ToolExecutor().execute(
        "coding_file_create",
        {"path": "created.txt", "content": "hello"},
        defaultspack_capability_plan_context(
            "coding_file_create",
            profile_policy={"yolo_mode": True},
        ),
    )

    assert result == {
        "result": (
            "Capability execution failed: CapabilityExecutor is not bound; "
            "implicit executor creation is forbidden"
        ),
        "is_error": True,
        "widget": None,
    }
    assert not (tmp_path / "created.txt").exists()

    ToolRegistry._instance = None
    approval = ToolExecutor().execute(
        "coding_file_write",
        {"path": "needs-approval.txt", "content": "blocked"},
        defaultspack_capability_plan_context("coding_file_write"),
    )

    assert approval["is_error"] is False
    assert approval["widget"]["approval_required"] is True
    assert not (tmp_path / "needs-approval.txt").exists()


def test_coding_handlers_do_not_trust_body_approved_flag(tmp_path, monkeypatch):
    from blocks.coding.file_write import run as file_write_run
    from blocks.coding.terminal_exec import run as terminal_exec_run

    monkeypatch.chdir(tmp_path)

    write = file_write_run({"path": "pwned.txt", "content": "blocked", "approved": True}, {})
    assert write["status"] == "ok"
    assert write["data"]["approval_required"] is True
    assert not (tmp_path / "pwned.txt").exists()

    command = "python3 -c 'open(\"terminal-pwned.txt\", \"w\").write(\"blocked\")'"
    terminal = terminal_exec_run({"command": command, "approved": True}, {})
    assert terminal["status"] == "error"
    assert terminal["error"]["code"] == "EXEC_ERROR"
    assert "workspace_id" in terminal["error"]["message"]
    assert not (tmp_path / "terminal-pwned.txt").exists()


def test_coding_handlers_accept_only_server_approval_context(tmp_path, monkeypatch):
    from blocks.coding.file_write import run as file_write_run
    from domain.coding.workspace_store import WorkspaceStore
    from domain.tool_policy.internal_context import mark_tool_server_approval_context
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH",
        str(tmp_path / "coding_workspaces.json"),
    )
    WorkspaceStore().create(tmp_path, workspace_id="ws1", trusted=True)
    (tmp_path / "approved.txt").write_text("", encoding="utf-8")
    bind_verified_coding_contracts(monkeypatch, tmp_path, workspace_id="ws1")

    result = file_write_run(
        {"workspace_id": "ws1", "path": "approved.txt", "content": "ok"},
        mark_tool_server_approval_context({}),
    )

    assert result["status"] == "ok"
    assert result["data"]["written"] is True
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "ok"


def test_retired_direct_coding_route_is_not_registered(tmp_path, monkeypatch):
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    monkeypatch.chdir(tmp_path)
    server = DefaultsHttpServer(facade=None)
    assert server._match_route("POST", "/api/coding/files/write") == (
        None,
        None,
        None,
        None,
        None,
    )
    assert not (tmp_path / "direct-pwned.txt").exists()


def test_direct_coding_route_cannot_execute_with_forged_approved(tmp_path, monkeypatch):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert_profile_resolver_requires_authority_snapshot()
    assert_payload_mutations_denied(harness(tmp_path))


def test_sensitive_routes_do_not_use_wildcard_cors():
    from ecosystem.defaultspack.transport.http import _is_sensitive_http_path

    assert _is_sensitive_http_path("/api/coding/terminal/exec") is True
    assert _is_sensitive_http_path("/api/coding/files/write") is True
    assert _is_sensitive_http_path("/api/coding/approvals") is True
    assert _is_sensitive_http_path("/api/authority/requests/auth_1/approve") is True
    assert _is_sensitive_http_path("/api/browser/artifacts") is True
    assert _is_sensitive_http_path("/api/coding/agent/sessions") is True
    assert _is_sensitive_http_path("/api/integrations/secrets") is True
    assert _is_sensitive_http_path("/api/agent/self-improvement/status") is True
    assert _is_sensitive_http_path("/api/agent/self-improvement/run") is True
    assert _is_sensitive_http_path("/v1/conversations/c1/run-results/r1/browser-screenshots") is True
    assert _is_sensitive_http_path("/api/coding/files") is True
    assert _is_sensitive_http_path("/api/coding/files/read") is True
    assert _is_sensitive_http_path("/api/coding/files/search") is True
    assert _is_sensitive_http_path("/api/coding/files/diff") is True
    assert _is_sensitive_http_path("/api/chat/conversations/c1/run-results/r1/browser-screenshots") is False


def test_http_signal_wait_continues_after_non_interrupt_signal(monkeypatch):
    from ecosystem.defaultspack.transport import http as http_module

    calls = []

    def fake_pause():
        calls.append(1)
        if len(calls) >= 3:
            raise KeyboardInterrupt()

    monkeypatch.setattr(http_module.signal, "pause", fake_pause, raising=False)

    try:
        http_module._wait_for_signal()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("_wait_for_signal should propagate KeyboardInterrupt")

    assert len(calls) == 3


def test_pack_operation_fallback_registry_is_empty_and_host_routes_are_handler_owned():
    from ecosystem.defaultspack.transport.registry import (
        _FALLBACK_HTTP_ROUTE_SPECS,
        canonical_http_route_specs,
    )

    assert _FALLBACK_HTTP_ROUTE_SPECS == []
    routes = canonical_http_route_specs(include_always_available=True)
    assert routes
    assert all(spec.handler_name for spec in routes)
    assert all(
        not spec.block_module
        and not spec.fallback_block_module
        and not spec.legacy_block_module
        and not spec.function_id
        for spec in routes
    )
    retired_paths = {
        "/api/capabilities",
        "/api/agent-service/manifest",
        "/api/coding/files",
        "/api/coding/terminal/exec",
        "/api/chat/conversations/{id}/stop",
        "/api/agent/company/status",
        "/api/agent/mimo-company/status",
        "/api/share",
    }
    assert retired_paths.isdisjoint({spec.pattern for spec in routes})


def test_browser_computer_route_module_imports_and_delegates(monkeypatch):
    import importlib

    module = importlib.import_module("blocks.tool.browser_computer")
    calls = []

    def fake_run_computer_action(action, payload, context=None, **kwargs):
        calls.append((action, payload, context, kwargs))
        return {"handled": True}

    monkeypatch.setattr(module, "run_computer_action", fake_run_computer_action)

    result = module.run(
        {"action": "computer.screenshot", "payload": {"reason": "test"}},
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["widget"] == {"handled": True}
    model_context = json.loads(result["data"]["result"])["model_context"]
    assert model_context["action"] == "computer.screenshot"
    assert model_context["task_transition"]["next_phase"] == "interact_with_visible_target"
    assert calls == [("computer.screenshot", {"reason": "test"}, {}, {"tool_name": "browser_computer", "artifact_root": None, "yolo_mode": False})]


def test_stdio_rejects_retired_chat_stop_route_and_uds_preserves_wire_injection():
    from ecosystem.defaultspack.transport import stdio, uds

    assert stdio._match_route(
        "POST",
        "/api/chat/conversations/c-stop/stop",
    ) == (None, None, {})

    pattern, module_name, path_params = uds._match_route(
        "POST",
        "/api/chat/conversations/c-stop/stop",
    )
    assert pattern == "/api/chat/conversations/{id}/stop"
    assert module_name == "blocks.chat.stop"
    assert path_params == {"id": "c-stop"}
    assert uds._ID_INJECT_MAP[pattern] == ("conversation_id", "id")


def test_retired_operations_company_route_fails_closed_before_generic_status():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer(facade=None)
    assert server._match_route("GET", "/api/agent/company/status") == (
        None,
        None,
        None,
        None,
        None,
    )


def test_retired_mimo_company_route_fails_closed_before_generic_status():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer(facade=None)
    assert server._match_route("GET", "/api/agent/mimo-company/status") == (
        None,
        None,
        None,
        None,
        None,
    )


def test_standalone_http_chat_stream_fails_closed_without_v4_operation(monkeypatch):
    import ecosystem.defaultspack.transport.http as http_transport

    server = http_transport.DefaultsHttpServer(facade=None)
    captured = {}

    def fake_invoke_block(module_name, payload, context):
        captured["module_name"] = module_name
        captured["payload"] = payload
        captured["context"] = context
        return {"status": "ok", "data": {"_sse": True, "events": [{"type": "done"}]}}

    monkeypatch.setattr(http_transport, "invoke_block", fake_invoke_block)

    result = server._invoke_fallback_block(
        "blocks.chat.stream",
        {"message": {"role": "user", "content": "hello"}},
        {"id": "c-http"},
        {"id": "conversation_id"},
    )

    assert captured == {}
    assert result == {
        "status": "error",
        "error": {
            "code": "V4_OPERATION_UNAVAILABLE",
            "message": "Chat operation is absent from the captured Pack v4 catalog",
        },
    }
    assert http_transport._RequestHandler._sse_events_from_result(result) is None


def test_ui_clipboard_write_uses_local_clipboard(monkeypatch):
    from blocks.ui import clipboard

    captured = {}

    def fake_execute(contract_id, operation, payload, *, source_function_id, context):
        captured.update(
            {
                "contract_id": contract_id,
                "operation": operation,
                "payload": payload,
                "source_function_id": source_function_id,
                "context": context,
            }
        )
        return {"status": "ok", "data": {"written": True}}

    monkeypatch.setattr(clipboard, "execute_ui_host_contract", fake_execute)

    result = clipboard.run(
        {"content": "hello", "_headers": {"Origin": "http://127.0.0.1:8767"}},
        {},
    )
    denied = clipboard.run(
        {"content": "nope", "_headers": {"Origin": "https://example.com"}},
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["written"] is True
    assert captured["contract_id"] == clipboard.CLIPBOARD_WRITE
    assert captured["operation"] == "write"
    assert captured["payload"] == {"text": "hello"}
    assert denied["status"] == "error"
    assert denied["_http_status"] == 403


def test_transport_routes_match_captured_host_route_inventory():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs
    from ecosystem.defaultspack.transport.registry import compile_http_route_pattern

    server = DefaultsHttpServer(facade=None)
    canonical_routes = {
        (spec.method, spec.pattern)
        for spec in canonical_http_route_specs(include_always_available=True)
    }
    canonical_by_regex = {
        (spec.method, compile_http_route_pattern(spec.pattern).pattern): spec.pattern
        for spec in canonical_http_route_specs(include_always_available=True)
    }
    registered_routes = {
        (method, canonical_by_regex[(method, compiled.pattern)])
        for method, compiled, _handler, _source, _path_inject in server._routes
    }

    assert registered_routes == canonical_routes
    assert ("POST", "/api/coding/files/write") not in registered_routes
    assert ("GET", "/api/agent/company/status") not in registered_routes


def test_frontend_sidebar_uses_host_route_inventory_not_live_registry():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs

    class Facade:
        def get_interface(self, key, strategy=None):
            raise AssertionError("frontend transport must not read a live route registry")

    server = DefaultsHttpServer(Facade())
    expected = {
        (spec.method, spec.pattern)
        for spec in canonical_http_route_specs(include_always_available=True)
    }

    for method, path in expected:
        handler, _, source, _, _ = server._match_route(method, path)
        assert handler is not None, (method, path)
        assert source == "fallback"

    for method, path in (
        ("GET", "/api/artifacts"),
        ("POST", "/api/share"),
        ("GET", "/api/coding/files"),
        ("GET", "/api/agent/company/status"),
        ("GET", "/api/chat/channels"),
    ):
        assert server._match_route(method, path) == (
            None,
            None,
            None,
            None,
            None,
        )


def test_transport_direct_routes_json_has_interface_registry_parity():
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
    )
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert not (DEFAULTSPACK_ROOT / "routes.json").exists()
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


def test_frontend_sidebar_api_routes_match_in_registry_mode():
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
    )
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


def test_research_providers_use_shared_source_schema():
    from domain.research.providers import ExternalWebProvider, RedditProvider

    html = '<html><title>Example</title><a class="result__a" href="https://example.test">Example</a><div class="result__snippet">Snippet</div></html>'
    web = ExternalWebProvider(fetcher=lambda url, timeout: html)
    web_result = web.search("example", allow_network=True)

    assert web_result.sources[0]["type"] == "external_web"
    assert web_result.sources[0]["provider"] == "external_web"
    assert web.search("example", allow_network=False).network_enabled is False

    reddit_payload = '{"data":{"children":[{"data":{"id":"abc","title":"Hello","permalink":"/r/test/comments/abc/hello","subreddit":"test","score":3,"num_comments":2,"selftext":"Body"}}]}}'
    reddit = RedditProvider(fetcher=lambda url, timeout: reddit_payload)
    reddit_result = reddit.search("hello", subreddit="test")

    assert reddit_result.sources[0]["type"] == "reddit_post"
    assert reddit_result.sources[0]["provider"] == "reddit"
    assert reddit.search("hello", allow_network=False).network_enabled is False


def test_external_web_provider_parses_duckduckgo_lite_results():
    from domain.research.providers import ExternalWebProvider

    seen_urls = []
    html = """
    <html>
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&amp;rut=abc" class='result-link'>Example Docs</a>
      <td class='result-snippet'>Useful docs snippet.</td>
    </html>
    """
    provider = ExternalWebProvider(fetcher=lambda url, timeout: seen_urls.append(url) or html)

    result = provider.search("example docs", allow_network=True)

    assert "/lite/?" in seen_urls[0]
    assert result.sources[0]["title"] == "Example Docs"
    assert result.sources[0]["url"] == "https://example.com/docs"
    assert result.sources[0]["summary"] == "Useful docs snippet."


def test_external_web_provider_rejects_private_network_urls():
    from domain.research.providers import ExternalWebProvider

    result = ExternalWebProvider().search("http://127.0.0.1:8766/private", allow_network=True)

    assert result.sources == []
    assert "non-public" in result.summary


def test_external_web_provider_filters_domains_and_prefers_official_sources():
    from domain.research.providers import ExternalWebProvider

    html = """
    <html>
      <a class="result__a" href="https://blog.example.com/groq-compound">Unofficial Groq notes</a>
      <div class="result__snippet">Notes from a third party.</div>
      <a class="result__a" href="https://groq.com/docs/compound">Groq Compound Docs</a>
      <div class="result__snippet">Official docs.</div>
    </html>
    """
    provider = ExternalWebProvider(fetcher=lambda url, timeout: html)

    filtered = provider.search("groq compound docs", domains=["groq.com"], official_only=True)

    assert len(filtered.sources) == 1
    assert filtered.sources[0]["url"] == "https://groq.com/docs/compound"
    assert filtered.sources[0]["trust_level"] == "high"
    assert filtered.sources[0]["metadata"]["official"] is True


def test_external_web_provider_can_enrich_result_pages():
    from domain.research.providers import ExternalWebProvider

    def fake_fetch(url, timeout):
        if "duckduckgo.com" in url:
            return """
            <html>
              <a class="result__a" href="https://example.com/docs">Example Docs</a>
              <div class="result__snippet">Short snippet.</div>
            </html>
            """
        return "<html><title>Example Docs</title><body>Example Docs Detailed body text for the enriched summary.</body></html>"

    provider = ExternalWebProvider(fetcher=fake_fetch)
    result = provider.search("example docs", fetch_pages=True)

    assert result.sources[0]["title"] == "Example Docs"
    assert "Detailed body text" in result.sources[0]["summary"]
    assert result.sources[0]["metadata"]["enriched_from_page"] is True


def test_browser_computer_controller_gates_desktop_actions():
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()

    assert controller.run("browser.session")["action"] == "browser.session"
    assert controller.run("browser.session")["capabilities"]["cursor_move"] in {True, False}
    assert controller.run("browser.open_url", {"url": "https://example.test", "dry_run": True})["dry_run"] is True
    assert controller.run("computer.screenshot", {"dry_run": True})["requires_approval"] is False
    assert controller.run("computer.move", {"x": 1, "y": 2, "dry_run": True})["requires_approval"] is False
    approval = controller.run("computer.click", {"x": 1, "y": 2})
    assert approval["requires_approval"] is True
    assert "approval_token" not in approval
    assert controller.run("computer.click", {"x": 1, "y": 2, "approved": True})["requires_approval"] is True


def test_browser_computer_screenshot_is_read_only_without_approval(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(png_body)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    result = BrowserComputerController(artifact_root=tmp_path).run("computer.screenshot")

    assert result["action"] == "computer.screenshot"
    assert result.get("requires_approval") is True
    assert not any(tmp_path.glob("screenshot-*.png"))


def test_browser_computer_screenshot_uses_window_id_for_selected_macos_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "screencapture":
            Path(command[-1]).write_bytes(png_body)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_computer_state(
        {
            "target_window": {
                "app": "Google Chrome",
                "title": "ChatGPT - Google Chrome",
                "x": 50,
                "y": 80,
                "width": 1200,
                "height": 800,
                "window_id": 12345,
            }
        }
    )
    controller._active_window = lambda: None
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.screenshot", yolo_mode=True)

    assert result["action"] == "computer.screenshot"
    assert calls[0][:4] == ["screencapture", "-x", "-l", "12345"]
    assert result["target_window"]["window_id"] == 12345


def test_browser_computer_screenshot_uses_composite_capture_rect(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "screencapture":
            Path(command[-1]).write_bytes(png_body)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_computer_state(
        {
            "target_window": {
                "app": "Google Chrome",
                "title": "LINE Official Account Manager",
                "x": 0,
                "y": 37,
                "width": 1470,
                "height": 919,
                "window_id": 2811,
                "capture_rect": {"x": 0, "y": 37, "width": 1470, "height": 919},
                "content_rect": {"x": 0, "y": 158, "width": 1470, "height": 798},
                "capture_method": "rect",
            }
        }
    )
    controller._active_window = lambda: None
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.screenshot", yolo_mode=True)

    assert calls[0][:4] == ["screencapture", "-x", "-R", "0,37,1470,919"]
    assert result["target_window"]["capture_rect"]["y"] == 37
    assert result["action_coordinate_system"]["y"] == 37
    assert result["action_coordinate_system"]["height"] == 919


def test_browser_computer_screenshot_resolves_app_filter_without_prior_select(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "screencapture":
            Path(command[-1]).write_bytes(png_body)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True, "window_id": 111},
        {"app": "Google Chrome", "title": "LINE Chat", "x": 40, "y": 70, "width": 1200, "height": 780, "active": False, "window_id": 222},
    ]
    controller._active_window = lambda: {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900}
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.screenshot", {"app": "Google Chrome", "title": "LINE"}, yolo_mode=True)

    assert result["action"] == "computer.screenshot"
    assert calls[0][:4] == ["screencapture", "-x", "-l", "222"]
    assert result["target_window"]["app"] == "Google Chrome"
    assert result["target_window"]["title"] == "LINE Chat"
    assert controller._computer_state()["target_window"]["window_id"] == 222


def test_browser_computer_windows_screenshot_matches_title_when_app_label_is_missing(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"
    captured = {}

    def fake_windows_screenshot(self, path, target=None):
        captured["target"] = target
        Path(path).write_bytes(png_body)
        return {"x": 80, "y": 120, "width": 1280, "height": 720}

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(BrowserComputerController, "_windows_screenshot", fake_windows_screenshot)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True},
        {"app": "", "title": "LINE Chat - Google Chrome", "x": 80, "y": 120, "width": 1280, "height": 720, "active": False, "window_id": 987},
    ]
    controller._active_window = lambda: {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900}
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.screenshot", {"app": "Google Chrome", "title": "LINE"}, yolo_mode=True)

    assert result["action"] == "computer.screenshot"
    assert result["platform"] == "Windows"
    assert captured["target"]["title"] == "LINE Chat - Google Chrome"
    assert captured["target"]["window_id"] == 987
    assert result["target_window"]["title"] == "LINE Chat - Google Chrome"
    assert controller._computer_state()["target_window"]["window_id"] == 987


def test_browser_computer_windows_screenshot_matches_browser_process_alias(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"
    captured = {}

    def fake_windows_screenshot(self, path, target=None):
        captured["target"] = target
        Path(path).write_bytes(png_body)
        return {"x": 80, "y": 120, "width": 1280, "height": 720}

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(BrowserComputerController, "_windows_screenshot", fake_windows_screenshot)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True},
        {"app": "chrome", "title": "LINE Chat - Google Chrome", "x": 80, "y": 120, "width": 1280, "height": 720, "active": False, "window_id": 654},
    ]
    controller._active_window = lambda: {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900}
    controller._chrome_tabs = lambda: []

    result = controller.run("computer.screenshot", {"app": "Google Chrome", "title": "LINE"}, yolo_mode=True)

    assert result["action"] == "computer.screenshot"
    assert result["platform"] == "Windows"
    assert captured["target"]["app"] == "chrome"
    assert captured["target"]["title"] == "LINE Chat - Google Chrome"
    assert result["target_window"]["window_id"] == 654


def test_browser_computer_screenshot_title_contains_matches_non_chrome_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
    png_body = png_header + b"\x00\x00\x02\x80\x00\x00\x01\x90"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "screencapture":
            Path(command[-1]).write_bytes(png_body)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(BrowserComputerController, "_model_screenshot_copy", lambda self, path: path)
    monkeypatch.setattr(BrowserComputerController, "_cursor_position", staticmethod(lambda: None))

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True, "window_id": 111},
        {"app": "TextEdit", "title": "Project Notes", "x": 60, "y": 90, "width": 900, "height": 600, "active": False, "window_id": 444},
    ]
    controller._active_window = lambda: {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900}

    result = controller.run("computer.screenshot", {"title_contains": "Project Notes"}, yolo_mode=True)

    assert result["action"] == "computer.screenshot"
    assert calls[0][:4] == ["screencapture", "-x", "-l", "444"]
    assert result["target_window"]["app"] == "TextEdit"
    assert result["target_window"]["title"] == "Project Notes"


def test_browser_computer_select_window_title_contains_matches_non_chrome_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True, "window_id": 111},
        {"app": "TextEdit", "title": "Project Notes", "x": 60, "y": 90, "width": 900, "height": 600, "active": False, "window_id": 444},
    ]

    result = controller.run("computer.select_window", {"title_contains": "Project Notes", "focus": False}, yolo_mode=True)

    assert result["selected"] is True
    assert result["target_window"]["app"] == "TextEdit"
    assert result["target_window"]["title"] == "Project Notes"
    assert controller._computer_state()["target_window"]["window_id"] == 444


def test_browser_computer_title_filter_without_chrome_app_ignores_stale_chrome_target(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True, "window_id": 111},
    ]
    controller._chrome_tabs = lambda: [
        {"app": "Google Chrome", "window_index": 1, "tab_index": 2, "active": True, "title": "Project Notes", "url": "https://example.test/notes"}
    ]
    controller._write_sessions(
        {
            "chrome_target": {"app": "Google Chrome", "window_index": 1, "tab_index": 2, "title_contains": "Project Notes"},
            "last_url": "https://chatgpt.com/",
        }
    )

    selected = controller.run("computer.select_window", {"title_contains": "Project Notes", "focus": False}, yolo_mode=True)
    screenshot = controller.run("computer.screenshot", {"title_contains": "Project Notes"}, yolo_mode=True)

    assert selected["selected"] is False
    assert "chrome_target" not in selected
    assert screenshot["supported"] is True
    assert screenshot["is_error"] is True
    assert screenshot["error_code"] == "SCREENSHOT_TARGET_UNAVAILABLE"
    assert screenshot["target_resolved"] is False
    assert screenshot["capture_attempted"] is False
    assert "chrome_target" not in screenshot
    assert not [command for command in calls if command and command[0] == "screencapture"]


def test_browser_computer_chatgpt_title_does_not_imply_chrome(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Vivaldi", "title": "ChatGPT - Vivaldi", "x": 40, "y": 60, "width": 1000, "height": 800, "active": False},
    ]

    result = controller.run("computer.select_window", {"title_contains": "ChatGPT", "focus": False}, yolo_mode=True)

    assert result["selected"] is True
    assert result["target_window"]["app"] == "Vivaldi"
    assert "chrome_target" not in result


def test_browser_computer_apps_lists_open_and_installed_apps(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._running_apps = lambda: [
        {"name": "Vivaldi", "running": True, "active": True, "window_count": 1},
        {"name": "Visual Studio Code", "running": True, "active": False, "window_count": 2},
    ]
    controller._installed_apps = lambda payload=None: [
        {"name": "Vivaldi", "path": "/Applications/Vivaldi.app", "running": False},
        {"name": "TextEdit", "path": "/System/Applications/TextEdit.app", "running": False},
    ]

    running = controller.run("computer.apps")
    all_apps = controller.run("computer.apps", {"scope": "all", "include_installed": True})

    assert [app["name"] for app in running["open_apps"]] == ["Vivaldi", "Visual Studio Code"]
    assert any(app["name"] == "TextEdit" for app in all_apps["installed_apps"])
    assert any(app["name"] == "Visual Studio Code" for app in all_apps["apps"])


def test_browser_computer_select_app_targets_non_browser_app_without_focus(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    activated = []
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._running_apps = lambda: [
        {"name": "Vivaldi", "running": True, "active": True, "window_count": 1},
        {"name": "Visual Studio Code", "running": True, "active": False, "window_count": 2},
    ]
    controller._installed_apps = lambda payload=None: []
    controller._activate_app_name = lambda app_name: activated.append(app_name) or True

    result = controller.run("computer.select_app", {"app": "Studio Code", "focus": False}, yolo_mode=True)

    assert result["selected"] is True
    assert result["target_app"]["name"] == "Visual Studio Code"
    assert controller._computer_state()["target_app"]["name"] == "Visual Studio Code"
    assert activated == []


def test_browser_computer_show_app_focuses_matching_visible_window(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    focused = []
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True},
        {"app": "Vivaldi", "title": "ChatGPT - Vivaldi", "x": 40, "y": 60, "width": 1000, "height": 800, "active": False},
    ]
    controller._focus_window = lambda window: focused.append(window)
    controller._active_window = lambda: {
        "app": "Vivaldi",
        "title": "ChatGPT - Vivaldi",
        "x": 40,
        "y": 60,
        "width": 1000,
        "height": 800,
        "active": True,
    }

    result = controller.run("computer.show_app", {"app": "Vivaldi", "title": "ChatGPT"}, yolo_mode=True)

    assert result["shown"] is True
    assert result["target_window"]["app"] == "Vivaldi"
    assert controller._computer_state()["target_window"]["title"] == "ChatGPT - Vivaldi"
    assert focused[0]["app"] == "Vivaldi"


def test_browser_computer_windows_uses_full_windows_list_on_windows(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._windows_windows = lambda: [
        {"app": "Code", "title": "main.py - Visual Studio Code", "x": 10, "y": 20, "width": 900, "height": 700, "active": True},
        {"app": "Vivaldi", "title": "ChatGPT - Vivaldi", "x": 40, "y": 60, "width": 1000, "height": 800, "active": False},
    ]
    controller._windows_active_window = lambda: {"app": "Fallback", "title": "Fallback", "x": 0, "y": 0, "width": 200, "height": 200}

    result = controller.run("computer.windows")

    assert [window["app"] for window in result["windows"]] == ["Code", "Vivaldi"]


def test_browser_computer_screenshot_missing_app_filter_refuses_front_desktop(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True, "window_id": 111},
    ]

    result = controller.run("computer.screenshot", {"app": "Google Chrome", "title": "LINE"}, yolo_mode=True)

    assert result["supported"] is True
    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_TARGET_UNAVAILABLE"
    assert result["target_resolved"] is False
    assert result["capture_attempted"] is False
    assert not [command for command in calls if command and command[0] == "screencapture"]


def test_browser_computer_screenshot_ignores_hidden_browser_targets_even_with_fallback_flag(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True},
    ]
    controller._active_window = lambda: None
    controller._write_sessions(
        {
            "last_url": "https://chat.line.biz/chat",
            "chrome_target": {"app": "Google Chrome", "window_index": 1, "tab_index": 2, "title": "LINE Chat"},
        }
    )

    result = controller.run(
        "computer.screenshot",
        {"app": "Google Chrome", "title": "LINE", "allow_foreground_fallback": True},
        yolo_mode=True,
    )

    assert result["supported"] is True
    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_TARGET_UNAVAILABLE"
    assert result["target_resolved"] is False
    assert result["capture_attempted"] is False
    assert "chrome_target" not in result
    assert not [command for command in calls if command and command[0] == "screencapture"]


def test_browser_computer_screenshot_hidden_chrome_tab_reports_fallback_needed(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: [
        {"app": "Codex", "title": "RumiDP", "x": 0, "y": 0, "width": 1470, "height": 900, "active": True},
    ]
    controller._write_sessions(
        {
            "chrome_target": {"app": "Google Chrome", "window_index": 1, "tab_index": 2, "title": "LINE Chat"},
            "last_url": "https://chat.line.biz/chat",
        }
    )

    result = controller.run("computer.screenshot", {"app": "Google Chrome", "title": "LINE"}, yolo_mode=True)

    assert result["supported"] is True
    assert result["is_error"] is True
    assert result["error_code"] == "SCREENSHOT_TARGET_UNAVAILABLE"
    assert result["target_resolved"] is False
    assert result["capture_attempted"] is False
    assert "chrome_target" not in result
    assert "recovery" not in result


def test_browser_use_maps_cursor_move_to_browser_computer_payload():
    from domain.tool.executor import _browser_computer_action_payload

    action, payload = _browser_computer_action_payload(
        "browser_use",
        {"action": "move", "x": 120, "y": 240, "dry_run": True},
    )

    assert action == "computer.move"
    assert payload == {"x": 120, "y": 240, "dry_run": True}


def test_computer_use_payload_preserves_window_targeting_fields():
    from domain.tool.executor import _browser_computer_action_payload

    action, payload = _browser_computer_action_payload(
        "computer_use",
        {
            "action": "type",
            "text": "hello\n",
            "app": "Google Chrome",
            "title": "ChatGPT",
            "focus": False,
            "physical": False,
            "background": True,
            "method": "chrome_background",
            "driver": "auto",
            "allow_foreground_fallback": True,
            "allow_user_input_overlap": True,
            "modifier": "meta",
        },
    )

    assert action == "computer.type"
    assert payload == {
        "text": "hello\n",
        "app": "Google Chrome",
        "title": "ChatGPT",
        "focus": False,
        "physical": False,
        "method": "chrome_background",
        "driver": "auto",
        "modifier": "meta",
    }


def test_computer_use_local_action_map_supports_context_and_windows():
    from domain.tool.executor import _browser_computer_action_payload

    assert _browser_computer_action_payload("computer_use", {"action": "context"})[0] == "computer.context"
    assert _browser_computer_action_payload("computer_use", {"action": "windows"})[0] == "computer.windows"
    assert _browser_computer_action_payload("computer_use", {"action": "select_window"})[0] == "computer.select_window"
    assert _browser_computer_action_payload("computer_use", {"action": "drag"})[0] == "computer.drag"
    assert _browser_computer_action_payload("browser_use", {"action": "context"})[0] == "computer.context"
    assert _browser_computer_action_payload("browser_use", {"action": "windows"})[0] == "computer.windows"
    assert _browser_computer_action_payload("browser_use", {"action": "select_window"})[0] == "computer.select_window"
    assert _browser_computer_action_payload("browser_use", {"action": "drag"})[0] == "computer.drag"


def test_computer_use_payload_preserves_zoom_crop_fields():
    from domain.tool.executor import _browser_computer_action_payload

    action, payload = _browser_computer_action_payload(
        "computer_use",
        {
            "action": "screenshot",
            "source": "latest",
            "zoom": 3,
            "normalized_x": 420,
            "normalized_y": 510,
            "crop_x": 120,
            "crop_y": 180,
            "crop_width": 420,
            "crop_height": 260,
            "normalized_box": [100, 200, 700, 800],
            "detail": "high_detail",
        },
    )

    assert action == "computer.screenshot"
    assert payload["source"] == "latest"
    assert payload["zoom"] == 3
    assert payload["normalized_x"] == 420
    assert payload["normalized_y"] == 510
    assert payload["crop_x"] == 120
    assert payload["crop_y"] == 180
    assert payload["crop_width"] == 420
    assert payload["crop_height"] == 260
    assert payload["normalized_box"] == [100, 200, 700, 800]
    assert payload["detail"] == "high_detail"


def test_computer_use_function_wrapper_forwards_zoom_crop_fields(monkeypatch):
    from ecosystem.rumi_default_tools_pack.functions.computer_use import main as computer_use_main

    captured = {}

    def fake_run_browser_computer(context, request):
        captured["context"] = context
        captured["request"] = request
        return {"ok": True}

    monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

    result = computer_use_main.run(
        {"conversation_id": "conv-1"},
        {
            "action": "screenshot",
            "source": "latest",
            "zoom": 2,
            "normalized_x": 500,
            "normalized_y": 320,
            "crop_width": 360,
            "crop_height": 240,
        },
    )

    assert result == {"ok": True}
    assert captured["request"]["action"] == "computer.screenshot"
    assert captured["request"]["payload"]["source"] == "latest"
    assert captured["request"]["payload"]["zoom"] == 2
    assert captured["request"]["payload"]["normalized_x"] == 500
    assert captured["request"]["payload"]["normalized_y"] == 320
    assert captured["request"]["payload"]["crop_width"] == 360
    assert captured["request"]["payload"]["crop_height"] == 240


def test_browser_use_function_wrapper_forwards_zoom_crop_fields(monkeypatch):
    from ecosystem.rumi_default_tools_pack.functions.browser_use import main as browser_use_main

    captured = {}

    def fake_run_browser_computer(context, request):
        captured["request"] = request
        return {"ok": True}

    monkeypatch.setattr(browser_use_main, "_run_browser_computer", fake_run_browser_computer)

    browser_use_main.run(
        {},
        {
            "action": "screenshot",
            "source": "latest",
            "zoom": 2,
            "normalized_box": [200, 250, 800, 850],
            "detail": "high_detail",
        },
    )

    assert captured["request"]["action"] == "computer.screenshot"
    assert captured["request"]["payload"]["source"] == "latest"
    assert captured["request"]["payload"]["zoom"] == 2
    assert captured["request"]["payload"]["normalized_box"] == [200, 250, 800, 850]
    assert captured["request"]["payload"]["detail"] == "high_detail"


def test_computer_use_context_defaults_do_not_enable_background():
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.type",
        {"text": "hello", "app": "Google Chrome"},
        {
            "computer_use_background_preferred": True,
            "computer_use_allow_foreground_fallback": True,
        },
    )

    assert "background" not in payload
    assert "driver" not in payload
    assert "allow_foreground_fallback" not in payload
    assert "allow_user_input_overlap" not in payload


def test_computer_use_context_defaults_add_target_without_forcing_physical_click():
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.click",
        {"x": 20, "y": 30},
        {
            "user_requested_computer_use": True,
            "computer_use_target_app": "Google Chrome",
            "computer_use_target_title": "LINE",
        },
    )

    assert payload["app"] == "Google Chrome"
    assert payload["title"] == "LINE"
    assert "physical" not in payload

    explicit = _computer_use_payload_with_context_defaults(
        "computer.click",
        {"x": 20, "y": 30, "physical": True},
        {"user_requested_computer_use": True},
    )

    assert explicit["physical"] is True


def test_computer_use_context_defaults_do_not_override_explicit_show_app_target():
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.show_app",
        {"name": "Vivaldi"},
        {
            "computer_use_target_app": "Google Chrome",
            "computer_use_target_title": "ChatGPT",
        },
    )

    assert payload == {"name": "Vivaldi"}


def test_browser_open_url_uses_inferred_target_app():
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "browser.open_url",
        {"url": "https://chatgpt.com/"},
        {"computer_use_target_app": "Vivaldi", "computer_use_target_title": "ChatGPT"},
    )

    assert payload["app"] == "Vivaldi"
    assert "title" not in payload


def test_chat_text_does_not_set_background_preferences():
    import blocks.chat.send as send

    prefs = send._computer_use_preferences_from_text(
        "バックグラウンドでChrome操作。無理な場合はユーザー入力と被ってもいいのでOK。"
    )

    assert "computer_use_background_preferred" not in prefs
    assert "computer_use_allow_foreground_fallback" not in prefs
    assert "computer_use_background_required" not in prefs
    assert prefs["computer_use_target_app"] == "Google Chrome"


def test_chat_text_prefers_vivaldi_and_ignores_negated_chrome():
    import blocks.chat.send as send

    prefs = send._computer_use_preferences_from_text(
        "VivaldiでChatGPTを開いてhello。Google Chromeは使わないでください。"
    )

    assert prefs["computer_use_target_app"] == "Vivaldi"
    assert prefs["computer_use_target_title"] == "ChatGPT"


def test_chat_text_marks_explicit_mouse_keyboard_computer_use():
    import blocks.chat.send as send
    from domain.chat import run_request

    text = "Vivaldiをマウスとキーボードで操作してYouTubeを開いて"

    send_prefs = send._computer_use_preferences_from_text(text)
    run_prefs = run_request._computer_use_preferences_from_text(text)

    for prefs in (send_prefs, run_prefs):
        assert prefs["computer_use_target_app"] == "Vivaldi"
        assert prefs["computer_use_mouse_keyboard_requested"] is True
        assert prefs["computer_use_physical_clicks"] is True


def test_computer_use_runtime_prompt_requires_visible_mouse_keyboard_steps():
    from domain.chat.run_request import _computer_use_runtime_prompt

    prompt = _computer_use_runtime_prompt(
        {
            "user_requested_computer_use": True,
            "computer_use_target_app": "Vivaldi",
            "computer_use_mouse_keyboard_requested": True,
            "computer_use_physical_clicks": True,
        },
        [{"tool_id": "computer_use", "name": "computer_use"}],
    )

    assert "browser.open_url or app launch" in prompt
    assert "computer.type, computer.key, computer.click" in prompt
    assert "command+l" in prompt
    assert "physical=true" in prompt


def test_chat_text_sets_computer_use_chrome_line_target_preferences():
    import blocks.chat.send as send

    prefs = send._computer_use_preferences_from_text(
        "google chromeでLINEのチャット画面を開いてるのでメッセージを送って"
    )

    assert prefs["computer_use_target_app"] == "Google Chrome"
    assert prefs["computer_use_target_title"] == "LINE"


def test_user_requested_computer_use_requires_approval_for_interactive_actions(
    monkeypatch,
    defaultspack_capability_plan_context,
):
    from domain.tool.executor import ToolExecutor
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    def fake_run(self, action, payload=None, *, yolo_mode=False):
        raise AssertionError("browser_computer must not run before approval")

    monkeypatch.setattr(BrowserComputerController, "run", fake_run)

    result = ToolExecutor().execute(
        "browser_computer",
        {"action": "browser.open_url", "payload": {"url": "https://chatgpt.com"}},
        defaultspack_capability_plan_context(
            "browser_computer",
            user_requested_computer_use=True,
        ),
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["tool_name"] == "browser_computer"
    assert result["widget"]["risk_level"] == "high"
    assert result["widget"]["arguments"] == {
        "action": "browser.open_url",
        "payload": {
            "url": "https://chatgpt.com",
            "profile_id": "default",
            "persistent": False,
            "target_app": "",
        },
    }


def test_user_requested_computer_use_requires_approval_for_drag(
    monkeypatch,
    defaultspack_capability_plan_context,
):
    from domain.tool.executor import ToolExecutor
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    def fake_run(self, action, payload=None, *, yolo_mode=False):
        raise AssertionError("computer_use must not run before approval")

    monkeypatch.setattr(BrowserComputerController, "run", fake_run)

    result = ToolExecutor().execute(
        "computer_use",
        {"action": "drag", "x1": 10, "y1": 20, "x2": 30, "y2": 40},
        defaultspack_capability_plan_context(
            "computer_use",
            user_requested_computer_use=True,
        ),
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["tool_name"] == "computer_use"
    assert result["widget"]["risk_level"] == "high"
    assert result["widget"]["arguments"] == {
        "action": "computer.drag",
        "payload": {
            "x1": 10,
            "y1": 20,
            "x2": 30,
            "y2": 40,
        },
    }


def test_browser_computer_function_defaults_do_not_force_physical_click():
    from ecosystem.rumi_default_tools_pack.functions.browser_computer.main import _payload_with_context_defaults

    payload = _payload_with_context_defaults(
        "computer.click",
        {"x": 10, "y": 20},
        {"user_requested_computer_use": True},
    )

    assert payload == {"x": 10, "y": 20}


def test_browser_computer_executor_returns_approval_before_controller_errors(
    monkeypatch,
    defaultspack_capability_plan_context,
):
    from domain.tool.executor import ToolExecutor
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    def fake_run(self, action, payload=None, *, yolo_mode=False):
        raise AssertionError("computer_use controller must not run before approval")

    monkeypatch.setattr(BrowserComputerController, "run", fake_run)

    result = ToolExecutor().execute(
        "computer_use",
        {"action": "type", "text": "hello", "app": "Google Chrome"},
        defaultspack_capability_plan_context(
            "computer_use",
            user_requested_computer_use=True,
        ),
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["tool_name"] == "computer_use"
    assert result["widget"]["arguments"] == {
        "action": "computer.type",
        "payload": {"text": "hello", "app": "Google Chrome", "background": True},
    }


def test_browser_open_url_uses_foreground_default_browser(monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))

        class Process:
            pass

        return Process()

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "Popen", fake_popen)

    controller = BrowserComputerController()
    result = controller.run(
        "browser.open_url",
        {"url": "https://chatgpt.com", "persistent": False},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["managed_profile"] is False
    assert result["launch"]["mode"] == "default_browser"
    assert result["recommended_next_actions"][:2] == ["computer.type", "computer.key"]
    assert "normal approval gates still apply" in result["input_guidance"]
    assert "chrome_target" not in result
    assert "browser_target" not in result
    assert calls[0][0] == ["open", "https://chatgpt.com"]


def test_browser_open_url_can_target_vivaldi_foreground(monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        BrowserComputerController,
        "_active_window_for_app",
        lambda self, app_name: {"app": app_name, "title": "ChatGPT", "width": 900, "height": 700},
    )

    controller = BrowserComputerController()
    result = controller.run(
        "browser.open_url",
        {"url": "https://chatgpt.com", "persistent": False, "app": "Vivaldi"},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["managed_profile"] is False
    assert result["target_app"] == "Vivaldi"
    assert "browser_target" not in result
    assert "chrome_target" not in result
    open_calls = [call for call in calls if call[0] and call[0][0] == "open"]
    assert open_calls[0][0] == ["open", "-a", "Vivaldi", "https://chatgpt.com"]


def test_browser_open_url_approval_payload_target_app_runs_foreground(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        BrowserComputerController,
        "_active_window_for_app",
        lambda self, app_name: {"app": app_name, "title": "Gemini", "width": 900, "height": 700},
    )

    controller = BrowserComputerController()
    controller._approval_path = tmp_path / "shared" / "browser_computer_approvals.json"
    approval = controller.run(
        "browser.open_url",
        {"url": "https://gemini.google.com/app", "persistent": False, "app": "Google Chrome"},
    )

    assert approval["requires_approval"] is True
    assert approval["payload"]["target_app"] == "Google Chrome"

    result = controller.run(
        "browser.open_url",
        dict(approval["payload"]),
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["managed_profile"] is False
    assert result["target_app"] == "Google Chrome"
    assert calls[0][0] == ["open", "-a", "Google Chrome", "https://gemini.google.com/app"]


def test_browser_open_url_app_target_bypasses_managed_profile(monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        BrowserComputerController,
        "_active_window_for_app",
        lambda self, app_name: {"app": app_name, "title": "ChatGPT", "width": 900, "height": 700},
    )

    controller = BrowserComputerController()
    result = controller.run(
        "browser.open_url",
        {"url": "https://chatgpt.com", "app": "Vivaldi"},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["managed_profile"] is False
    assert result["persistent"] is True
    assert result["target_app"] == "Vivaldi"
    assert calls[0][0] == ["open", "-a", "Vivaldi", "https://chatgpt.com"]


def test_browser_open_url_specific_vivaldi_does_not_fall_back_to_default_browser(monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    def fake_run(command, **kwargs):
        raise OSError("missing app")

    opened = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(browser_computer.webbrowser, "open", lambda url: opened.append(url))

    result = BrowserComputerController().run(
        "browser.open_url",
        {"url": "https://chatgpt.com", "persistent": False, "app": "Vivaldi"},
        yolo_mode=True,
    )

    assert result["is_error"] is True
    assert result["opened"] is False
    assert result["target_app"] == "Vivaldi"
    assert opened == []


def test_browser_open_url_unknown_specific_app_uses_requested_app_only(monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        BrowserComputerController,
        "_active_window_for_app",
        lambda self, app_name: {"app": app_name, "title": "Start Page", "width": 900, "height": 700},
    )

    result = BrowserComputerController().run(
        "browser.open_url",
        {"url": "https://chatgpt.com", "persistent": False, "app": "Safari"},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["target_app"] == "Safari"
    assert calls[0][0] == ["open", "-a", "Safari", "https://chatgpt.com"]


def test_select_window_requires_visible_vivaldi_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    def fake_windows(self):
        return []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(BrowserComputerController, "_list_windows", fake_windows)

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"

    result = controller.run("computer.select_window", {"app": "Vivaldi", "url_contains": "chatgpt.com", "focus": False})

    assert result["selected"] is False
    assert "browser_target" not in result
    assert "chrome_target" not in result


def test_computer_type_rejects_background_chrome(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background request should not run")),
    )

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_sessions({"last_opened_background": True, "last_url": "https://chatgpt.com"})

    result = controller.run("computer.type", {"text": "hello", "background": True, "app": "Google Chrome"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["executed"] is False
    assert result["recovery"]["kind"] == "visible_window_required"


def test_computer_type_rejects_background_vivaldi(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background request should not run")),
    )

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_sessions(
        {
            "last_opened_background": True,
            "last_url": "https://chatgpt.com/",
            "browser_target": {"app": "Vivaldi", "url": "https://chatgpt.com/", "window_index": 3, "tab_index": 4},
        }
    )

    result = controller.run("computer.type", {"text": "hello", "background": True, "app": "Vivaldi"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["executed"] is False
    assert result["recovery"]["kind"] == "visible_window_required"


def test_computer_enter_rejects_background_vivaldi(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background request should not run")),
    )

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_sessions(
        {
            "last_opened_background": True,
            "last_url": "https://chatgpt.com/",
            "browser_target": {"app": "Vivaldi", "url": "https://chatgpt.com/", "window_index": 3, "tab_index": 4},
        }
    )

    result = controller.run("computer.key", {"key": "return", "background": True, "app": "Vivaldi"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["executed"] is False
    assert result["recovery"]["kind"] == "visible_window_required"


def test_background_vivaldi_reports_visible_window_recovery(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background request should not run")),
    )

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_sessions(
        {
            "last_opened_background": True,
            "last_url": "https://chatgpt.com/",
            "browser_target": {"app": "Vivaldi", "url": "https://chatgpt.com/", "window_index": 3, "tab_index": 4},
        }
    )

    result = controller.run(
        "computer.type",
        {"text": "hello", "background": True, "app": "Vivaldi", "allow_foreground_fallback": False},
        yolo_mode=True,
    )

    assert result["is_error"] is True
    assert result["recovery"]["kind"] == "visible_window_required"


def test_computer_type_ignores_last_opened_hidden_chrome_tab(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background request should not run")),
    )

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._write_sessions(
        {
            "last_opened_background": True,
            "last_url": "https://chatgpt.com/",
            "chrome_target": {"app": "Google Chrome", "url": "https://chatgpt.com/", "window_index": 2, "tab_index": 5},
        }
    )

    result = controller.run("computer.type", {"text": "hello", "background": True, "app": "Google Chrome"}, yolo_mode=True)

    assert result["is_error"] is True
    assert result["executed"] is False
    assert result["recovery"]["kind"] == "visible_window_required"
    assert "chrome_target" not in result


def test_macos_key_scripts_support_named_keys_and_modifiers():
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()

    assert controller._apple_script("computer.key", {"key": "return"}) == 'tell application "System Events" to key code 36'
    assert controller._apple_script("computer.key", {"key": "l", "modifier": "meta"}) == (
        'tell application "System Events" to keystroke "l" using {command down}'
    )


def test_computer_move_uses_cliclick_on_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.shutil, "which", lambda name: "/opt/homebrew/bin/cliclick" if name == "cliclick" else None)
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    controller = BrowserComputerController()
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"

    result = controller.run("computer.move", {"x": 120, "y": 240, "physical": True}, yolo_mode=True)

    assert result["executed"] is True
    assert result["target"] == {"x": 120, "y": 240}
    assert calls[0][0] == ["/opt/homebrew/bin/cliclick", "m:120,240"]


def test_computer_move_defaults_to_virtual_ai_cursor(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"

    result = controller.run("computer.move", {"x": 120, "y": 240}, yolo_mode=True)

    assert result["executed"] is True
    assert result["virtual_cursor"] is True
    assert result["target"] == {"x": 120, "y": 240}
    assert calls == []


def test_computer_click_physical_true_operates_visible_action(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    clicks = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(controller, "_darwin_click", lambda payload: clicks.append(dict(payload)))
    monkeypatch.setattr(controller, "_foreground_action_focus_error", lambda action, payload: None)

    result = controller.run(
        "computer.click",
        {"x": 120, "y": 240, "coordinate_space": "screen", "include_screenshot": False, "physical": True},
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert "virtual_cursor" not in result
    assert result["target"] == {"x": 120, "y": 240}
    assert clicks == [{"x": 120, "y": 240, "coordinate_space": "screen", "include_screenshot": False, "physical": True}]


def test_computer_click_can_preview_with_virtual_only(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    clicks = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(controller, "_darwin_click", lambda payload: clicks.append(dict(payload)))

    result = controller.run(
        "computer.click",
        {"x": 120, "y": 240, "coordinate_space": "screen", "virtual_only": True, "include_screenshot": False},
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result["virtual_cursor"] is True
    assert result["target"] == {"x": 120, "y": 240}
    assert clicks == []


def test_computer_type_returns_post_action_screenshot_by_default(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    scripts = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    def fake_run(command, **kwargs):
        scripts.append(command)
        return subprocess.CompletedProcess(command, 0)

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(controller, "_try_computer_seat_action", lambda action, payload, **kwargs: None)
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: True)
    monkeypatch.setattr(
        controller,
        "_capture_action_result_screenshot",
        lambda payload, marker, **kwargs: {"screenshot_path": str(tmp_path / "type.png"), "verification": {"kind": "post_action_screenshot"}},
    )

    result = controller.run("computer.type", {"text": "hello"}, yolo_mode=True)

    assert result["executed"] is True
    assert result["driver"] == "foreground_input"
    assert result["screenshot_path"].endswith("type.png")
    assert result["verification"]["kind"] == "post_action_screenshot"
    assert scripts and scripts[0][0] == "osascript"


def test_computer_click_uses_cliclick_on_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.shutil, "which", lambda name: "/opt/homebrew/bin/cliclick" if name == "cliclick" else None)
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    BrowserComputerController(artifact_root=tmp_path)._darwin_click({"x": 120, "y": 240})

    assert calls[0][0] == ["/opt/homebrew/bin/cliclick", "c:120,240"]


def test_computer_click_falls_back_to_swift_on_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_which(name):
        if name == "swift":
            return "/usr/bin/swift"
        return None

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == sys.executable:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.shutil, "which", fake_which)
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    BrowserComputerController(artifact_root=tmp_path)._darwin_click({"x": 120, "y": 240})

    assert calls[0][0][0] == sys.executable
    assert calls[0][0][1] == "-c"
    assert calls[1][0][0] == "/usr/bin/swift"
    assert calls[1][1]["timeout"] == browser_computer._DARWIN_CGEVENT_TIMEOUT_SECONDS
    assert "leftMouseDown" in calls[1][0][2]
    assert "CGPoint(x: 120, y: 240)" in calls[1][0][2]


def test_computer_type_applescript_preserves_non_ascii_text(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    script = BrowserComputerController(artifact_root=tmp_path)._apple_script(
        "computer.type",
        {"text": "こんにちは！"},
    )

    assert 'keystroke "こんにちは！"' in script
    assert "\\u3053" not in script


def test_computer_type_uses_clipboard_preserving_paste_for_non_ascii_on_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    BrowserComputerController(artifact_root=tmp_path)._darwin_type(
        {"text": "computer useの修正と検証が完了しました。"}
    )

    assert len(calls) == 1
    command = calls[0][0]
    assert command[:2] == ["osascript", "-e"]
    assert command[3:] == ["--", "computer useの修正と検証が完了しました。"]
    script = command[2]
    assert "set rumiPasteText to item 1 of argv" in script
    assert "set rumiOriginalClipboard to the clipboard" in script
    assert "set the clipboard to rumiPasteText" in script
    assert 'keystroke "v" using {command down}' in script
    assert "set the clipboard to rumiOriginalClipboard" in script
    assert 'keystroke "computer useの修正と検証が完了しました。"' not in script
    assert "\\u306e" not in " ".join(command)


def test_computer_type_uses_clipboard_paste_for_ascii_on_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    import ecosystem.rumi_default_tools_pack.domain.tool.browser_computer as browser_computer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    BrowserComputerController(artifact_root=tmp_path)._darwin_type({"text": "hello"})

    assert len(calls) == 1
    command = calls[0][0]
    assert command[:2] == ["osascript", "-e"]
    assert command[3:] == ["--", "hello"]
    script = command[2]
    assert "set the clipboard to rumiPasteText" in script
    assert 'keystroke "v" using {command down}' in script
    assert 'keystroke "hello"' not in script


def test_browser_computer_manages_persistent_profiles_and_cookie_jars(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._approval_path = tmp_path / "shared" / "browser_computer_approvals.json"
    controller._browser_root = tmp_path / "shared" / "browser"
    controller._profile_root = controller._browser_root / "profiles"

    created = controller.run("browser.profile.create", {"profile_id": "Work Login", "label": "Work Login"})
    assert created["profile"]["id"] == "work-login"
    assert created["active_profile_id"] == "work-login"
    assert Path(created["profile"]["profile_dir"]).exists()
    assert Path(created["profile"]["cache_dir"]).exists()

    imported = controller.run(
        "browser.cookies.import",
        {
            "profile_id": "work-login",
            "cookies": [
                {"name": "sid", "value": "secret-token", "domain": "example.test", "path": "/"},
            ],
        },
    )
    assert imported["count"] == 1

    listed = controller.run("browser.cookies.list", {"profile_id": "work-login"})
    assert listed["count"] == 1
    assert listed["cookies"][0]["value"] == "***"
    assert listed["cookies"][0]["value_redacted"] is True

    revealed = controller.run("browser.cookies.list", {"profile_id": "work-login", "include_values": True})
    assert revealed["cookies"][0]["value"] == "secret-token"

    dry_delete = controller.run("browser.cookies.delete", {"profile_id": "work-login", "name": "sid", "dry_run": True})
    assert dry_delete["matches"] == 1
    approval = controller.run("browser.cookies.delete", {"profile_id": "work-login", "name": "sid"})
    assert approval["requires_approval"] is True
    deleted = controller.run(
        "browser.cookies.delete",
        {"profile_id": "work-login", "name": "sid"},
        yolo_mode=True,
    )
    assert deleted["deleted"] == 1


def test_browser_open_url_uses_managed_profile_launch_plan(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._approval_path = tmp_path / "shared" / "browser_computer_approvals.json"
    controller._browser_root = tmp_path / "shared" / "browser"
    controller._profile_root = controller._browser_root / "profiles"
    fake_browser = tmp_path / "chrome"
    fake_browser.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_browser.chmod(0o755)
    controller._find_browser_executable = lambda: fake_browser

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "profile_id": "research", "dry_run": True},
    )

    assert result["requires_approval"] is False
    assert result["launch"]["mode"] == "managed_profile"
    assert result["launch"]["command"][0] == str(fake_browser)
    assert "--user-data-dir=" in result["launch"]["command"][1]
    assert "--disk-cache-dir=" in result["launch"]["command"][2]
    assert "--no-first-run" in result["launch"]["command"]
    assert "--no-default-browser-check" in result["launch"]["command"]
    assert "--disable-sync" in result["launch"]["command"]
    assert result["launch"]["command"][-1] == "https://example.test"


def test_browser_profile_cache_and_cookie_clear_are_approval_gated(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._approval_path = tmp_path / "shared" / "browser_computer_approvals.json"
    controller._browser_root = tmp_path / "shared" / "browser"
    controller._profile_root = controller._browser_root / "profiles"
    controller.run("browser.profile.create", {"profile_id": "managed"})
    cache_file = controller._profile_path("managed") / "cache" / "entry.bin"
    cache_file.write_bytes(b"cached")
    cookie_file = controller._profile_path("managed") / "managed_cookies.json"
    cookie_file.write_text('{"version":1,"cookies":[]}', encoding="utf-8")

    dry_cache = controller.run("browser.profile.clear_cache", {"profile_id": "managed", "dry_run": True})
    assert dry_cache["size_bytes"] == 6
    assert cache_file.exists()

    approval = controller.run("browser.profile.clear_cache", {"profile_id": "managed"})
    assert approval["requires_approval"] is True
    cleared = controller.run(
        "browser.profile.clear_cache",
        {"profile_id": "managed"},
        yolo_mode=True,
    )
    assert cleared["removed"]
    assert not cache_file.exists()

    cookie_approval = controller.run("browser.profile.clear_cookies", {"profile_id": "managed"})
    assert cookie_approval["requires_approval"] is True
    cleared_cookies = controller.run(
        "browser.profile.clear_cookies",
        {
            "profile_id": "managed",
            "include_managed": True,
        },
        yolo_mode=True,
    )
    assert str(cookie_file) in cleared_cookies["removed"]
    assert not cookie_file.exists()


def test_capability_detail_endpoint_returns_one_manifest_and_404_for_unknown():
    from blocks.capability.manifest import run

    result = run({"capability_id": "local_file"})
    assert result["status"] == "ok"
    assert result["data"]["id"] == "local_file"

    missing = run({"capability_id": "missing-capability"})
    assert missing["status"] == "error"
    assert missing["error"]["code"] == "NOT_FOUND"
    assert missing["_http_status"] == 404


def test_share_store_creates_lists_and_revokes_local_links(tmp_path):
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from domain.share.store import ShareStore

    ConversationStore("defaults", user_data_root=tmp_path).create(
        {"id": "c1", "model_reference": "stub/default"},
        expected_revision=0,
    )

    store = ShareStore(tmp_path)
    record = store.create({"target_type": "conversation", "target_id": "c1", "content": "hello"})

    assert record["share_url"].startswith("/share/")
    assert record["api_url"].startswith("/api/share/")
    shared_content = store.get(record["token"])["content"]
    assert shared_content["kind"] == "rumi.defaultspack.conversation_share"
    assert shared_content["preview"]["message_count"] == 0
    assert len(store.list()) == 1
    assert store.revoke(record["token"]) is True
    assert store.get(record["token"]) is None


def test_file_ops_diff_patch_snapshot_restore(tmp_path):
    from domain.coding.file_ops import FileOps

    ops = FileOps(tmp_path)
    ops.create_file("notes/example.txt", "hello world\n")

    diff = ops.diff_text("notes/example.txt", "hello rumi\n")
    assert "hello world" in diff
    assert "hello rumi" in diff

    patch = ops.apply_patch_text("notes/example.txt", "world", "rumi")
    assert patch["patched"] is True
    assert ops.read_file("notes/example.txt") == "hello rumi\n"

    snapshot = ops.snapshot(["notes/example.txt"])
    ops.write_file("notes/example.txt", "changed\n")
    restored = ops.restore_snapshot(snapshot["snapshot_id"], ["notes/example.txt"])
    assert restored["restored"] == ["notes/example.txt"]
    assert ops.read_file("notes/example.txt") == "hello rumi\n"


def test_terminal_exec_requires_approval_for_medium_risk_and_runs_read_only(tmp_path):
    from domain.coding.terminal import Terminal

    terminal = Terminal(tmp_path)

    read_only = terminal.execute("pwd", approved=False)
    assert read_only["exit_code"] == 0
    assert read_only["risk"]["risk_level"] == "low"

    medium = terminal.execute("python3 -c 'print(42)'", approved=False)
    assert medium["approval_required"] is True
    assert medium["exit_code"] is None

    approved = terminal.execute("python3 -c 'print(42)'", approved=True)
    assert approved["exit_code"] == 0
    assert approved["stdout"].strip() == "42"


def test_git_ops_returns_real_status_and_diff(tmp_path):
    from domain.coding.git_ops import GitOps

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "file.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("two\n", encoding="utf-8")

    git = GitOps(tmp_path)
    status = git.status()
    diff = git.diff()

    assert status["clean"] is False
    assert "file.txt" in status["modified"]
    assert "-one" in diff["diff"]
    assert "+two" in diff["diff"]


def test_artifact_store_is_local_and_versioned(tmp_path):
    from domain.artifact.store import ArtifactStore

    pack_root = tmp_path / "defaultspack"
    store = ArtifactStore(pack_root)
    artifact = store.create("markdown", "Plan", "# Plan\n", path="plans/plan.md", source_task="test")

    assert artifact["version"] == 1
    assert artifact["content_ref"] == "user_data/artifacts/plans/plan.md"
    assert store.list()[0]["artifact_id"] == artifact["artifact_id"]
    assert store.get(artifact["artifact_id"])["content"] == "# Plan\n"

    try:
        store.create("markdown", "Escape", "nope", path="../escape.md")
    except ValueError as exc:
        assert "escapes artifact root" in str(exc)
    else:
        raise AssertionError("artifact store allowed path traversal")

def test_chat_cancellation_register_keeps_pending_stop_request():
    """request_cancel before register should NOT be lost.

    stop.run calls request_cancel even when no streaming callback is
    registered yet.  The pending cancel flag must survive so that the
    next register() fires the callback immediately — otherwise the
    stop request is silently dropped.
    """
    from domain.chat.cancellation import ChatCancellationRegistry

    reg = ChatCancellationRegistry()
    called = []
    reg.request_cancel("conv_pending")
    reg.register("conv_pending", lambda: called.append(True))
    assert called == [True], f"pending stop was dropped: {called}"


def test_stop_run_skips_request_cancel_when_no_active_callbacks(tmp_path, monkeypatch):
    """stop.run must not pollute _cancelled when no callback is active.

    Without the has_callbacks guard, request_cancel marks the
    conversation as cancelled even when nothing is streaming,
    which causes the *next* turn's register() to fire immediately.
    """
    from domain.chat.cancellation import ChatCancellationRegistry, get_chat_cancellation_registry
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    reg = get_chat_cancellation_registry()
    # No callback registered → stop.run must NOT call request_cancel
    from blocks.chat.stop import run
    result = run({"conversation_id": conversation["id"]}, {})

    assert result["status"] == "ok"
    assert reg.is_cancelled(conversation["id"]) is False, (
        "request_cancel was called without active callbacks"
    )

    # Next turn: register should NOT fire immediately
    next_called = []
    reg.register(conversation["id"], lambda: next_called.append(True))
    assert next_called == [], f"cancel flag leaked to next turn: {next_called}"

    ChatStore._instance = None
