from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _column_by_title(columns, title):
    return next(column for column in columns if column["title"] == title)


def _store(path):
    from ecosystem.rumi_kanban_state_store_pack.runtime.store import KanbanStateStore
    from domain.kanban.store import KanbanStore

    return KanbanStore(path, state_store_factory=KanbanStateStore)


def test_kanban_store_bootstraps_board_and_moves_cards(tmp_path):
    store = _store(tmp_path / "kanban.db")
    board = store.get_or_create_board("conversation", "conv-1", title="Conversation board")
    columns = store.list_columns(board["board_id"])

    assert [column["title"] for column in columns] == ["Backlog", "Doing", "Review", "Done"]
    assert _column_by_title(columns, "Done")["done"] is True

    card = store.create_card(
        board["board_id"],
        {
            "title": "Wire API",
            "description": "Finish the transport handler",
            "labels": ["backend", "backend", ""],
        },
    )
    assert card["labels"] == ["backend"]
    assert card["position"] == 0
    assert card["column_id"] == columns[0]["column_id"]

    doing = _column_by_title(columns, "Doing")
    moved = store.move_card(card["card_id"], {"column_id": doing["column_id"]})
    assert moved["column_id"] == doing["column_id"]

    updated = store.update_card(card["card_id"], {"priority": "high"})
    assert updated["priority"] == "high"

    snapshot = store.board_snapshot(board["board_id"])
    assert snapshot["board"]["board_id"] == board["board_id"]
    assert [item["card_id"] for item in snapshot["cards"]] == [card["card_id"]]
    assert {event["event_type"] for event in snapshot["events"]} >= {
        "board.bootstrap",
        "card.created",
        "card.moved",
        "card.updated",
    }


def test_kanban_store_reorders_cards_and_enforces_wip_limit(tmp_path):
    store = _store(tmp_path / "kanban.db")
    board = store.get_or_create_board("workspace", "accessible-board")
    columns = store.list_columns(board["board_id"])
    backlog = _column_by_title(columns, "Backlog")
    doing = _column_by_title(columns, "Doing")
    store.update_column(doing["column_id"], {"wip_limit": 1})

    first = store.create_card(board["board_id"], {"title": "First"})
    second = store.create_card(board["board_id"], {"title": "Second"})
    third = store.create_card(board["board_id"], {"title": "Third"})

    store.move_card(
        third["card_id"],
        {
            "column_id": backlog["column_id"],
            "before_card_id": first["card_id"],
        },
    )
    backlog_cards = [
        card
        for card in store.list_cards(board["board_id"])
        if card["column_id"] == backlog["column_id"]
    ]
    assert [card["title"] for card in backlog_cards] == [
        "Third",
        "First",
        "Second",
    ]
    assert [card["position"] for card in backlog_cards] == [0, 1, 2]

    store.move_card(first["card_id"], {"column_id": doing["column_id"]})
    with pytest.raises(RuntimeError, match="WIP limit 1 is reached"):
        store.move_card(second["card_id"], {"column_id": doing["column_id"]})

    assert store.require_card(second["card_id"])["column_id"] == backlog["column_id"]


def test_kanban_store_rejects_empty_title_updates(tmp_path):
    from domain.kanban.models import KanbanValidationError

    store = _store(tmp_path / "kanban.db")
    board = store.get_or_create_board("conversation", "conv-empty-title")
    card = store.create_card(board["board_id"], {"title": "Keep title"})

    with pytest.raises(KanbanValidationError, match="title is required"):
        store.update_card(card["card_id"], {"title": "   "})

    assert store.require_card(card["card_id"])["title"] == "Keep title"


def test_kanban_store_accepts_group_scope(tmp_path):
    store = _store(tmp_path / "kanban.db")
    board = store.get_or_create_board("group", "group-alpha", title="Alpha board")
    same = store.get_or_create_board("group", "group-alpha")

    assert board["board_id"] == same["board_id"]
    assert board["scope_type"] == "group"
    assert board["scope_id"] == "group-alpha"
    assert [column["title"] for column in store.list_columns(board["board_id"])] == ["Backlog", "Doing", "Review", "Done"]


def test_kanban_store_fails_closed_without_an_injected_owner(tmp_path):
    from domain.kanban.store import KanbanOwnerUnavailable, KanbanStore
    from domain.kanban.service import KanbanService

    with pytest.raises(KanbanOwnerUnavailable, match="injected state-store factory"):
        KanbanStore(tmp_path / "kanban.db")
    with pytest.raises(KanbanOwnerUnavailable, match="injected state-store factory"):
        KanbanService()

    def empty_factory(profile_id, *, root=None):
        del profile_id, root
        return None

    with pytest.raises(KanbanOwnerUnavailable, match="usable owner"):
        KanbanStore(
            tmp_path / "kanban.db",
            state_store_factory=empty_factory,
        )


def test_kanban_service_agent_transitions_are_local_noops(tmp_path):
    from domain.kanban.service import KanbanService

    service = KanbanService(_store(tmp_path / "kanban.db"))
    snapshot = service.bootstrap_board({"scope_type": "workspace", "scope_id": "workspace-1"})
    board_id = snapshot["board"]["board_id"]

    card = service.create_card(board_id, {"title": "Implement agent button"})
    started = service.agent_start(card["card_id"], {"task": "Implement agent button", "model": "local"})
    columns = service.store.list_columns(board_id)
    assert started["agent_status"] == "running"
    assert started["agent_run_id"]
    assert started["agent_session_id"]
    assert started["column_id"] == _column_by_title(columns, "Doing")["column_id"]

    ready = service.agent_ready(card["card_id"])
    assert ready["agent_status"] == "ready"
    assert ready["column_id"] == _column_by_title(columns, "Review")["column_id"]

    applied = service.agent_apply(card["card_id"])
    assert applied["agent_status"] == "applied"
    assert applied["column_id"] == _column_by_title(columns, "Done")["column_id"]

    dismissed_card = service.create_card(board_id, {"title": "Review without applying"})
    service.agent_start(dismissed_card["card_id"], {})
    dismissed = service.agent_dismiss(dismissed_card["card_id"])
    assert dismissed["agent_status"] == "dismissed"
    assert dismissed["column_id"] == _column_by_title(columns, "Review")["column_id"]

    event_types = {event["event_type"] for event in service.store.list_events(board_id)}
    assert event_types >= {"agent.started", "agent.ready", "agent.applied", "agent.dismissed"}
