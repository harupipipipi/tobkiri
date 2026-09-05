"""Finite legacy HTTP alias for the selected Kanban contracts."""

from __future__ import annotations

import os
from typing import Any, Mapping

from blocks._common import error, ok
from domain.kanban.contract_facade import KanbanContractFacade, KanbanFacadeError
from domain.kanban.store import (
    KanbanOwnerUnavailable,
    StateStoreFactory,
    default_db_path,
)


def run(input_data: Any, context: Any = None) -> dict[str, Any]:
    """Dispatch an old route without constructing a primary Kanban service."""

    if not isinstance(input_data, dict):
        return _invalid("input_data must be a dict")
    payload = dict(input_data)
    try:
        compatibility = _explicit_adapter_result(payload, _context(context))
        if compatibility is not None:
            return ok(compatibility)
        return ok(KanbanContractFacade(payload, _context(context)).run(_action(payload)))
    except KanbanOwnerUnavailable as exc:
        response = error(str(exc), "KANBAN_OWNER_UNAVAILABLE")
        response["_http_status"] = 503
        return response
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


def _explicit_adapter_result(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Use the canonical owner adapter only for an explicit local path.

    The normal HTTP route remains the approval-aware global contract facade.
    Older local callers select this bounded adapter by setting the documented
    compatibility path; without that setting there is no hidden Kanban store.
    """

    if not os.environ.get("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", "").strip():
        return None
    from domain.kanban.service import KanbanService

    state_store_factory = _state_store_factory(context)
    service = KanbanService(
        db_path=default_db_path(),
        state_store_factory=state_store_factory,
    )
    action = _action(payload)
    if action in {"list_boards", "boards"}:
        return service.list_boards(dict(payload))
    if action in {"bootstrap_board", "bootstrap"}:
        return service.bootstrap_board(dict(payload))
    if action == "get_board":
        return service.get_board(str(payload.get("board_id") or ""))
    if action == "update_board":
        return service.update_board(str(payload.get("board_id") or ""), dict(payload))
    if action == "create_card":
        return service.create_card(str(payload.get("board_id") or ""), dict(payload))
    if action == "update_card":
        return service.update_card(str(payload.get("card_id") or ""), dict(payload))
    if action == "delete_card":
        return service.delete_card(str(payload.get("card_id") or ""))
    if action == "move_card":
        return service.move_card(str(payload.get("card_id") or ""), dict(payload))
    if action == "import_conversation":
        return service.import_conversation(str(payload.get("board_id") or ""), dict(payload))
    if action == "sync_conversation":
        from domain.kanban.chat_sync import sync_conversation_kanban

        result = sync_conversation_kanban(
            str(payload.get("conversation_id") or payload.get("source_id") or ""),
            reason=str(payload.get("reason") or "kanban_api"),
            db_path=default_db_path(),
            state_store_factory=state_store_factory,
        )
        return result or {}
    if action in {"sync_runs", "sync"}:
        return service.sync_runs(str(payload.get("board_id") or ""), dict(payload))
    if action == "agent_status":
        return service.agent_status(str(payload.get("card_id") or ""))
    if action == "agent_start":
        return service.agent_start(str(payload.get("card_id") or ""), dict(payload))
    if action == "agent_ready":
        return service.agent_ready(str(payload.get("card_id") or ""), dict(payload))
    if action == "agent_apply":
        return service.agent_apply(str(payload.get("card_id") or ""), dict(payload))
    if action == "agent_dismiss":
        return service.agent_dismiss(str(payload.get("card_id") or ""), dict(payload))
    if action == "create_column":
        return service.create_column(str(payload.get("board_id") or ""), dict(payload))
    if action == "update_column":
        return service.update_column(str(payload.get("column_id") or ""), dict(payload))
    if action == "delete_column":
        return service.delete_column(str(payload.get("column_id") or ""))
    return None


def _state_store_factory(context: Mapping[str, Any]) -> StateStoreFactory:
    """Return the caller-selected owner factory or fail closed."""

    value = context.get("kanban_state_store_factory") or context.get(
        "state_store_factory"
    )
    if not callable(value):
        raise KanbanOwnerUnavailable(
            "compatibility Kanban path requires an injected state-store factory"
        )
    return value


def _invalid(message: str) -> dict[str, Any]:
    response = error(message, "INVALID_INPUT")
    response["_http_status"] = 400
    return response
