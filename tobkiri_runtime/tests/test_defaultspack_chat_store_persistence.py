from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


@pytest.fixture
def conversation_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Bind the Wave 7 compatibility facade to its canonical owner."""
    from domain.chat import store as facade
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
        if contract_id == facade.CONVERSATION_MANAGE:
            if operation == "create":
                return owner.create(
                    payload["conversation"],
                    expected_revision=int(payload["expected_revision"]),
                )
            if operation == "update":
                return owner.update(
                    str(payload["conversation_id"]),
                    payload["patch"],
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
            if operation == "delete":
                return owner.delete(
                    str(payload["conversation_id"]),
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
        if contract_id == facade.MESSAGE_MANAGE and operation == "append":
            return owner.append_message(
                str(payload["conversation_id"]),
                payload["message"],
                expected_conversation_revision=int(
                    payload["expected_conversation_revision"]
                ),
            )
        raise AssertionError(
            f"unexpected contract call: {contract_id}/{operation}"
        )

    monkeypatch.setattr(facade, "_invoke", invoke)
    return owner


def test_chat_store_create_persists_only_host_generated_icon_id(
    conversation_owner,
) -> None:
    from domain.chat.store import ChatStore

    payload = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'onload="globalThis.__rumi_xss=1"></svg>'
    )
    conversation = ChatStore().create_conversation(
        model="stub/default",
        metadata={
            "title": "Local workspace",
            "icon_id": "client-controlled",
            "icon_svg": payload,
            "workspace_label": "Local",
        },
    )

    assert conversation["metadata"]["workspace_label"] == "Local"
    assert conversation["metadata"]["icon_id"] != "client-controlled"
    assert "icon_svg" not in conversation["metadata"]

    persisted = json.loads(
        conversation_owner.path.read_text(encoding="utf-8")
    )
    metadata = persisted["conversations"][conversation["id"]]["metadata"]
    assert metadata["icon_id"] == conversation["metadata"]["icon_id"]
    assert "icon_svg" not in metadata


def test_chat_store_update_replaces_all_client_icon_fields(
    conversation_owner,
) -> None:
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    updated = store.update_conversation(
        conversation["id"],
        {
            "title": "Updated title",
            "metadata": {
                "icon_id": "database",
                "icon_svg": "<svg><script>x</script></svg>",
                "workspace_label": "Local",
            },
        },
    )

    assert updated is not None
    assert updated["metadata"]["workspace_label"] == "Local"
    assert updated["metadata"]["icon_id"] != "database"
    assert "icon_svg" not in updated["metadata"]
    assert "icon_svg" not in conversation_owner.get(
        conversation["id"]
    )["metadata"]


def test_chat_store_list_conversations_omits_full_messages_by_default(
    conversation_owner,
) -> None:
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello world"}],
        },
    )

    listed, total = store.list_conversations(
        limit=10,
        include_messages=False,
    )

    assert total == 1
    assert listed[0]["id"] == conversation["id"]
    assert listed[0]["messages"] == []
    assert listed[0]["message_count"] == 1
    assert listed[0]["last_message_preview"] == "hello world"


def test_chat_store_conversation_window_is_a_read_only_projection(
    conversation_owner,
) -> None:
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    first = store.add_message(
        conversation["id"],
        {"id": "message-1", "role": "user", "content": "first"},
    )
    second = store.add_message(
        conversation["id"],
        {"id": "message-2", "role": "assistant", "content": "second"},
    )

    windowed, window = store.get_conversation_window(
        conversation["id"],
        message_limit=1,
    )

    assert first is not None
    assert second is not None
    assert windowed is not None
    assert window is not None
    assert [item["id"] for item in windowed["messages"]] == ["message-2"]
    assert window["total"] == 2
    assert [
        item["id"]
        for item in conversation_owner.get(conversation["id"])["messages"]
    ] == ["message-1", "message-2"]


def test_chat_store_append_links_to_the_current_message_atomically(
    conversation_owner,
) -> None:
    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    root = store.add_message(
        conversation["id"],
        {"id": "root", "role": "user", "content": "root"},
    )
    child = store.add_message(
        conversation["id"],
        {"id": "child", "role": "assistant", "content": "child"},
    )

    persisted = conversation_owner.get(conversation["id"])
    by_id = {item["id"]: item for item in persisted["messages"]}

    assert root is not None
    assert child is not None
    assert by_id["root"]["children_ids"] == ["child"]
    assert by_id["child"]["parent_id"] == "root"


def test_canonical_owner_replaces_only_lone_surrogates_before_persisting(
    conversation_owner,
) -> None:
    created = conversation_owner.create(
        {
            "id": "unicode-record",
            "title": "日本語 😀",
            "metadata": {
                "valid_unicode": "café 雪",
                "invalid_text": "bad \udc88 metadata",
            },
        },
        expected_revision=conversation_owner.snapshot()["revision"],
    )
    conversation_owner.append_message(
        "unicode-record",
        {
            "id": "unicode-message",
            "role": "assistant",
            "content": [{"type": "text", "text": "bad \udc88 text 日本語"}],
            "tool_logs": [{"result": "tool \udc88 log 😀"}],
        },
        expected_conversation_revision=created["conversation"][
            "conversation_revision"
        ],
    )

    persisted = json.loads(
        conversation_owner.path.read_text(encoding="utf-8")
    )
    conversation = persisted["conversations"]["unicode-record"]
    serialized = json.dumps(conversation, ensure_ascii=False)
    assert "\udc88" not in serialized
    assert conversation["title"] == "日本語 😀"
    assert conversation["metadata"]["valid_unicode"] == "café 雪"
    assert conversation["metadata"]["invalid_text"] == "bad ? metadata"
    assert conversation["messages"][0]["content"][0]["text"] == (
        "bad ? text 日本語"
    )
    assert conversation["messages"][0]["tool_logs"][0]["result"] == (
        "tool ? log 😀"
    )


def test_canonical_owner_drops_icon_svg_even_without_the_facade(
    conversation_owner,
) -> None:
    created = conversation_owner.create(
        {
            "id": "direct-owner-write",
            "metadata": {
                "icon_id": "database",
                "icon_svg": '<svg onload="globalThis.pwned=true"></svg>',
            },
        },
        expected_revision=conversation_owner.snapshot()["revision"],
    )

    assert created["conversation"]["metadata"] == {"icon_id": "database"}
    persisted = json.loads(
        conversation_owner.path.read_text(encoding="utf-8")
    )
    assert persisted["conversations"]["direct-owner-write"]["metadata"] == {
        "icon_id": "database"
    }


def test_canonical_owner_drops_persisted_icon_svg_on_load(
    conversation_owner,
) -> None:
    created = conversation_owner.create(
        {
            "id": "legacy-owner-record",
            "metadata": {
                "icon_id": "database",
                "workspace_label": "Local",
            },
        },
        expected_revision=conversation_owner.snapshot()["revision"],
    )
    persisted = json.loads(
        conversation_owner.path.read_text(encoding="utf-8")
    )
    persisted["conversations"]["legacy-owner-record"]["metadata"][
        "icon_svg"
    ] = '<svg onload="globalThis.pwned=true"></svg>'
    conversation_owner.path.write_text(
        json.dumps(persisted),
        encoding="utf-8",
    )

    restored = conversation_owner.get("legacy-owner-record")
    assert restored is not None
    assert restored["metadata"] == {
        "icon_id": "database",
        "workspace_label": "Local",
    }

    conversation_owner.update(
        "legacy-owner-record",
        {"title": "Rewritten safely"},
        expected_conversation_revision=created["conversation"][
            "conversation_revision"
        ],
    )
    rewritten = json.loads(
        conversation_owner.path.read_text(encoding="utf-8")
    )
    assert "icon_svg" not in rewritten["conversations"][
        "legacy-owner-record"
    ]["metadata"]
