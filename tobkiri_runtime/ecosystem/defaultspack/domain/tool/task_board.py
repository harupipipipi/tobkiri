"""Deprecated defaultspack alias for the contract-native task board tool."""

from __future__ import annotations

from typing import Any


_CODE = "TASK_BOARD_LEGACY_TOOL_DEPRECATED"
_MESSAGE = (
    "defaultspack task_board no longer owns Kanban state; select the "
    "contract-native task-board adapter for this profile"
)


class TaskBoardController:
    """Preserve the old handler shape without reopening a Kanban writer."""

    def __init__(self, root: Any = None) -> None:
        del root

    def run(
        self,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return an explicit recovery diagnostic for the legacy tool ID."""

        payload = dict(arguments or {})
        request_context = dict(context or {})
        return {
            "status": "deprecated",
            "code": _CODE,
            "message": _MESSAGE,
            "action": str(payload.get("action") or "list"),
            "profile_id": str(request_context.get("profile_id") or ""),
            "recovery": "install or select a task-board adapter using rumi.action.kanban.v1",
        }


def tool_task_board(
    arguments: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the legacy compatibility diagnostic in the historical envelope."""

    result = TaskBoardController().run(arguments, context)
    return {
        "result": result["message"],
        "is_error": True,
        "error_code": result["code"],
        "widget": {"type": "task_board", **result},
    }
