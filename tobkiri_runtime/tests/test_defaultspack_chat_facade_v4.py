from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")


def test_chat_facade_bounds_conflict_retries(
    defaultspack_conversation_owner, monkeypatch
):
    """A perpetually stale owner cannot turn append into an unbounded loop."""
    from domain.chat import store as facade
    from domain.chat.store import ChatStore, MAX_APPEND_RETRIES
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationConflict,
    )

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    original_invoke = facade._invoke
    attempts = 0

    def always_conflict(contract_id, operation, payload):
        nonlocal attempts
        if contract_id == facade.MESSAGE_MANAGE and operation == "append":
            attempts += 1
            raise ConversationConflict("simulated stale revision")
        return original_invoke(contract_id, operation, payload)

    monkeypatch.setattr(facade, "_invoke", always_conflict)

    with pytest.raises(ConversationConflict, match="simulated stale revision"):
        store.add_message(
            conversation["id"],
            {"role": "user", "content": "retry me"},
        )

    assert attempts == MAX_APPEND_RETRIES
    assert defaultspack_conversation_owner.get(conversation["id"])["messages"] == []


def test_chat_facade_rejects_all_message_mutations_for_shared_read_only_owner(
    defaultspack_conversation_owner,
):
    """Shared read-only metadata is enforced at every message mutation seam."""
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    message = store.add_message(
        conversation["id"],
        {"id": "read-only-message", "role": "user", "content": "keep me"},
    )
    current = defaultspack_conversation_owner.get(conversation["id"])
    defaultspack_conversation_owner.update(
        conversation["id"],
        {"metadata": {"shared_read_only": True}},
        expected_conversation_revision=current["conversation_revision"],
    )

    assert message is not None
    assert store.add_message(
        conversation["id"], {"role": "assistant", "content": "blocked"}
    ) is None
    assert store.update_message(
        conversation["id"], message["id"], {"content": "blocked"}
    ) is None
    assert store.delete_message(conversation["id"], message["id"]) is False
    assert store.delete_messages_bulk(conversation["id"], [message["id"]]) == 0
    assert store.insert_message_at(
        conversation["id"],
        {"role": "assistant", "content": "blocked"},
        position_index=0,
    ) is None

    persisted = defaultspack_conversation_owner.get(conversation["id"])
    assert [item["id"] for item in persisted["messages"]] == [message["id"]]
    assert persisted["messages"][0]["raw_text"] == "keep me"


def test_chat_facade_preserves_supplied_metadata_when_merging_compatibility_extras(
    defaultspack_conversation_owner,
):
    """Compatibility fields must not overwrite canonical v4 metadata."""
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    message = store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "content": "draft",
            "metadata": {"phase": "draft", "stale": True},
        },
    )

    updated = store.update_message(
        conversation["id"],
        message["id"],
        {
            "raw_text": "final",
            "metadata": {"phase": "final"},
            "tool_status": "complete",
        },
    )

    assert updated["raw_text"] == "final"
    assert updated["metadata"] == {
        "phase": "final",
        "tool_status": "complete",
    }


def test_chat_facade_persists_only_non_ephemeral_attachment_records(
    defaultspack_conversation_owner,
):
    """The v2 manifest records durable attachments and excludes model-only data."""
    from domain.chat.attachments.store import manifest_path
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    refs = store.persist_attachments(
        conversation["id"],
        [
            {
                "id": "ephemeral-audio",
                "name": "voice.webm",
                "type": "audio/webm",
                "content": "model only",
                "ephemeral": True,
                "do_not_persist": True,
            },
            {
                "id": "durable-note",
                "name": "note.txt",
                "type": "text/plain",
                "content": "persist this",
            },
        ],
    )

    manifest = json.loads(
        manifest_path(store.conversation_workspace_dir(conversation["id"]))
        .read_text(encoding="utf-8")
    )
    assert [ref["id"] for ref in refs] == ["durable-note"]
    assert [record["id"] for record in manifest["attachments"]] == [
        "durable-note"
    ]
    assert manifest["attachments"][0]["representations"]["text"]["text"] == (
        "persist this"
    )


def test_chat_facade_search_excludes_message_text_until_requested(
    defaultspack_conversation_owner,
):
    """List search and exact message search keep their distinct scopes."""
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    store.add_message(
        conversation["id"],
        {"role": "user", "content": "private needle"},
    )

    listed, listed_total = store.list_conversations(query="private needle")
    with_messages, message_total = store.list_conversations(
        query="private needle", include_messages=True
    )
    exact, exact_total = store.search_conversations("private needle")
    missing, missing_total = store.search_conversations("not present")

    assert listed == []
    assert listed_total == 0
    assert with_messages[0]["id"] == conversation["id"]
    assert message_total == 1
    assert exact_total == 1
    assert exact[0]["matches"][0]["exact"] is True
    assert missing == []
    assert missing_total == 0


def test_chat_facade_retries_stale_order_without_duplicate_sequences(
    defaultspack_conversation_owner,
):
    """Concurrent appenders preserve owner order even with the same stale hint."""
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    first = store.add_message(
        conversation["id"],
        {"role": "user", "content": "first"},
    )

    def append(index):
        return store.add_message(
            conversation["id"],
            {
                "role": "assistant",
                "content": f"parallel {index}",
                "parent_id": None,
                "sequence_number": first["sequence_number"] + 1,
            },
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        appended = list(executor.map(append, range(16)))

    stored = store.get_conversation(conversation["id"])
    owner_stored = defaultspack_conversation_owner.get(conversation["id"])
    sequences = [item["sequence_number"] for item in stored["messages"]]
    assert all(item is not None for item in appended)
    assert sequences == list(range(1, 18))
    assert [item["sequence"] for item in owner_stored["messages"]] == list(
        range(17)
    )
    assert sorted(item["sequence_number"] for item in appended) == list(
        range(2, 18)
    )
