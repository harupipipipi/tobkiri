"""Deprecated defaultspack alias for task-board agent-session integration."""

from __future__ import annotations

from typing import Any


_CODE = "TASK_BOARD_AGENT_SESSION_LEGACY_TOOL_DEPRECATED"
_MESSAGE = (
    "defaultspack task-board agent sessions no longer own Kanban or agent state; "
    "select the contract-native task-board adapter"
)


class TaskBoardAgentSessionController:
    """Preserve the historical entrypoint as a no-write migration diagnostic."""

    def __init__(self, task_board: Any = None) -> None:
        del task_board

    def run(
        self,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a stable recovery response without accessing old state."""

        payload = dict(arguments or {})
        request_context = dict(context or {})
        return {
            "status": "deprecated",
            "code": _CODE,
            "message": _MESSAGE,
            "action": str(payload.get("action") or "status"),
            "profile_id": str(request_context.get("profile_id") or ""),
            "recovery": "use an agent adapter that consumes rumi.action.kanban.v1",
        }


def tool_task_board_agent_session(
    arguments: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the historical tool envelope without an old agent/session writer."""

    result = TaskBoardAgentSessionController().run(arguments, context)
    return {
        "result": result["message"],
        "is_error": True,
        "error_code": result["code"],
        "widget": {"type": "task_board_agent_session", **result},
    }
