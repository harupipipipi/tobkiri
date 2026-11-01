"""Neutral display visibility with real owner records and exact side reads."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest
from tests.test_side_chat_pack import PublicClient
from ecosystem.tobkiri_side_chat_pack.runtime.side_chat import SideChat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem/defaultspack"))

@pytest.fixture
def projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Adapt public reads to a display facade, preserving the actual owner."""
    from domain.chat import store as facade

    client = PublicClient(tmp_path)
    client.store.update(
        "conversation-1", {"title": "Needle parent"}, expected_conversation_revision=1,
    )
    side = SideChat(client, "defaults")
    child = side.ensure("conversation-1", 2)
    child_id = child["conversation_id"]
    client.store.update(child_id, {"title": "Needle child"}, expected_conversation_revision=1)
    client.store.append_message(
        child_id, {"id": "child-user", "role": "user", "content": "Needle child text"},
        expected_conversation_revision=2,
    )
    client.store.append_message(
        "conversation-1", {"id": "parent-user", "role": "user", "content": "Needle parent text"},
        expected_conversation_revision=3,
    )

    def invoke(contract: str, operation: str, payload: Any) -> Any:
        assert contract == facade.CONVERSATION
        if operation == "list":
            return client.store.snapshot()
        assert operation == "get"
        return client.store.get(payload["conversation_id"])

    monkeypatch.setattr(facade, "_invoke", invoke)
    return facade.ChatStore(), side, client.store, child_id


def test_hidden_child_absent_main_list_and_unknown_id_search(projection: Any) -> None:
    facade, _, owner, child_id = projection
    before = owner.path.read_bytes()
    page, total = facade.list_conversations(limit=1, include_messages=True)
    assert total == 1 and [item["id"] for item in page] == ["conversation-1"]
    page, total = facade.list_conversations(offset=1)
    assert page == [] and total == 1
    results, total = facade.search_conversations("Needle")
    assert total == 1
    assert [item["conversation_id"] for item in results] == ["conversation-1"]
    assert [item["id"] for item in facade.search("Needle")] == ["parent-user"]
    assert child_id in facade.conversations  # The raw snapshot is preserved.
    assert owner.path.read_bytes() == before


def test_hidden_child_remains_accessible_by_exact_public_read(projection: Any) -> None:
    facade, side, owner, child_id = projection
    assert {record["id"] for record in owner.snapshot()["conversations"]} == {
        "conversation-1", child_id,
    }
    assert facade.get_conversation(child_id)["metadata"]["is_hidden"] is True
    assert facade.search("Needle", conversation_id=child_id)[0]["id"] == "child-user"
    thread = side.get("conversation-1")
    assert thread["status"] == "available" and thread["conversation_id"] == child_id
    assert thread["thread"]["messages"][0]["content"] == "Needle child text"


@pytest.mark.parametrize("visibility,listed", [(True, False), (False, True), ("true", True)])
def test_visibility_requires_exact_boolean_and_has_no_side_kind_branch(
    projection: Any, visibility: Any, listed: bool,
) -> None:
    facade, _, owner, child_id = projection
    owner.update(
        child_id, {"metadata": {"is_hidden": visibility}},
        expected_conversation_revision=3,
    )
    results, _ = facade.list_conversations()
    assert (child_id in {item["id"] for item in results}) is listed
