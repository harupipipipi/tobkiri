from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.kanban.store import KanbanOwnerUnavailable, KanbanStore  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.tool.task_board import TaskBoardController, tool_task_board  # noqa: E402


_TASK_BOARD_CODE = "TASK_BOARD_LEGACY_TOOL_DEPRECATED"


def _assert_deprecated(result: dict[str, object], action: str) -> None:
    assert result["status"] == "deprecated"
    assert result["code"] == _TASK_BOARD_CODE
    assert result["action"] == action
    assert result["recovery"]


def test_task_board_controller_persists_workspace_board_and_moves_cards(tmp_path):
    workspace = tmp_path / "conversation" / "workspace"
    result = TaskBoardController().run(
        {"action": "create", "title": "legacy card"},
        {"conversation_workspace_dir": str(workspace)},
    )
    _assert_deprecated(result, "create")
    assert not workspace.exists()


def test_task_board_and_kanban_service_share_workspace_board(tmp_path, monkeypatch):
    del monkeypatch
    with pytest.raises(KanbanOwnerUnavailable, match="injected state-store factory"):
        KanbanStore(tmp_path / "kanban.db")


def test_tool_executor_task_board_shares_env_kanban_api_board(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    response = tool_task_board({"action": "create", "title": "legacy card"}, {})
    assert response["is_error"] is True
    assert response["error_code"] == _TASK_BOARD_CODE
    assert response["widget"]["status"] == "deprecated"


def test_task_board_ignores_untrusted_context_db_path(tmp_path, monkeypatch):
    redirected = tmp_path / "redirected-kanban.db"
    result = TaskBoardController().run(
        {"action": "create", "title": "legacy card"},
        {"kanban_db_path": str(redirected)},
    )
    _assert_deprecated(result, "create")
    assert not redirected.exists()


def test_task_board_lists_cards_created_by_kanban_service(tmp_path, monkeypatch):
    del monkeypatch
    with pytest.raises(KanbanOwnerUnavailable, match="injected state-store factory"):
        KanbanStore(tmp_path / "kanban.db")


def test_task_board_imports_legacy_json_once_into_kanban(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "task_board.json").write_text("{}", encoding="utf-8")
    result = TaskBoardController().run(
        {"action": "list"},
        {"conversation_workspace_dir": str(workspace)},
    )
    _assert_deprecated(result, "list")
    assert (workspace / "task_board.json").read_text(encoding="utf-8") == "{}"


def test_task_board_imports_legacy_json_even_when_board_has_existing_cards(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / "task_board.json"
    legacy.write_text('{"cards": [{"title": "legacy"}]}', encoding="utf-8")
    result = TaskBoardController().run(
        {"action": "list"},
        {"conversation_workspace_dir": str(workspace)},
    )
    _assert_deprecated(result, "list")
    assert legacy.read_text(encoding="utf-8") == '{"cards": [{"title": "legacy"}]}'


def test_task_board_controller_configures_columns_and_rehomes_removed_column_cards(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardController().run({"action": "configure", "columns": ["Done"]}, {}),
        "configure",
    )


def test_task_board_controller_preserves_card_column_on_same_shape_rename(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardController().run({"action": "move", "card_id": "legacy", "column": "Done"}, {}),
        "move",
    )


def test_task_board_metadata_update_deep_merges_by_default(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardController().run(
            {"action": "update", "card_id": "legacy", "metadata": {"key": "value"}},
            {},
        ),
        "update",
    )


def test_task_board_controller_preserves_board_order_and_done_count_for_custom_terminal_columns(tmp_path):
    del tmp_path
    _assert_deprecated(
        TaskBoardController().run({"action": "list"}, {}),
        "list",
    )


def test_tool_registry_and_executor_invoke_manifest_backed_task_board(tmp_path):
    del tmp_path
    tool = ToolRegistry().get("tool_task_board")
    assert tool is None


def test_tool_task_board_function_runtime_registers_and_invokes(tmp_path):
    del tmp_path
    assert importlib.util.find_spec("domain.function_runtime.bridge") is None
    assert importlib.util.find_spec(
        "ecosystem.defaultspack.domain.function_runtime.bridge"
    ) is None
