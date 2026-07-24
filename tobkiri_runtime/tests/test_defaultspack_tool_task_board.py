from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytest.skip(
    "legacy Kanban service/store were retired; contract_facade tests own this boundary",
    allow_module_level=True,
)

from domain.tool.task_board import TaskBoardController  # noqa: E402
from domain.tool.executor import ToolExecutor  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.kanban.service import KanbanService  # noqa: E402
from domain.kanban.store import KanbanStore  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_kanban_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(tmp_path / "canonical-kanban.db"))
    KanbanStore._instance = None
    yield
    KanbanStore._instance = None


def test_task_board_controller_persists_workspace_board_and_moves_cards(tmp_path):
    workspace = tmp_path / "conversation" / "workspace"
    controller = TaskBoardController()
    context = {"conversation_workspace_dir": str(workspace)}

    created = controller.run(
        {"action": "create", "title": "Review PR slice", "priority": "high"},
        context,
    )
    card_id = created["changed"]["id"]
    moved = controller.run(
        {"action": "move", "card_id": card_id, "column": "Doing"},
        context,
    )

    board_path = workspace / "task_board.json"
    store = KanbanStore()
    stored = store.require_card(card_id)
    stored_column = store.require_column(stored["column_id"])
    api_snapshot = KanbanService().bootstrap_board(
        {"scope_type": "workspace", "scope_id": str(workspace.resolve())},
    )

    assert not board_path.exists()
    assert not (workspace / "task_board_kanban.db").exists()
    assert created["kanban"]["board"]["board_id"] == moved["kanban_board_id"]
    assert api_snapshot["board"]["board_id"] == moved["kanban_board_id"]
    assert [card["title"] for card in api_snapshot["cards"]] == ["Review PR slice"]
    assert [column["title"] for column in moved["columns"]] == ["Backlog", "Doing", "Review", "Done"]
    assert moved["cards"][0]["column_id"] == "doing"
    assert moved["cards"][0]["kanban_column_id"] == stored_column["column_id"]
    assert stored_column["title"] == "Doing"
    assert stored["priority"] == "high"


def test_task_board_and_kanban_service_share_workspace_board(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(db_path))
    KanbanStore._instance = None
    context = {"workspace_id": "ws-shared", "conversation_id": "conv-1"}
    controller = TaskBoardController()
    service = KanbanService(KanbanStore(db_path))

    created = controller.run({"action": "create", "title": "Visible from Kanban", "notes": "same source"}, context)
    snapshot = service.bootstrap_board({"scope_type": "workspace", "scope_id": "ws-shared"})

    assert snapshot["board"]["board_id"] == created["kanban_board_id"]
    assert [card["title"] for card in snapshot["cards"]] == ["Visible from Kanban"]
    assert snapshot["cards"][0]["description"] == "same source"
    assert snapshot["cards"][0]["conversation_id"] == "conv-1"


def test_tool_executor_task_board_shares_env_kanban_api_board(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(tmp_path / "shared-kanban.db"))
    KanbanStore._instance = None
    context = {
        "workspace_id": "ws-env-shared",
        "conversation_id": "conv-env",
        "profile_policy": {"yolo_mode": True},
        "principal_id": "defaultspack",
    }

    result = ToolExecutor().execute(
        "tool_task_board",
        {"action": "create", "title": "ToolExecutor shared card"},
        context,
    )

    from blocks.kanban.api import run

    response = run(
        {
            "action": "list_boards",
            "scope_type": "workspace",
            "scope_id": "ws-env-shared",
            "bootstrap": True,
        },
        {},
    )

    assert result["is_error"] is False
    assert response["status"] == "ok"
    assert response["data"]["board"]["title"] == "Kanban: ws-env-shared"
    assert [card["title"] for card in response["data"]["cards"]] == ["ToolExecutor shared card"]


def test_task_board_ignores_untrusted_context_db_path(tmp_path, monkeypatch):
    primary_db = tmp_path / "primary-kanban.db"
    redirected_db = tmp_path / "redirected-kanban.db"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(primary_db))
    KanbanStore._instance = None

    result = ToolExecutor().execute(
        "tool_task_board",
        {"action": "create", "title": "Trusted DB only"},
        {
            "workspace_id": "ws-trusted-db",
            "conversation_id": "conv-trusted-db",
            "kanban_db_path": str(redirected_db),
            "task_board_kanban_db_path": str(redirected_db),
            "_task_board_test_context": True,
            "profile_policy": {"yolo_mode": True},
            "principal_id": "defaultspack",
        },
    )

    primary = KanbanService(KanbanStore(primary_db)).bootstrap_board(
        {"scope_type": "workspace", "scope_id": "ws-trusted-db"},
    )

    assert result["is_error"] is False
    assert [card["title"] for card in primary["cards"]] == ["Trusted DB only"]
    assert not redirected_db.exists()


def test_task_board_lists_cards_created_by_kanban_service(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(db_path))
    KanbanStore._instance = None
    service = KanbanService(KanbanStore(db_path))
    snapshot = service.bootstrap_board({"scope_type": "workspace", "scope_id": "ws-inverse"})
    card = service.create_card(
        snapshot["board"]["board_id"],
        {
            "title": "Created in Kanban",
            "description": "projected into Task Board",
            "checklist": [{"id": "c1", "title": "Check projection", "done": True}],
        },
    )

    listed = TaskBoardController().run(
        {"action": "list"},
        {"workspace_id": "ws-inverse"},
    )

    assert listed["changed"] is None
    assert listed["cards"][0]["id"] == card["card_id"]
    assert listed["cards"][0]["notes"] == "projected into Task Board"
    assert listed["cards"][0]["subtasks"][0]["title"] == "Check projection"


def test_task_board_imports_legacy_json_once_into_kanban(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    legacy = {
        "columns": ["Inbox", "Active", "Done"],
        "cards": [
            {
                "id": "legacy-root",
                "title": "Legacy root",
                "column_id": "inbox",
                "position": 0,
                "notes": "old JSON",
            },
            {
                "id": "legacy-child",
                "title": "Legacy child",
                "column_id": "active",
                "position": 0,
                "depends_on": ["legacy-root"],
                "blocked_by": ["legacy-root"],
                "subtasks": [{"id": "sub-1", "title": "Keep subtask", "done": False}],
            },
        ],
    }
    (workspace / "task_board.json").write_text(json.dumps(legacy), encoding="utf-8")

    listed = TaskBoardController().run({"action": "list"}, {"conversation_workspace_dir": str(workspace)})
    listed_again = TaskBoardController().run({"action": "list"}, {"conversation_workspace_dir": str(workspace)})
    child = next(card for card in listed["cards"] if card["title"] == "Legacy child")
    root = next(card for card in listed["cards"] if card["title"] == "Legacy root")

    assert len(listed["cards"]) == 2
    assert len(listed_again["cards"]) == 2
    assert child["depends_on"] == [root["id"]]
    assert child["blocked_by"] == [root["id"]]
    assert child["subtasks"][0]["title"] == "Keep subtask"
    assert listed["metadata"]["task_board_json_imported"].endswith("task_board.json")


def test_task_board_imports_legacy_json_even_when_board_has_existing_cards(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    legacy = {
        "columns": ["Inbox", "Active", "Done"],
        "cards": [
            {
                "id": "legacy-mixed",
                "title": "Legacy mixed card",
                "column_id": "active",
                "position": 0,
                "notes": "old JSON",
            },
        ],
    }
    (workspace / "task_board.json").write_text(json.dumps(legacy), encoding="utf-8")

    controller = TaskBoardController()
    created = controller.run(
        {"action": "create", "title": "Existing canonical card"},
        {"conversation_workspace_dir": str(workspace)},
    )
    listed = controller.run({"action": "list"}, {"conversation_workspace_dir": str(workspace)})
    listed_again = controller.run({"action": "list"}, {"conversation_workspace_dir": str(workspace)})

    titles = [card["title"] for card in listed["cards"]]
    assert created["changed"]["title"] == "Existing canonical card"
    assert titles == ["Existing canonical card", "Legacy mixed card"]
    assert [card["title"] for card in listed_again["cards"]] == titles
    legacy_card = next(card for card in listed["cards"] if card["title"] == "Legacy mixed card")
    assert legacy_card["metadata"]["task_board"]["legacy_task_board_id"] == "legacy-mixed"
    assert listed["metadata"]["task_board_json_imported"].endswith("task_board.json")


def test_task_board_controller_configures_columns_and_rehomes_removed_column_cards(tmp_path):
    workspace = tmp_path / "workspace"
    controller = TaskBoardController()

    created = controller.run(
        {"action": "create", "title": "Ship minimal backend", "column": "Review"},
        {"conversation_workspace_dir": str(workspace)},
    )
    configured = controller.run(
        {"action": "configure", "columns": ["Inbox", "Active", "Done"]},
        {"conversation_workspace_dir": str(workspace)},
    )

    assert created["changed"]["column_id"] == "review"
    assert [column["id"] for column in configured["columns"]] == ["inbox", "active", "done"]
    assert configured["cards"][0]["column_id"] == "inbox"


def test_task_board_controller_preserves_card_column_on_same_shape_rename(tmp_path):
    workspace = tmp_path / "workspace"
    controller = TaskBoardController()

    created = controller.run(
        {"action": "create", "title": "Ready for QA", "column": "Review"},
        {"conversation_workspace_dir": str(workspace)},
    )
    configured = controller.run(
        {"action": "configure", "columns": ["Backlog", "Doing", "QA", "Done"]},
        {"conversation_workspace_dir": str(workspace)},
    )

    assert created["changed"]["column_id"] == "review"
    assert [column["id"] for column in configured["columns"]] == ["backlog", "doing", "qa", "done"]
    assert configured["cards"][0]["column_id"] == "qa"


def test_task_board_metadata_update_deep_merges_by_default(tmp_path):
    workspace = tmp_path / "workspace"
    controller = TaskBoardController()

    created = controller.run(
        {
            "action": "create",
            "title": "Preserve metadata",
            "metadata": {
                "agent": {"session_id": "sess-1", "status": "running"},
                "task_board": {"blocker_reason": "waiting"},
            },
        },
        {"conversation_workspace_dir": str(workspace)},
    )
    updated = controller.run(
        {
            "action": "update",
            "card_id": created["changed"]["id"],
            "metadata": {"agent": {"status": "ready"}, "extra": {"review": True}},
        },
        {"conversation_workspace_dir": str(workspace)},
    )

    metadata = updated["changed"]["metadata"]
    assert metadata["agent"] == {"session_id": "sess-1", "status": "ready"}
    assert metadata["task_board"]["blocker_reason"] == "waiting"
    assert metadata["extra"] == {"review": True}


def test_task_board_controller_preserves_board_order_and_done_count_for_custom_terminal_columns(tmp_path):
    workspace = tmp_path / "workspace"
    controller = TaskBoardController()

    controller.run(
        {
            "action": "configure",
            "columns": [
                {"title": "Inbox"},
                {"title": "Active"},
                {"title": "Completed"},
            ],
        },
        {"conversation_workspace_dir": str(workspace)},
    )
    controller.run({"action": "create", "title": "First", "column": "Inbox"}, {"conversation_workspace_dir": str(workspace)})
    controller.run({"action": "create", "title": "Second", "column": "Active"}, {"conversation_workspace_dir": str(workspace)})
    listed = controller.run(
        {"action": "create", "title": "Third", "column": "Completed"},
        {"conversation_workspace_dir": str(workspace)},
    )

    assert listed["summary"].endswith("(2 open)")
    assert [card["title"] for card in listed["cards"]] == ["First", "Second", "Third"]
    assert [column["done"] for column in listed["columns"]] == [False, False, True]


def test_tool_registry_and_executor_invoke_manifest_backed_task_board(tmp_path):
    ToolRegistry._instance = None
    registry = ToolRegistry()

    tool = registry.get("tool_task_board")
    result = ToolExecutor().execute(
        "tool_task_board",
        {"action": "create", "title": "Exercise ToolExecutor"},
        {
            "conversation_workspace_dir": str(tmp_path),
            "profile_policy": {"yolo_mode": True},
            "principal_id": "defaultspack",
        },
    )

    assert tool is not None
    assert tool["execution"]["handler"] == "domain.tool.task_board:tool_task_board"
    assert "done" in tool["schema"]["parameters"]["properties"]["columns"]["items"]["oneOf"][1]["properties"]
    assert result["is_error"] is False
    assert result["widget"]["type"] == "task_board"
    assert result["widget"]["cards"][0]["title"] == "Exercise ToolExecutor"


def test_tool_task_board_function_runtime_registers_and_invokes(tmp_path):
    from core_runtime.di_container import get_container, reset_container
    from core_runtime.pack_function_runtime import invoke_pack_function
    from ecosystem.defaultspack.domain.function_runtime.bridge import ensure_defaultspack_functions_registered
    from ecosystem.defaultspack.domain.function_runtime.dispatcher import run_defaultspack_function

    reset_container()
    registered = ensure_defaultspack_functions_registered(get_container())
    dispatched = run_defaultspack_function(
        "tool_task_board",
        {"action": "create", "title": "Exercise dispatcher"},
        {"conversation_workspace_dir": str(tmp_path / "dispatcher")},
    )
    output = invoke_pack_function(
        "defaultspack",
        "tool_task_board",
        {"action": "create", "title": "Exercise function runtime"},
        {"conversation_workspace_dir": str(tmp_path)},
    )

    assert registered > 0
    assert dispatched["status"] == "ok"
    assert dispatched["data"]["widget"]["type"] == "task_board"
    assert output["status"] == "ok"
    assert output["data"]["is_error"] is False
    assert output["data"]["widget"]["type"] == "task_board"
    assert output["data"]["widget"]["columns"][0]["title"] == "Backlog"
