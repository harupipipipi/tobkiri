"""Normalize native Kanban requests and project canonical owner records."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Mapping

_PACK = "rumi_kanban_state_store_pack"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def _target(binding_id: str, write: bool = False) -> tuple[str, str, str, str, str]:
    suffix = "action" if write else "resource"
    return (
        binding_id,
        f"tobkiri.{suffix}.kanban.v1",
        f"{_PACK}.kanban-state-{suffix}",
        f"{_PACK}.kanban-state.{suffix}",
        f"{_PACK}.kanban-state.{suffix}",
    )


KANBAN_LIST_TARGET = _target("defaults.kanban.list")
KANBAN_GET_TARGET = _target("defaults.kanban.get")
KANBAN_CREATE_TARGET = _target("defaults.kanban.create", True)
KANBAN_CARD_TARGET = _target("defaults.kanban.card-upsert", True)
KANBAN_MOVE_TARGET = _target("defaults.kanban.card-move", True)
KANBAN_DELETE_TARGET = _target("defaults.kanban.card-delete", True)
KANBAN_IMPORT_TARGET = _target("defaults.kanban.import-conversation", True)
KANBAN_TARGETS = {
    KANBAN_LIST_TARGET,
    KANBAN_GET_TARGET,
    KANBAN_CREATE_TARGET,
    KANBAN_CARD_TARGET,
    KANBAN_MOVE_TARGET,
    KANBAN_DELETE_TARGET,
    KANBAN_IMPORT_TARGET,
}


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("Kanban identity is invalid")
    return value


def normalize_kanban_request(
    target: tuple[str, str, str, str, str],
    payload: Mapping[str, Any],
    *,
    profile_id: str,
) -> dict[str, Any]:
    """Bind finite owner requests to a captured Profile with explicit CAS."""
    if not profile_id or target not in KANBAN_TARGETS:
        raise ValueError("Kanban request requires a captured Profile and target")
    control = {
        "approved",
        "authority_receipt",
        "profile_id",
        "caller_id",
        "caller_pack_id",
        "caller_function_id",
        "session_id",
    }
    if control.intersection(payload):
        raise ValueError("Kanban control fields are not public input")
    result: dict[str, Any] = {"profile_id": profile_id}
    if target == KANBAN_LIST_TARGET:
        if set(payload) - {"scope_type", "scope_id", "bootstrap"}:
            raise ValueError("Kanban list input is invalid")
        if payload.get("bootstrap") not in {None, False, "false"}:
            raise ValueError("Kanban reads cannot bootstrap a board")
        return {**result, "operation": "list"}
    if target == KANBAN_GET_TARGET:
        if set(payload) != {"board_id"}:
            raise ValueError("Kanban get input is invalid")
        return {
            **result,
            "operation": "get",
            "board_id": _identifier(payload["board_id"]),
        }
    revision = payload.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Kanban mutation requires expected_revision")
    result["expected_revision"] = revision
    if target == KANBAN_CREATE_TARGET:
        if set(payload) - {"scope_type", "scope_id", "title", "expected_revision"}:
            raise ValueError("Kanban create input is invalid")
        scope_type = payload.get("scope_type", "global")
        scope_id = _identifier(payload.get("scope_id", "default"))
        if scope_type not in {
            "global",
            "group",
            "conversation",
            "workspace",
            "company",
        }:
            raise ValueError("Kanban scope is invalid")
        identity = hashlib.sha256(f"{scope_type}:{scope_id}".encode()).hexdigest()[:32]
        return {
            **result,
            "operation": "board.create",
            "board_id": f"board-{identity}",
            "title": str(payload.get("title") or "Kanban"),
            "scope": {"type": scope_type, "id": scope_id},
        }
    result["board_id"] = _identifier(payload.get("board_id"))
    if target == KANBAN_IMPORT_TARGET:
        allowed = {
            "board_id",
            "expected_revision",
            "conversation_id",
            "column_id",
            "title",
            "model",
            "workspace_id",
            "company_id",
            "use_ai",
        }
        if set(payload) - allowed:
            raise ValueError("Kanban import input is invalid")
        if payload.get("use_ai") not in {None, False}:
            raise ValueError("Kanban import supports deterministic local extraction")
        result.update(
            {
                key: payload[key]
                for key in allowed
                if key in payload and key not in {"use_ai", "expected_revision"}
            }
        )
        result["conversation_id"] = _identifier(payload.get("conversation_id"))
        if payload.get("column_id") is not None:
            result["column_id"] = _identifier(payload["column_id"])
        return {**result, "operation": "conversation.import"}
    fields = {"board_id", "expected_revision", "card_id"}
    if target == KANBAN_DELETE_TARGET:
        if set(payload) - fields:
            raise ValueError("Kanban delete input is invalid")
        return {
            **result,
            "operation": "card.delete",
            "record_id": _identifier(payload.get("card_id")),
        }
    if target == KANBAN_MOVE_TARGET:
        if set(payload) - fields - {"column_id", "position"}:
            raise ValueError("Kanban move input is invalid")
        return {
            **result,
            "operation": "card.move",
            "record_id": _identifier(payload.get("card_id")),
            "record": {
                "column_id": _identifier(payload.get("column_id")),
                "position": payload.get("position", 0),
            },
        }
    allowed = {
        "column_id",
        "title",
        "description",
        "priority",
        "labels",
        "checklist",
        "conversation_id",
        "workspace_id",
        "company_id",
    }
    if set(payload) - fields - allowed:
        raise ValueError("Kanban card input is invalid")
    record = {key: payload[key] for key in allowed if key in payload}
    record["column_id"] = _identifier(payload.get("column_id"))
    if not isinstance(record.get("title"), str) or not record["title"].strip():
        raise ValueError("Kanban card title is required")
    return {
        **result,
        "operation": "card.upsert",
        "record": record,
        "record_id": _identifier(payload.get("card_id") or uuid.uuid4().hex),
    }


def present_kanban_result(
    target: tuple[str, str, str, str, str],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Project owner ids into the existing native board/card interface."""
    if result.get("state") == "error":
        return dict(result)
    revision = result.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Kanban owner revision is invalid")
    if target == KANBAN_LIST_TARGET:
        boards = result.get("boards")
        if not isinstance(boards, list):
            raise ValueError("Kanban owner boards are invalid")
        return {"boards": [_board(item) for item in boards], "revision": revision}
    board = result.get("board")
    if isinstance(board, Mapping):
        board_id = _identifier(board.get("id"))
        return {
            "board": _board(board),
            "revision": revision,
            "columns": [
                _record(item, "column_id", board_id)
                for item in board.get("columns", {}).values()
            ],
            "cards": [
                _record(item, "card_id", board_id)
                for item in board.get("cards", {}).values()
            ],
            "events": list(board.get("events", [])),
        }
    card = result.get("card")
    if isinstance(card, Mapping):
        return {
            **dict(card),
            "card_id": _identifier(card.get("id")),
            "revision": revision,
            **({"imported": result["imported"]} if "imported" in result else {}),
        }
    return dict(result)


def _board(value: Mapping[str, Any]) -> dict[str, Any]:
    scope = value.get("scope", {})
    return {
        **dict(value),
        "board_id": _identifier(value.get("id")),
        "scope_type": scope.get("type", "global"),
        "scope_id": scope.get("id", "default"),
        "created_at": value.get("created_at_ms"),
        "updated_at": value.get("updated_at_ms"),
    }


def _record(value: Mapping[str, Any], key: str, board_id: str) -> dict[str, Any]:
    return {
        **dict(value),
        key: _identifier(value.get("id")),
        "board_id": board_id,
        "created_at": value.get("created_at_ms"),
        "updated_at": value.get("updated_at_ms"),
    }
