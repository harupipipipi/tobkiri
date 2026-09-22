from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.tool.task_board import TaskBoardController
from domain.tool.task_board_agent_session import (
    TaskBoardAgentSessionController,
    tool_task_board_agent_session,
)


_TASK_BOARD_CODE = "TASK_BOARD_LEGACY_TOOL_DEPRECATED"
_AGENT_SESSION_CODE = "TASK_BOARD_AGENT_SESSION_LEGACY_TOOL_DEPRECATED"


def _assert_deprecated(result: dict[str, object], code: str) -> None:
    assert result["status"] == "deprecated"
    assert result["code"] == code
    assert result["recovery"]


def test_task_board_card_starts_tracks_and_applies_agent_session(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardController().run(
            {"action": "create", "title": "legacy card"},
            {"profile_id": "profile-a"},
        ),
        _TASK_BOARD_CODE,
    )
    _assert_deprecated(
        TaskBoardAgentSessionController().run(
            {"action": "start", "card_id": "legacy-card"},
            {"profile_id": "profile-a"},
        ),
        _AGENT_SESSION_CODE,
    )


def test_task_board_agent_session_tool_and_dispatcher_are_registered(tmp_path):
    del tmp_path
    response = tool_task_board_agent_session(
        {"action": "start", "card_id": "legacy-card"},
        {"profile_id": "profile-a"},
    )
    assert response["is_error"] is True
    assert response["error_code"] == _AGENT_SESSION_CODE
    assert response["widget"]["status"] == "deprecated"


def test_task_board_agent_session_maps_custom_board_columns_and_done_state(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardAgentSessionController().run(
            {"action": "apply", "card_id": "legacy-card", "column": "Completed"},
            {"profile_id": "profile-a"},
        ),
        _AGENT_SESSION_CODE,
    )


def test_task_board_agent_session_forwards_board_id_and_scope_selectors(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardAgentSessionController().run(
            {
                "action": "status",
                "card_id": "legacy-card",
                "board_id": "legacy-board",
                "scope": {"type": "workspace", "id": "legacy-workspace"},
            },
            {"profile_id": "profile-a"},
        ),
        _AGENT_SESSION_CODE,
    )


def test_task_board_agent_session_rejects_unknown_explicit_column(tmp_path):
    del tmp_path
    result = TaskBoardAgentSessionController().run(
        {"action": "start", "card_id": "legacy-card", "column": "Nope"},
        {"profile_id": "profile-a"},
    )
    _assert_deprecated(result, _AGENT_SESSION_CODE)
    assert "Unknown task board column" not in str(result)
