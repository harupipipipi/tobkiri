"""Read-only reader for one pre-Wave-10 Kanban SQLite snapshot."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


class LegacyKanbanSnapshotError(RuntimeError):
    """Raised when a legacy snapshot cannot be read without mutation."""


def read_board_snapshot(board_id: str) -> dict[str, Any]:
    """Read one old board in SQLite read-only mode for one-shot export only."""

    target = str(board_id or "").strip()
    if not target:
        raise LegacyKanbanSnapshotError("legacy Kanban board ID is required")
    path = _db_path()
    if not path.exists():
        raise LegacyKanbanSnapshotError("legacy Kanban database is unavailable")
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            board = connection.execute(
                "SELECT * FROM kanban_boards WHERE board_id = ?", (target,)
            ).fetchone()
            if board is None:
                raise LegacyKanbanSnapshotError("legacy Kanban board is unavailable")
            columns = connection.execute(
                "SELECT * FROM kanban_columns WHERE board_id = ? "
                "ORDER BY position ASC, created_at ASC",
                (target,),
            ).fetchall()
            cards = connection.execute(
                "SELECT * FROM kanban_cards WHERE board_id = ? "
                "ORDER BY column_id ASC, position ASC, created_at ASC",
                (target,),
            ).fetchall()
            events = connection.execute(
                "SELECT * FROM kanban_events WHERE board_id = ? "
                "ORDER BY created_at ASC LIMIT 10000",
                (target,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LegacyKanbanSnapshotError("legacy Kanban database is unreadable") from exc
    return {
        "board": _board(board),
        "columns": [_column(row) for row in columns],
        "cards": [_card(row) for row in cards],
        "events": [_event(row) for row in events],
    }


def _db_path() -> Path:
    override = os.environ.get("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "user_data/shared/kanban/kanban.db"


def _board(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["metadata"] = _json(value.pop("metadata_json", "{}"), {})
    return value


def _column(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["done"] = bool(value.get("done"))
    return value


def _card(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key, fallback in (
        ("labels_json", []),
        ("checklist_json", []),
        ("depends_on_json", []),
        ("blocked_by_json", []),
        ("metadata_json", {}),
    ):
        target = key.removesuffix("_json")
        value[target] = _json(value.pop(key, ""), fallback)
    return value


def _event(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["payload"] = _json(value.pop("payload_json", "{}"), {})
    return value


def _json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback
