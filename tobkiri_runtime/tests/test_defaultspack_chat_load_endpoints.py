from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")


def _reset_chat_store(monkeypatch, tmp_path):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    return ChatStore()


def _conversation_with_messages(store, count):
    conversation = store.create_conversation(model="stub/default")
    message_ids = []
    for index in range(count):
        message = store.add_message(
            conversation["id"],
            {
                "id": f"m-{index}",
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"message {index}",
            },
        )
        message_ids.append(message["id"])
    return conversation, message_ids


def test_chat_store_get_conversation_window_returns_tail_and_offset(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation, message_ids = _conversation_with_messages(store, 8)

    tail, tail_window = store.get_conversation_window(conversation["id"], message_limit=3)
    offset_page, offset_window = store.get_conversation_window(
        conversation["id"],
        message_limit=2,
        message_offset=2,
    )

    assert [message["id"] for message in tail["messages"]] == message_ids[-3:]
    assert tail_window == {
        "offset": 5,
        "limit": 3,
        "returned": 3,
        "total": 8,
        "has_more_before": True,
        "has_more_after": False,
        "order": "chronological",
    }
    assert [message["id"] for message in offset_page["messages"]] == message_ids[2:4]
    assert offset_window == {
        "offset": 2,
        "limit": 2,
        "returned": 2,
        "total": 8,
        "has_more_before": True,
        "has_more_after": True,
        "order": "chronological",
    }


def test_chat_store_window_uses_conversation_file_before_large_index(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation, message_ids = _conversation_with_messages(store, 4)

    def fail_full_index_refresh(*_args, **_kwargs):
        raise AssertionError("single conversation reads should not list the full owner")

    monkeypatch.setattr(store, "_snapshot", fail_full_index_refresh)

    tail, window = store.get_conversation_window(conversation["id"], message_limit=2)

    assert [message["id"] for message in tail["messages"]] == message_ids[-2:]
    assert window["total"] == 4
    assert window["returned"] == 2


def test_compact_conversation_window_keeps_large_response_bounded():
    from domain.chat.public_metadata import (
        DEFAULT_CONVERSATION_MESSAGE_LIMIT,
        compact_conversation_for_response,
    )

    long_text = "x" * 50000
    messages = []
    for index in range(DEFAULT_CONVERSATION_MESSAGE_LIMIT):
        messages.append(
            {
                "id": f"m-{index}",
                "role": "assistant",
                "content": [{"type": "text", "text": long_text}],
                "raw_text": long_text,
                "metadata": {
                    "trace": long_text,
                    "trace_items": [long_text for _ in range(20)],
                },
                "events": [
                    {"event": "step", "index": event_index, "detail": long_text}
                    for event_index in range(20)
                ],
                "tool_logs": [
                    {"tool": "shell", "index": log_index, "output": long_text}
                    for log_index in range(12)
                ],
            }
        )

    compact = compact_conversation_for_response(
        {
            "id": "large-conversation",
            "title": "Large Conversation",
            "messages": messages,
        },
        messages_window={
            "offset": 538,
            "limit": DEFAULT_CONVERSATION_MESSAGE_LIMIT,
            "returned": DEFAULT_CONVERSATION_MESSAGE_LIMIT,
            "total": 658,
            "has_more_before": True,
            "has_more_after": False,
            "order": "chronological",
        },
    )

    encoded = json.dumps(compact, ensure_ascii=False).encode("utf-8")
    sample = compact["messages"][0]

    assert len(encoded) < 1024 * 1024
    assert compact["message_count"] == 658
    assert compact["messages_truncated"] is True
    assert compact["messages_window"]["returned"] == DEFAULT_CONVERSATION_MESSAGE_LIMIT
    assert compact["messages_window"]["truncated"] is True
    assert "[truncated" in sample["content"][0]["text"]
    assert "[truncated" in sample["raw_text"]
    assert len(sample["events"]) <= 6
    assert len(sample["tool_logs"]) <= 4
    assert sample["metadata"]["public_response"]["truncated"] is True
    assert {
        "content",
        "raw_text",
        "metadata",
        "events",
        "tool_logs",
    }.issubset(set(sample["metadata"]["public_response"]["fields"]))


def test_chat_get_conversation_respects_query_params_window_controls(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation, message_ids = _conversation_with_messages(store, 5)

    from blocks.chat import get_conversation

    paged = get_conversation.run(
        {
            "conversation_id": conversation["id"],
            "_query_params": {
                "message_limit": "2",
                "message_offset": "1",
            },
        },
        {},
    )
    without_messages = get_conversation.run(
        {
            "conversation_id": conversation["id"],
            "_query_params": {"include_messages": "false"},
        },
        {},
    )

    assert paged["status"] == "ok"
    assert [message["id"] for message in paged["data"]["messages"]] == message_ids[1:3]
    assert paged["data"]["messages_window"] == {
        "offset": 1,
        "limit": 2,
        "returned": 2,
        "total": 5,
        "has_more_before": True,
        "has_more_after": True,
        "order": "chronological",
        "truncated": True,
    }
    assert without_messages["status"] == "ok"
    assert without_messages["data"]["messages"] == []
    assert without_messages["data"]["messages_window"]["limit"] == 0
    assert without_messages["data"]["messages_window"]["returned"] == 0
    assert without_messages["data"]["messages_window"]["total"] == 5
    assert without_messages["data"]["messages_truncated"] is True


def test_chat_get_conversation_marks_stale_mimo_company_chat_for_redirect(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    state_path = tmp_path / "mimo" / "state.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(state_path))

    active = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    stale = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    store.update_conversation(active["id"], {"title": "MiMo Coding Company"})
    store.update_conversation(stale["id"], {"title": "[stale] MiMo Coding Company"})
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"conversation_id": active["id"]}), encoding="utf-8")

    from blocks.chat import get_conversation

    stale_result = get_conversation.run({"conversation_id": stale["id"]}, {})
    active_result = get_conversation.run({"conversation_id": active["id"]}, {})

    assert stale_result["status"] == "ok"
    stale_metadata = stale_result["data"]["metadata"]
    assert stale_metadata["superseded"] is True
    assert stale_metadata["superseded_reason"] == "mimo_coding_company_inactive_chat"
    assert stale_metadata["active_conversation_id"] == active["id"]
    assert stale_metadata["replacement_conversation_id"] == active["id"]
    assert active_result["status"] == "ok"
    assert active_result["data"]["metadata"].get("superseded") is not True


def test_http_safe_get_chat_ui_routes_use_block_fallback(monkeypatch):
    import transport.http as transport_http

    server = transport_http.DefaultsHttpServer.__new__(transport_http.DefaultsHttpServer)
    server.facade = object()
    calls = []

    def fake_invoke_block(module_name, payload, context):
        calls.append((module_name, dict(payload), dict(context)))
        return {"status": "ok", "data": {"module": module_name}}

    monkeypatch.setattr(transport_http, "invoke_block", fake_invoke_block)

    detail = server._invoke_function_route(
        "defaultspack:chat_get_conversation",
        {"_actual_method": "GET", "conversation_id": "c1"},
        {},
        fallback_block_module="blocks.chat.get_conversation",
    )
    preview = server._invoke_fallback_block(
        "blocks.ui.conversation_preview",
        {"_actual_method": "GET", "conversation_id": "c1"},
        {},
    )
    settings = server._invoke_fallback_block(
        "blocks.ui.settings",
        {"_actual_method": "GET"},
        {},
    )

    assert detail["status"] == "ok"
    assert preview["status"] == "ok"
    assert settings["status"] == "ok"
    assert [call[0] for call in calls] == [
        "blocks.chat.get_conversation",
        "blocks.ui.conversation_preview",
        "blocks.ui.settings",
    ]
    assert calls[0][2]["_defaultspack_http_route_adapter"] is True
    assert calls[1][2]["_defaultspack_http_route_adapter"] is True
    assert calls[2][2]["_defaultspack_http_route_adapter"] is True
