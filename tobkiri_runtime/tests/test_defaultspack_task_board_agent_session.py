from __future__ import annotations

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
from domain.tool.task_board_agent_session import TaskBoardAgentSessionController  # noqa: E402
from domain.tool.executor import ToolExecutor  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.kanban.store import KanbanStore  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_kanban_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(tmp_path / "canonical-kanban.db"))
    KanbanStore._instance = None
    yield
    KanbanStore._instance = None


def _reset_sessions():
    from blocks.agent import _state

    _state._multi_sessions.clear()


def test_task_board_card_starts_tracks_and_applies_agent_session(tmp_path):
    _reset_sessions()
    context = {"conversation_workspace_dir": str(tmp_path / "workspace")}
    card = TaskBoardController().run(
        {"action": "create", "title": "Implement task run", "notes": "Keep it backend-only."},
        context,
    )["changed"]
    controller = TaskBoardAgentSessionController()

    started = controller.run({"action": "start", "card_id": card["id"], "max_turns": 1}, context)
    session_id = started["session_link"]["session_id"]

    assert session_id.startswith("multi_")
    assert started["card"]["column_id"] == "doing"
    assert started["card"]["metadata"]["agent_session"]["task"].startswith("Implement task run")

    status = controller.run({"action": "status", "card_id": card["id"]}, context)
    report = controller.run({"action": "merge_report", "card_id": card["id"]}, context)
    applied = controller.run({"action": "apply", "card_id": card["id"]}, context)

    assert status["session_link"]["status"] == "created"
    assert report["card"]["column_id"] == "review"
    assert report["session_link"]["ready_for_review"] is True
    assert report["session_link"]["merge_report"]["merge_strategy"] == "manual_conflict_report"
    assert applied["card"]["column_id"] == "done"
    assert applied["session_link"]["terminal_state"] == "applied"

    stored = KanbanStore().require_card(card["id"])
    assert stored["agent_session_id"] == session_id
    assert stored["agent_status"] == "applied"


def test_task_board_agent_session_tool_and_dispatcher_are_registered(tmp_path):
    _reset_sessions()
    ToolRegistry._instance = None
    context = {"conversation_workspace_dir": str(tmp_path / "workspace")}
    card = TaskBoardController().run({"action": "create", "title": "Registry task"}, context)["changed"]
    registry = ToolRegistry()

    tool = registry.get("tool_task_board_agent_session")
    executed = ToolExecutor().execute(
        "tool_task_board_agent_session",
        {"action": "start", "card_id": card["id"]},
        {**context, "profile_policy": {"yolo_mode": True}, "principal_id": "defaultspack"},
    )

    from ecosystem.defaultspack.domain.function_runtime.dispatcher import run_defaultspack_function

    dispatched_card = TaskBoardController().run(
        {"action": "create", "title": "Dispatcher task"},
        {"conversation_workspace_dir": str(tmp_path / "dispatcher")},
    )["changed"]
    dispatched = run_defaultspack_function(
        "tool_task_board_agent_session",
        {"action": "start", "card_id": dispatched_card["id"]},
        {"conversation_workspace_dir": str(tmp_path / "dispatcher")},
    )

    assert tool is not None
    assert tool["execution"]["handler"] == "domain.tool.task_board_agent_session:tool_task_board_agent_session"
    properties = tool["schema"]["parameters"]["properties"]
    for key in ("board_id", "kanban_board_id", "scope", "scope_type", "scope_id"):
        assert key in properties
    assert executed["is_error"] is False
    assert executed["widget"]["type"] == "task_board_agent_session"
    assert dispatched["status"] == "ok"
    assert dispatched["data"]["widget"]["session_link"]["session_id"].startswith("multi_")


def test_task_board_agent_session_maps_custom_board_columns_and_done_state(tmp_path):
    _reset_sessions()
    context = {"conversation_workspace_dir": str(tmp_path / "workspace")}
    task_board = TaskBoardController()
    task_board.run({"action": "configure", "columns": ["Inbox", "Active", "QA Review", "Completed"]}, context)
    card = task_board.run({"action": "create", "title": "Custom board task"}, context)["changed"]
    controller = TaskBoardAgentSessionController()

    started = controller.run({"action": "start", "card_id": card["id"], "max_turns": 1}, context)
    report = controller.run({"action": "merge_report", "card_id": card["id"]}, context)
    applied = controller.run({"action": "apply", "card_id": card["id"]}, context)
    board = task_board.run({"action": "list"}, context)

    assert started["card"]["column_id"] == "active"
    assert report["card"]["column_id"] == "qa-review"
    assert applied["card"]["column_id"] == "completed"
    assert board["summary"].endswith("(0 open)")


def test_task_board_agent_session_forwards_board_id_and_scope_selectors(tmp_path):
    _reset_sessions()
    context = {"conversation_workspace_dir": str(tmp_path / "workspace")}
    task_board = TaskBoardController()
    scoped = {"type": "workspace", "id": "ws-scoped-board"}
    scoped_card = task_board.run({"action": "create", "title": "Scoped board task", "scope": scoped}, context)["changed"]
    task_board.run({"action": "create", "title": "Ambient board task"}, context)
    controller = TaskBoardAgentSessionController()

    started = controller.run(
        {"action": "start", "card_id": scoped_card["id"], "scope": scoped, "max_turns": 1},
        context,
    )
    status = controller.run(
        {"action": "status", "card_id": scoped_card["id"], "board_id": scoped_card["board_id"]},
        context,
    )
    ready = controller.run(
        {
            "action": "mark_ready",
            "card_id": scoped_card["id"],
            "scope_type": "workspace",
            "scope_id": "ws-scoped-board",
        },
        context,
    )

    scoped_board = task_board.run({"action": "list", "scope": scoped}, context)
    ambient_board = task_board.run({"action": "list"}, context)

    assert started["card"]["board_id"] == scoped_card["board_id"]
    assert status["card"]["board_id"] == scoped_card["board_id"]
    assert ready["card"]["column_id"] == "review"
    assert scoped_board["cards"][0]["metadata"]["agent_session"]["ready_for_review"] is True
    assert "agent_session" not in ambient_board["cards"][0]["metadata"]


def test_task_board_agent_session_rejects_unknown_explicit_column(tmp_path):
    _reset_sessions()
    context = {"conversation_workspace_dir": str(tmp_path / "workspace")}
    card = TaskBoardController().run({"action": "create", "title": "Explicit column task"}, context)["changed"]
    controller = TaskBoardAgentSessionController()

    try:
        controller.run({"action": "start", "card_id": card["id"], "column": "Nope", "max_turns": 1}, context)
    except ValueError as exc:
        assert "Unknown task board column" in str(exc)
    else:
        raise AssertionError("expected explicit invalid column to fail")
