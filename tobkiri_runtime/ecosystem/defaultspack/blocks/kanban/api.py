"""Finite legacy HTTP alias for the selected Kanban contracts."""

from __future__ import annotations

from typing import Any, Mapping

from blocks._common import error, ok
from domain.kanban.contract_facade import KanbanContractFacade, KanbanFacadeError


def run(input_data: Any, context: Any = None) -> dict[str, Any]:
    """Dispatch an old route without constructing a primary Kanban service."""

    if not isinstance(input_data, dict):
        return _invalid("input_data must be a dict")
    payload = dict(input_data)
    try:
        return ok(KanbanContractFacade(payload, _context(context)).run(_action(payload)))
    except KanbanFacadeError as exc:
        response = error(str(exc), exc.code)
        response["_http_status"] = exc.http_status
        return response
    except Exception as exc:
        return error("kanban compatibility facade failed: " + str(exc), "KANBAN_FACADE_ERROR")


def _action(payload: Mapping[str, Any]) -> str:
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    if action:
        return action
    method = str(payload.get("_method") or payload.get("_actual_method") or "").upper()
    if method == "GET" and payload.get("board_id"):
        return "get_board"
    if method == "GET":
        return "list_boards"
    if method == "PUT" and payload.get("board_id"):
        return "update_board"
    if method == "PUT" and payload.get("card_id"):
        return "update_card"
    if method == "PUT" and payload.get("column_id"):
        return "update_column"
    if method == "DELETE" and payload.get("card_id"):
        return "delete_card"
    if method == "DELETE" and payload.get("column_id"):
        return "delete_column"
    return "list_boards"


def _context(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _invalid(message: str) -> dict[str, Any]:
    response = error(message, "INVALID_INPUT")
    response["_http_status"] = 400
    return response
