from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _column_by_title(snapshot, title):
    return next(column for column in snapshot["columns"] if column["title"] == title)


def test_kanban_list_requires_captured_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/kanban/boards",
        "tobkiri.kanban.v1",
        "defaultspack.kanban.list-boards",
    )


def test_kanban_mutation_requires_captured_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/kanban/boards",
        "tobkiri.kanban.v1",
        "defaultspack.kanban.create-board",
    )


def test_kanban_block_handler_bootstraps_and_mutates_board(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(tmp_path / "kanban.db"))

    from blocks.kanban.api import run
    from ecosystem.rumi_kanban_state_store_pack.runtime.store import KanbanStateStore

    owner_context = {"kanban_state_store_factory": KanbanStateStore}

    bootstrapped = run(
        {
            "_method": "GET",
            "scope_type": "conversation",
            "scope_id": "conv-1",
            "bootstrap": "true",
        },
        owner_context,
    )
    assert bootstrapped["status"] == "ok"
    snapshot = bootstrapped["data"]
    assert snapshot["board"]["scope_type"] == "conversation"
    assert [column["title"] for column in snapshot["columns"]] == ["Backlog", "Doing", "Review", "Done"]

    board_id = snapshot["board"]["board_id"]
    created = run(
        {"action": "create_card", "board_id": board_id, "title": "Finish API"},
        owner_context,
    )
    assert created["status"] == "ok"
    card = created["data"]
    assert card["title"] == "Finish API"

    started = run(
        {
            "action": "agent_start",
            "card_id": card["card_id"],
            "task": "Finish API",
            "model": "local",
        },
        owner_context,
    )
    assert started["status"] == "ok"
    assert started["data"]["agent_status"] == "running"
    assert started["data"]["column_id"] == _column_by_title(snapshot, "Doing")["column_id"]

    ready = run(
        {"action": "agent_ready", "card_id": card["card_id"]},
        owner_context,
    )
    assert ready["status"] == "ok"
    assert ready["data"]["agent_status"] == "ready"
    assert ready["data"]["column_id"] == _column_by_title(snapshot, "Review")["column_id"]

    applied = run(
        {"action": "agent_apply", "card_id": card["card_id"]},
        owner_context,
    )
    assert applied["status"] == "ok"
    assert applied["data"]["agent_status"] == "applied"
    assert applied["data"]["column_id"] == _column_by_title(snapshot, "Done")["column_id"]

    synced = run({"action": "sync_runs", "board_id": board_id}, owner_context)
    assert synced["status"] == "ok"
    assert synced["data"]["board"]["board_id"] == board_id
    assert any(event["event_type"] == "runs.sync.noop" for event in synced["data"]["events"])
