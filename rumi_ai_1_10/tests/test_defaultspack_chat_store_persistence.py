from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_chat_store_startup_skips_best_effort_history_backfill_when_disk_is_full(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": 0,
                "conversations": {
                    "conv-1": {
                        "id": "conv-1",
                        "title": "Recovered Conversation",
                        "created_at": 0,
                        "updated_at": 0,
                        "model": "stub/default",
                        "messages": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    def fail_backfill(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ChatStore, "_save_conversation_files", fail_backfill)

    store = ChatStore()

    assert store.get_conversation("conv-1")["title"] == "Recovered Conversation"
    ChatStore._instance = None


def test_chat_store_update_saves_only_changed_conversation_history(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    first = store.create_conversation(model="stub/default")
    second = store.create_conversation(model="stub/default")
    saved_conversation_ids = []

    def record_save(self, conversation_id, conversation):
        saved_conversation_ids.append(str(conversation_id))

    monkeypatch.setattr(ChatStore, "_save_conversation_file", record_save)

    updated = store.update_conversation(first["id"], {"title": "Only First Updated"})

    assert updated["title"] == "Only First Updated"
    assert saved_conversation_ids == [first["id"]]
    assert second["id"] not in saved_conversation_ids
    ChatStore._instance = None


def test_chat_store_list_conversations_omits_full_messages_by_default(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    store.add_message(conversation["id"], {"role": "user", "content": [{"type": "text", "text": "hello world"}]})

    listed, total = store.list_conversations(limit=10, include_messages=False)

    assert total == 1
    assert listed[0]["id"] == conversation["id"]
    assert listed[0]["messages"] == []
    assert listed[0]["message_count"] == 1
    assert listed[0]["last_message_preview"] == "hello world"
    ChatStore._instance = None


def test_chat_store_update_replaces_client_supplied_icon_svg(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    payload = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'onload="globalThis.__rumi_xss=1"></svg>'
    )

    updated = store.update_conversation(
        conversation["id"],
        {"metadata": {"icon_svg": payload, "workspace_label": "Local"}},
    )

    assert updated["metadata"]["workspace_label"] == "Local"
    assert updated["metadata"]["icon_svg"] != payload
    assert "onload" not in updated["metadata"]["icon_svg"].lower()
    ChatStore._instance = None


def test_chat_store_load_replaces_persisted_icon_svg(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'onload="globalThis.__rumi_xss=1"></svg>'
    )
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": 0,
                "conversations": {
                    "conv-1": {
                        "id": "conv-1",
                        "title": "Recovered Conversation",
                        "created_at": 0,
                        "updated_at": 0,
                        "model": "stub/default",
                        "metadata": {"icon_svg": payload, "workspace_label": "Local"},
                        "messages": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    conversation = ChatStore().get_conversation("conv-1")

    assert conversation["metadata"]["workspace_label"] == "Local"
    assert conversation["metadata"]["icon_svg"] != payload
    assert "onload" not in conversation["metadata"]["icon_svg"].lower()
    ChatStore._instance = None


def test_chat_store_concurrent_stale_sequence_appends_allocate_tail_numbers(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    user_message = store.add_message(
        conversation["id"],
        {"role": "user", "content": [{"type": "text", "text": "kickoff"}]},
    )
    stale_sequence = int(user_message["sequence_number"]) + 1
    start = threading.Event()

    def append_assistant(index):
        start.wait(timeout=5)
        return store.add_message(
            conversation["id"],
            {
                "role": "assistant",
                "parent_id": user_message["id"],
                "sequence_number": stale_sequence,
                "content": [{"type": "text", "text": f"reply {index}"}],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append_assistant, index) for index in (1, 2)]
        start.set()
        results = [future.result(timeout=5) for future in futures]

    stored = store.get_conversation(conversation["id"])
    sequences = [message["sequence_number"] for message in stored["messages"]]

    assert sorted(message["sequence_number"] for message in results) == [2, 3]
    assert sequences == [1, 2, 3]
    assert len(set(sequences)) == len(sequences)
    ChatStore._instance = None


def test_chat_store_load_repairs_duplicate_and_out_of_order_sequences_in_append_order(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": 0,
                "conversations": {
                    "conv-1": {
                        "id": "conv-1",
                        "title": "Recovered Conversation",
                        "created_at": 0,
                        "updated_at": 0,
                        "model": "stub/default",
                        "messages": [
                            {"id": "m1", "role": "user", "content": "one", "sequence_number": 1},
                            {"id": "m2", "role": "assistant", "content": "two", "sequence_number": 3},
                            {"id": "m3", "role": "user", "content": "three", "sequence_number": 3},
                            {"id": "m4", "role": "assistant", "content": "four", "sequence_number": 2},
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    conversation = ChatStore().get_conversation("conv-1")

    assert [message["id"] for message in conversation["messages"]] == ["m1", "m2", "m3", "m4"]
    assert [message["sequence_number"] for message in conversation["messages"]] == [1, 2, 3, 4]
    ChatStore._instance = None


@pytest.mark.parametrize(
    "raw_sequence",
    ["1e309", "-1e309", "true", "false", "1.0", '"1"', "null", "0", "[]", "{}"],
)
def test_chat_store_repairs_invalid_sequence_types_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_sequence: str
) -> None:
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "chat" / "conversations.json"
    storage_path.parent.mkdir()
    payload = {
        "conversations": {
            "conv-1": {
                "id": "conv-1",
                "model": "stub/default",
                "messages": [
                    {
                        "id": "m1",
                        "role": "user",
                        "content": "Keep this message",
                        "sequence_number": "BAD_SEQUENCE",
                    }
                ],
            }
        }
    }
    storage_path.write_text(
        json.dumps(payload).replace('"BAD_SEQUENCE"', raw_sequence), encoding="utf-8"
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    monkeypatch.setattr(ChatStore, "_instance", None)

    store = ChatStore()
    recovered = store.get_conversation("conv-1")["messages"][0]
    assert type(recovered["sequence_number"]) is int
    assert recovered["sequence_number"] == 1
    assert recovered["content"] == "Keep this message"

    appended = store.add_message("conv-1", {"role": "assistant", "content": "Reply"})
    assert appended["sequence_number"] == 2
    monkeypatch.setattr(ChatStore, "_instance", None)
    restarted = ChatStore().get_conversation("conv-1")["messages"]
    assert [message["sequence_number"] for message in restarted] == [1, 2]
    assert all(type(message["sequence_number"]) is int for message in restarted)
