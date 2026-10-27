from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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


def _owner_storage_path(tmp_path):
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    return ConversationStore(
        "defaults",
        user_data_root=Path(os.environ["RUMI_TEST_CONVERSATION_OWNER_ROOT"]),
    ).path


def _add_messages(store, conversation_id, count):
    messages = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(
            store.add_message(
                conversation_id,
                {"role": role, "content": "message " + str(index)},
            )
        )
    return messages


def test_chat_store_reassigns_stale_append_sequence_numbers(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    first = store.add_message(conversation["id"], {"role": "user", "content": "first"})
    second = store.add_message(conversation["id"], {"role": "user", "content": "second"})

    assistant = store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "content": "reply to first",
            "parent_id": first["id"],
            "sequence_number": first["sequence_number"] + 1,
        },
    )

    assert second["sequence_number"] == 2
    assert assistant["sequence_number"] == 3
    assert [message["sequence_number"] for message in store.get_conversation(conversation["id"])["messages"]] == [1, 2, 3]


def test_chat_store_serializes_concurrent_stale_sequence_appends(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    first = store.add_message(conversation["id"], {"role": "user", "content": "first"})
    stale_sequence = first["sequence_number"] + 1

    def append_assistant(index):
        return store.add_message(
            conversation["id"],
            {
                "role": "assistant",
                "content": "parallel reply " + str(index),
                "parent_id": first["id"],
                "sequence_number": stale_sequence,
            },
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        appended = list(executor.map(append_assistant, range(16)))

    stored = store.get_conversation(conversation["id"])["messages"]
    sequences = [message["sequence_number"] for message in stored]

    assert all(message is not None for message in appended)
    assert sequences == list(range(1, 18))
    assert len(sequences) == len(set(sequences))
    assert sorted(message["sequence_number"] for message in appended) == list(range(2, 18))


def test_chat_store_normalizes_duplicate_sequence_numbers_on_load(tmp_path, monkeypatch):
    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    first = store.add_message(conversation["id"], {"role": "user", "content": "first"})
    store.add_message(conversation["id"], {"role": "assistant", "content": "first reply"})
    store.add_message(conversation["id"], {"role": "user", "content": "second"})
    storage_path = _owner_storage_path(tmp_path)
    payload = json.loads(storage_path.read_text(encoding="utf-8"))
    for message in payload["conversations"][conversation["id"]]["messages"]:
        if message["id"] != first["id"]:
            message["sequence_number"] = 2
    storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ChatStore = type(store)
    ChatStore._instance = None

    reloaded = ChatStore().get_conversation(conversation["id"])

    assert [message["sequence_number"] for message in reloaded["messages"]] == [1, 2, 3]


def test_chat_store_filters_and_sorts_pinned_conversations(tmp_path, monkeypatch):
    from blocks.chat.list_conversations import run as list_conversations

    store = _reset_chat_store(monkeypatch, tmp_path)
    workspace = store.create_conversation(
        model="stub/default",
        tags=["alpha", "shared"],
        metadata={"company_id": "co-1", "workspace_id": "ws-1", "workspace_label": "Alpha Workspace"},
        conversation_kind="chat",
        group_id="group-a",
    )
    older_pin = store.create_conversation(model="stub/default", tags=["shared"], metadata={"company_id": "co-2"})
    newer_pin = store.create_conversation(model="stub/default", tags=["shared"], metadata={"company_id": "co-2"})

    store.update_conversation(workspace["id"], {"title": "Workspace Notes"})
    store.add_message(workspace["id"], {"role": "user", "content": "message-only needle"})
    store.update_conversation(older_pin["id"], {"title": "Older Pin", "is_pinned": True, "pinned_at": 1000})
    store.update_conversation(newer_pin["id"], {"title": "Newer Pin", "is_pinned": True, "pinned_at": 2000})
    store.update_conversation(workspace["id"], {"title": "Workspace Notes Updated"})

    pinned, total = store.list_conversations(is_pinned=True)
    assert total == 2
    assert [item["id"] for item in pinned] == [newer_pin["id"], older_pin["id"]]

    filtered, total = store.list_conversations(
        tag="alpha",
        tags=["shared"],
        company_id="co-1",
        workspace_id="ws-1",
        conversation_kind="chat",
        group_id="group-a",
        query="Alpha Workspace",
    )
    assert total == 1
    assert filtered[0]["id"] == workspace["id"]

    no_message_match, total = store.list_conversations(query="message-only")
    assert total == 0
    message_match, total = store.list_conversations(query="message-only", include_messages=True)
    assert total == 1
    assert message_match[0]["id"] == workspace["id"]

    block_result = list_conversations(
        {"query": "message-only", "include_messages": "true", "is_archived": "false", "is_pinned": "false"},
        {},
    )
    assert block_result["status"] == "ok"
    assert block_result["data"]["total"] == 1
    assert block_result["data"]["conversations"][0]["id"] == workspace["id"]

    from domain.chat.store import ChatStore

    ChatStore._instance = None


def test_chat_store_projects_owner_pin_defaults(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    projected = ChatStore().get_conversation(conversation["id"])

    assert projected["is_pinned"] is False
    assert projected["pinned_at"] is None
    assert projected["pin_scope"] == "global"
    ChatStore._instance = None


def test_chat_store_reloads_external_conversation_index_updates(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    first_message = store.add_message(
        conversation["id"],
        {"role": "user", "content": [{"type": "text", "text": "original"}]},
    )
    storage_path = _owner_storage_path(tmp_path)
    payload = json.loads(storage_path.read_text(encoding="utf-8"))
    external_message = {
        "id": "external-assistant-message",
        "conversation_id": conversation["id"],
        "parent_id": first_message["id"],
        "children_ids": [],
        "sequence_number": 2,
        "role": "assistant",
        "content": [{"type": "text", "text": "external reply"}],
        "raw_text": "external reply",
        "created_at": first_message["created_at"] + 1,
        "finish_reason": None,
        "usage": None,
        "widget": None,
        "metadata": None,
        "events": None,
        "tool_logs": None,
    }
    external_conversation = payload["conversations"][conversation["id"]]
    external_conversation["messages"].append(external_message)
    external_conversation["current_node_id"] = external_message["id"]
    external_conversation["updated_at"] = first_message["created_at"] + 2
    payload["updated_at"] = first_message["created_at"] + 2
    storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.utime(storage_path, (time.time() + 2, time.time() + 2))

    reloaded = store.get_conversation(conversation["id"])
    assert reloaded["current_node_id"] == "external-assistant-message"
    assert [message["id"] for message in reloaded["messages"]][-1] == "external-assistant-message"

    added = store.add_message(conversation["id"], {"role": "user", "content": "after external"})
    updated = store.get_conversation(conversation["id"])
    assert added["parent_id"] == "external-assistant-message"
    assert [message["id"] for message in updated["messages"]][-2:] == ["external-assistant-message", added["id"]]
    ChatStore._instance = None


def test_chat_compact_protects_last_messages(tmp_path, monkeypatch):
    from blocks.chat.compact import run as compact
    from domain.chat.store import ChatStore

    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    messages = _add_messages(store, conversation["id"], 16)
    protected_ids = [message["id"] for message in messages[-4:]]

    result = compact({"conversation_id": conversation["id"], "protect_last_messages": 4}, {})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["deleted_message_ids"] == [message["id"] for message in messages[:-4]]
    updated = ChatStore().get_conversation(conversation["id"])
    assert [message["id"] for message in updated["messages"][-4:]] == protected_ids
    assert len(updated["messages"]) == 5
    assert updated["messages"][0]["metadata"]["compact"] is True
    ChatStore._instance = None


def test_auto_compact_suggest_is_non_destructive(tmp_path, monkeypatch):
    from blocks.chat.auto_compact import run as auto_compact
    from domain.chat.store import ChatStore

    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    messages = _add_messages(store, conversation["id"], 14)
    before_ids = [message["id"] for message in messages]

    result = auto_compact({"conversation_id": conversation["id"], "mode": "suggest", "protect_last_messages": 4}, {})

    assert result["status"] == "ok"
    assert result["data"]["compactable"] is True
    assert result["data"]["would_delete_message_ids"]
    after_ids = [message["id"] for message in ChatStore().get_conversation(conversation["id"])["messages"]]
    assert after_ids == before_ids
    ChatStore._instance = None


def test_auto_compact_apply_requires_approval_and_writes_summary_metadata(tmp_path, monkeypatch):
    from blocks.chat.auto_compact import run as auto_compact
    from domain.chat.store import ChatStore

    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    messages = _add_messages(store, conversation["id"], 14)
    protected_ids = [message["id"] for message in messages[-5:]]

    rejected = auto_compact({"conversation_id": conversation["id"], "mode": "apply", "protect_last_messages": 5}, {})
    assert rejected["status"] == "error"
    assert rejected["error"]["code"] == "APPROVAL_REQUIRED"

    result = auto_compact(
        {"conversation_id": conversation["id"], "mode": "apply", "approved": True, "protect_last_messages": 5},
        {},
    )

    assert result["status"] == "ok"
    summary = result["data"]["summary_message"]
    metadata = summary["metadata"]
    assert metadata["is_summary"] is True
    assert metadata["compact"] is True
    assert metadata["model"] == "stub/default"
    assert metadata["protect_last_messages"] == 5
    assert metadata["original_message_ids"] == result["data"]["deleted_message_ids"]
    assert metadata["compacted_at"]
    assert metadata["content_ref"].startswith("chat://conversations/")
    updated = ChatStore().get_conversation(conversation["id"])
    assert [message["id"] for message in updated["messages"][-5:]] == protected_ids
    ChatStore._instance = None


def test_compact_slash_command_uses_chat_compact_when_conversation_id_is_present(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.frontend.command_registry import SlashCommandRegistry

    store = _reset_chat_store(monkeypatch, tmp_path)
    conversation = store.create_conversation(model="stub/default")
    _add_messages(store, conversation["id"], 13)

    result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
        {
            "command": "compact",
            "mode": "chat",
            "conversation_id": conversation["id"],
            "args": {"protect_last_messages": 3},
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["executed"] is True
    assert result["data"]["result"]["deleted_count"] == 10
    assert result["data"]["result"]["summary_message"]["metadata"]["compact"] is True
    ChatStore._instance = None
