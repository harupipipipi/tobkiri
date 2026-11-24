"""Small compatibility types for the canonical Kanban adapter.

The durable owner lives in ``rumi_kanban_state_store_pack``.  These helpers
only preserve the finite shape used by older defaultspack callers; they do
not define a second storage schema.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

DEFAULT_COLUMNS = ("Backlog", "Doing", "Review", "Done")
SCOPE_TYPES = {"conversation", "workspace", "company", "group", "global"}


class KanbanError(RuntimeError):
    """Base error for the compatibility projection."""

    code = "KANBAN_ERROR"
    http_status = 400


class KanbanValidationError(KanbanError, ValueError):
    """Raised when a compatibility payload is invalid."""

    code = "INVALID_INPUT"
    http_status = 400


class KanbanNotFoundError(KanbanError, KeyError):
    """Raised when a canonical board, column, or card is absent."""

    code = "NOT_FOUND"
    http_status = 404


def gen_id(prefix: str) -> str:
    """Return a collision-resistant compatibility identifier."""

    return prefix + uuid.uuid4().hex


def now_ms() -> int:
    """Return the current Unix time in milliseconds."""

    return int(time.time() * 1000)


def json_dumps(value: Any) -> str:
    """Serialize a value deterministically for compatibility callers."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: Any, fallback: Any) -> Any:
    """Decode JSON and return a type-compatible fallback on malformed input."""

    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    if isinstance(fallback, list):
        return parsed if isinstance(parsed, list) else fallback
    if isinstance(fallback, dict):
        return parsed if isinstance(parsed, dict) else fallback
    return parsed


def string_list(value: Any) -> list[str]:
    """Normalize a string or list into a stable de-duplicated list."""

    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def clean_list(value: Any) -> list[Any]:
    """Copy a list without accepting arbitrary scalar payloads."""

    return list(value) if isinstance(value, list) else []


def normalize_scope(scope_type: str, scope_id: str) -> tuple[str, str]:
    """Validate and normalize a legacy scope pair."""

    normalized_type = str(scope_type or "global").strip().lower()
    normalized_id = str(scope_id or "default").strip()
    if normalized_type not in SCOPE_TYPES:
        raise KanbanValidationError("invalid scope_type: " + normalized_type)
    if not normalized_id:
        raise KanbanValidationError("scope_id is required")
    return normalized_type, normalized_id


def is_done_column(title: str) -> bool:
    """Return whether a column title represents a completed state."""

    return str(title or "").strip().casefold() in {
        "done",
        "complete",
        "completed",
        "closed",
    }
