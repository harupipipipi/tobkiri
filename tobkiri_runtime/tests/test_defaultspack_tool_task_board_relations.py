from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.kanban import api
from domain.tool.task_board import TaskBoardController, tool_task_board


_TASK_BOARD_CODE = "TASK_BOARD_LEGACY_TOOL_DEPRECATED"


def test_task_board_tracks_dependencies_blockers_and_subtasks(tmp_path):
    del tmp_path
    result = TaskBoardController().run(
        {
            "action": "block",
            "card_id": "legacy-card",
            "blocked_by": ["legacy-blocker"],
            "subtasks": [{"id": "subtask", "title": "legacy"}],
        },
        {},
    )
    assert result["status"] == "deprecated"
    assert result["code"] == _TASK_BOARD_CODE
    assert result["action"] == "block"


def test_tool_task_board_relations_manifest_loads_and_executes(tmp_path):
    del tmp_path
    response = tool_task_board(
        {"action": "create", "title": "legacy root", "depends_on": ["legacy"]},
        {},
    )
    assert response["is_error"] is True
    assert response["error_code"] == _TASK_BOARD_CODE
    assert response["widget"]["status"] == "deprecated"
    assert importlib.util.find_spec("domain.function_runtime.bridge") is None


def test_task_board_block_action_keeps_dependency_aliases_in_summary(tmp_path):
    del tmp_path
    response = api.run(
        {
            "action": "block",
            "card_id": "legacy-card",
            "dependencies": ["legacy-dependency"],
        },
        {},
    )
    assert response["status"] == "error"
    assert response["error"]["code"] == "KANBAN_OWNER_UNAVAILABLE"
