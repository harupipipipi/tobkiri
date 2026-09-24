"""One-shot legacy Kanban snapshot export for the Wave 10 facade cutover."""

from __future__ import annotations

from typing import Any, Mapping


class LegacyKanbanMigrationError(RuntimeError):
    """Raised when a legacy board cannot be exported for one-shot migration."""


def export_board_snapshot(board_id: str) -> dict[str, Any]:
    """Export one old SQLite board without exposing it as a runtime fallback.

    This is the only permitted defaultspack read of legacy Kanban SQLite state.
    The caller must submit the returned value to the selected global owner with
    an exact migration receipt; this function never invokes that owner itself.
    """

    target = str(board_id or "").strip()
    if not target:
        raise LegacyKanbanMigrationError("legacy Kanban board ID is required")
    from .legacy_snapshot_reader import (
        LegacyKanbanSnapshotError,
        read_board_snapshot,
    )

    try:
        legacy = read_board_snapshot(target)
    except LegacyKanbanSnapshotError as exc:
        raise LegacyKanbanMigrationError(
            f"legacy Kanban board is unavailable: {target}"
        ) from exc
    board = _mapping(legacy.get("board"), "legacy board")
    return {
        "id": str(board.get("board_id") or target),
        "title": str(board.get("title") or target),
        "scope": {
            "type": str(board.get("scope_type") or "profile"),
            "id": str(board.get("scope_id") or "default"),
        },
        "metadata": _mapping_or_empty(board.get("metadata")),
        "created_at": board.get("created_at"),
        "updated_at": board.get("updated_at"),
        "columns": [_column(item) for item in _records(legacy.get("columns"))],
        "cards": [_card(item) for item in _records(legacy.get("cards"))],
        "events": [_event(item) for item in _records(legacy.get("events"))],
    }


def _column(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(value.get("column_id") or value.get("id") or ""),
        "title": str(value.get("title") or "Column"),
        "position": value.get("position"),
        "done": bool(value.get("done")),
        "wip_limit": value.get("wip_limit"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
    }


def _card(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(value),
        "id": str(value.get("card_id") or value.get("id") or ""),
        "archived_at_ms": value.get("archived_at"),
    }


def _event(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(value),
        "id": str(value.get("event_id") or value.get("id") or ""),
        "type": str(value.get("event_type") or value.get("type") or "event"),
    }


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LegacyKanbanMigrationError(f"{label} is invalid")
    return value


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
