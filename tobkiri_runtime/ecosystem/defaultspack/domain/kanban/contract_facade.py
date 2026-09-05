"""Finite defaultspack facade over the selected Kanban global owner."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)
from domain.safety import approval
from domain.tool_policy.internal_context import tool_server_approval_context_is_internal

from .legacy_migration import LegacyKanbanMigrationError, export_board_snapshot

AUTHORITY = "rumi.service.host.authorize.v1"
RESOURCE = "rumi.resource.kanban.v1"
ACTION = "rumi.action.kanban.v1"
STATE_PACK_ID = "rumi_kanban_state_store_pack"


class KanbanFacadeError(RuntimeError):
    """Return a stable legacy-route diagnostic without falling back to SQLite."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = status


class KanbanContractFacade:
    """Translate finite legacy operations into selected Kanban contracts."""

    def __init__(self, input_data: Mapping[str, Any], context: Mapping[str, Any]) -> None:
        self.input = dict(input_data)
        self.context = dict(context)
        self.profile_id = _profile_id()

    def run(self, action: str) -> dict[str, Any]:
        """Handle one old route without importing a legacy service/runtime."""

        if action in {"list_boards", "boards"}:
            return {"boards": [_legacy_summary(item) for item in self._list()["boards"]]}
        if action in {"bootstrap_board", "bootstrap"}:
            return self._bootstrap()
        if action == "get_board":
            return self._board(_required_id(self.input, "board_id"))
        if action == "migrate_board":
            return self._migrate(_required_id(self.input, "board_id"))
        if action == "update_board":
            board_id = _required_id(self.input, "board_id")
            self._mutate("board.update", {"board_id": board_id, "updates": _updates(self.input)})
            return self._board(board_id)
        if action == "create_card":
            board_id = _required_id(self.input, "board_id")
            board = self._raw_board(board_id)
            record = _card_record(self.input, _first_column(board))
            self._mutate("card.upsert", {"board_id": board_id, "record": record})
            return _legacy_card(record)
        if action in {"update_card", "move_card", "delete_card"}:
            return self._card_action(action, _required_id(self.input, "card_id"))
        if action == "create_column":
            return self._column_action(action, _required_id(self.input, "board_id"))
        if action in {"update_column", "delete_column"}:
            return self._column_action(action, _required_id(self.input, "column_id"))
        deprecated = {
            "import_conversation", "sync_conversation", "sync_runs", "sync",
            "agent_start", "agent_status", "agent_ready", "agent_apply",
            "agent_dismiss",
        }
        if action in deprecated:
            raise KanbanFacadeError(
                "KANBAN_LEGACY_ACTION_DEPRECATED",
                "legacy Kanban agent and sync actions require the Wave 10 adapter route",
                410,
            )
        raise KanbanFacadeError("INVALID_INPUT", f"unsupported kanban action: {action}")

    def _bootstrap(self) -> dict[str, Any]:
        scope = self.input.get("scope") if isinstance(self.input.get("scope"), Mapping) else {}
        scope_type = str(self.input.get("scope_type") or scope.get("type") or "global")
        scope_id = str(self.input.get("scope_id") or scope.get("id") or "default")
        for item in self._list()["boards"]:
            current = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
            if current.get("type") == scope_type and current.get("id") == scope_id:
                return self._board(str(item["id"]))
        board_id = "legacy-" + _hash(f"{scope_type}\0{scope_id}")[:40]
        self._mutate(
            "board.create",
            {
                "board_id": board_id,
                "title": str(self.input.get("title") or scope_id),
                "scope": {"type": scope_type, "id": scope_id},
                "metadata": {},
                "columns": None,
            },
        )
        return self._board(board_id)

    def _migrate(self, board_id: str) -> dict[str, Any]:
        try:
            snapshot = export_board_snapshot(board_id)
        except LegacyKanbanMigrationError as exc:
            raise KanbanFacadeError("KANBAN_MIGRATION_SOURCE_UNAVAILABLE", str(exc), 404) from exc
        self._mutate("migration.import_snapshot", {"snapshot": snapshot})
        return self._board(board_id)

    def _card_action(self, action: str, card_id: str) -> dict[str, Any]:
        found = self._invoke(RESOURCE, "find_card", {"card_id": card_id})
        if not isinstance(found, Mapping):
            raise KanbanFacadeError("KANBAN_MIGRATION_REQUIRED", "board must be migrated", 409)
        board_id, card = str(found["board_id"]), dict(found["card"])
        if action == "delete_card":
            self._mutate("card.delete", {"board_id": board_id, "record_id": card_id})
            return {"deleted": True, "card_id": card_id, "card": _legacy_card(card)}
        record = {**card, **_card_updates(self.input)}
        if action == "move_card":
            record["column_id"] = str(self.input.get("column_id") or record["column_id"])
            record["position"] = self.input.get("position", record.get("position", 0))
        result = self._mutate("card.upsert", {"board_id": board_id, "record": record})
        return _legacy_card(result.get("card") or record)

    def _column_action(self, action: str, column_id: str) -> dict[str, Any]:
        if action == "create_column":
            board_id = _required_id(self.input, "board_id")
            record = {"id": "legacy-column-" + uuid.uuid4().hex, **_updates(self.input)}
        else:
            found = self._invoke(RESOURCE, "find_column", {"column_id": column_id})
            if not isinstance(found, Mapping):
                raise KanbanFacadeError("KANBAN_MIGRATION_REQUIRED", "board must be migrated", 409)
            board_id, record = str(found["board_id"]), dict(found["column"])
            if action == "delete_column":
                self._mutate("column.delete", {"board_id": board_id, "record_id": column_id})
                return {"deleted": True, "column_id": column_id, "column": _legacy_column(record)}
            record.update(_updates(self.input))
        result = self._mutate("column.upsert", {"board_id": board_id, "record": record})
        return _legacy_column(result.get("column") or record)

    def _board(self, board_id: str) -> dict[str, Any]:
        return _legacy_board(self._raw_board(board_id))

    def _raw_board(self, board_id: str) -> dict[str, Any]:
        value = self._invoke(RESOURCE, "get", {"board_id": board_id})
        if not isinstance(value, Mapping):
            raise KanbanFacadeError("KANBAN_MIGRATION_REQUIRED", "board must be migrated", 409)
        return dict(value)

    def _list(self) -> dict[str, Any]:
        value = self._invoke(RESOURCE, "list", {})
        if not isinstance(value, Mapping):
            raise KanbanFacadeError(
                "KANBAN_OWNER_UNAVAILABLE",
                "Kanban owner returned invalid data",
                503,
            )
        return dict(value)

    def _mutate(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        state = self._list()
        exact = {"expected_revision": int(state.get("revision") or 0), **arguments}
        receipt = _receipt(self.input, self.context, self.profile_id, name, exact)
        result = self._invoke(ACTION, name, {**exact, **receipt})
        return dict(result) if isinstance(result, Mapping) else {}

    def _invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> Any:
        return _invoke(contract, operation, {"profile_id": self.profile_id, **dict(payload)})


def _receipt(
    input_data: Mapping[str, Any],
    context: Mapping[str, Any],
    profile_id: str,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    internal = tool_server_approval_context_is_internal(dict(context))
    if not internal:
        token = str(input_data.get("approval_token") or "")
        if not token:
            raise KanbanFacadeError("APPROVAL_REQUIRED", "approval token is required", 403)
        verified = approval.verify_execution_token(
            token,
            f"kanban.{name}",
            approval.hash_arguments(dict(input_data)),
            consume=True,
        )
        if not verified.valid:
            raise KanbanFacadeError("APPROVAL_INVALID", "approval token is invalid", 403)
    caller_id = str(
        context.get("principal_id")
        or context.get("user_id")
        or "defaultspack.local_user"
    )
    scope = {
        "service_pack_id": STATE_PACK_ID,
        "operation": f"kanban.state.{name}",
        "authority": "kanban.state.manage",
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": f"blocks.kanban.api.{name}",
        "profile_id": profile_id,
        "workspace_id": "",
        "session_id": str(context.get("session_id") or ""),
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = _invoke(AUTHORITY, "authorize", scope)
    if not isinstance(issued, Mapping) or not issued.get("authorized"):
        raise KanbanFacadeError(
            "KANBAN_AUTHORITY_DENIED",
            str((issued or {}).get("reason") or "Kanban state denied"),
            403,
        )
    return {
        "authority_receipt": str(issued.get("receipt") or ""),
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": scope["caller_function_id"],
        "session_id": scope["session_id"],
    }


def _invoke(contract: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise KanbanFacadeError("KANBAN_OWNER_UNAVAILABLE", "Kanban owner is unavailable", 503)
    return invoke_global_contract(registry, contract, operation, payload)


def _profile_id() -> str:
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        raise KanbanFacadeError("KANBAN_OWNER_UNAVAILABLE", "resolved profile is unavailable", 503)
    return captured_profile_id(session)


def _required_id(value: Mapping[str, Any], key: str) -> str:
    result = str(value.get(key) or value.get("id") or "").strip()
    if not result:
        raise KanbanFacadeError("INVALID_INPUT", f"{key} is required")
    return result


def _updates(value: Mapping[str, Any]) -> dict[str, Any]:
    updates = value.get("updates")
    if isinstance(updates, Mapping):
        return dict(updates)
    allowed = {"title", "position", "done", "wip_limit", "metadata"}
    return {key: item for key, item in value.items() if key in allowed}


def _card_record(value: Mapping[str, Any], column_id: str) -> dict[str, Any]:
    return {
        "id": str(value.get("card_id") or "legacy-card-" + uuid.uuid4().hex),
        "column_id": str(value.get("column_id") or column_id),
        **_card_updates(value),
    }


def _card_updates(value: Mapping[str, Any]) -> dict[str, Any]:
    updates = _updates(value)
    allowed = {
        "title", "description", "priority", "assignee", "due_at", "source_type",
        "source_id", "conversation_id", "workspace_id", "company_id",
        "agent_run_id", "agent_session_id", "agent_status", "branch", "pr_url",
        "labels", "checklist", "depends_on", "blocked_by", "metadata", "position",
        "column_id",
    }
    return {key: value.get(key) for key in allowed if key in value} | updates


def _first_column(board: Mapping[str, Any]) -> str:
    columns = board.get("columns") if isinstance(board.get("columns"), Mapping) else {}
    if not columns:
        raise KanbanFacadeError("KANBAN_INVALID_BOARD", "board has no columns", 409)
    first = min(
        columns.values(),
        key=lambda item: int(item.get("position") or 0),
    )
    return str(first.get("id") or "")


def _legacy_board(board: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "board": _legacy_summary(board),
        "columns": [_legacy_column(item) for item in board.get("columns", {}).values()],
        "cards": [_legacy_card(item) for item in board.get("cards", {}).values()],
        "events": [_legacy_event(item) for item in board.get("events", [])],
    }


def _legacy_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    scope = value.get("scope") if isinstance(value.get("scope"), Mapping) else {}
    return {
        **dict(value),
        "board_id": value.get("id"),
        "scope_type": scope.get("type"),
        "scope_id": scope.get("id"),
    }


def _legacy_column(value: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(value), "column_id": value.get("id")}


def _legacy_card(value: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(value), "card_id": value.get("id")}


def _legacy_event(value: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(value), "event_id": value.get("id"), "event_type": value.get("type")}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
