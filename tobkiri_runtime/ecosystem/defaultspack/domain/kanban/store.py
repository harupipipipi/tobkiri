"""Compatibility projection over the profile-scoped Kanban state owner.

``KanbanStore`` remains importable for older defaultspack adapters, but it is
not a durable owner.  Every read and write is projected onto
``KanbanStateStore``.  A legacy path is accepted only when the caller supplies
one explicitly (the test/compatibility contract); production callers without
an active persisted profile fail closed instead of silently selecting a local
database.
"""

from __future__ import annotations

import inspect
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Protocol

from core_runtime.resolved_profile_scope import persisted_resolved_profile

from .models import (
    DEFAULT_COLUMNS,
    KanbanNotFoundError,
    KanbanValidationError,
    gen_id,
    is_done_column,
    normalize_scope,
    now_ms,
    string_list,
)


class KanbanOwnerUnavailable(RuntimeError):
    """Raised when an injected canonical Kanban owner is unavailable."""


class StateStoreFactory(Protocol):
    """Construct one profile-scoped owner for the compatibility projection."""

    def __call__(self, profile_id: str, *, root: Path | None = None) -> Any:
        """Return an owner implementing the canonical state-store protocol."""


_OWNER_METHODS = ("snapshot", "get", "find_card", "find_column", "apply")


def default_db_path() -> Path | None:
    """Return the explicitly configured compatibility path, if any."""

    value = os.environ.get("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", "").strip()
    return Path(value).expanduser() if value else None


class KanbanStore:
    """Expose the former store API while delegating to the canonical owner."""

    _instance: "KanbanStore | None" = None
    _class_lock = threading.RLock()

    def __new__(
        cls,
        db_path: str | Path | None = None,
        *,
        state_store_factory: StateStoreFactory | None = None,
    ):
        if db_path is not None or state_store_factory is not None:
            instance = super().__new__(cls)
            instance._initialized = False
            return instance
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        state_store_factory: StateStoreFactory | None = None,
    ) -> None:
        if state_store_factory is None or not callable(state_store_factory):
            raise KanbanOwnerUnavailable(
                "canonical Kanban owner requires an injected state-store factory"
            )
        target = Path(db_path).expanduser() if db_path is not None else None
        profile = persisted_resolved_profile()
        profile_id = str(getattr(profile, "profile_id", "") or "").strip()
        if not profile_id and target is None:
            raise KanbanOwnerUnavailable(
                "canonical Kanban owner requires a resolved profile or explicit adapter path"
            )
        # An explicit path is an adapter boundary, not a request to reopen the
        # removed SQLite schema.  The canonical JSON owner stores beneath the
        # selected directory and remains profile-scoped.
        selected_profile = profile_id or "default"
        root = target.parent if target is not None else None
        if (
            getattr(self, "_initialized", False)
            and self.db_path == target
            and self.profile_id == selected_profile
            and self._state_store_factory is state_store_factory
        ):
            return
        self.db_path = target
        self.profile_id = selected_profile
        try:
            owner = _create_owner(state_store_factory, selected_profile, root)
        except Exception as exc:
            raise KanbanOwnerUnavailable(
                "canonical Kanban owner factory could not provide an owner"
            ) from exc
        if owner is None or any(
            not callable(getattr(owner, name, None)) for name in _OWNER_METHODS
        ):
            raise KanbanOwnerUnavailable(
                "canonical Kanban owner factory returned no usable owner"
            )
        self._state_store_factory = state_store_factory
        self.owner = owner
        self._initialized = True

    def get_or_create_board(
        self,
        scope_type: str,
        scope_id: str,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Return one board for a scope, creating it through the owner."""

        normalized_type, normalized_id = normalize_scope(scope_type, scope_id)
        existing = self.get_board_by_scope(normalized_type, normalized_id)
        if existing is not None:
            return existing
        board_id = gen_id("kb_")
        self._apply(
            "board.create",
            {
                "board_id": board_id,
                "title": str(title or f"Kanban: {normalized_id}").strip(),
                "scope": {"type": normalized_type, "id": normalized_id},
                "metadata": {},
                "columns": list(DEFAULT_COLUMNS),
            },
        )
        self._append_event(board_id, "board.bootstrap", {})
        board = self.get_board(board_id)
        if board is None:
            raise KanbanValidationError("failed to create board")
        return board

    def get_board(self, board_id: str) -> dict[str, Any] | None:
        """Return the legacy summary projection for one board."""

        value = self.owner.get(str(board_id))
        return _legacy_summary(value) if isinstance(value, Mapping) else None

    def get_board_by_scope(
        self,
        scope_type: str,
        scope_id: str,
    ) -> dict[str, Any] | None:
        """Find a board by its canonical scope."""

        normalized_type, normalized_id = normalize_scope(scope_type, scope_id)
        for board in self.owner.snapshot().get("boards") or []:
            scope = board.get("scope") if isinstance(board, Mapping) else {}
            if (
                isinstance(scope, Mapping)
                and scope.get("type") == normalized_type
                and scope.get("id") == normalized_id
            ):
                return self.get_board(str(board.get("id") or ""))
        return None

    def require_board(self, board_id: str) -> dict[str, Any]:
        """Return a board or raise a compatibility not-found error."""

        board = self.get_board(board_id)
        if board is None:
            raise KanbanNotFoundError("board not found: " + str(board_id))
        return board

    def list_boards(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List board summaries from the owner."""

        result = []
        for value in self.owner.snapshot().get("boards") or []:
            if not isinstance(value, Mapping):
                continue
            scope = value.get("scope") if isinstance(value.get("scope"), Mapping) else {}
            if scope_type and scope.get("type") != str(scope_type).casefold():
                continue
            if scope_id and scope.get("id") != str(scope_id):
                continue
            board = self.get_board(str(value.get("id") or ""))
            if board is not None:
                result.append(board)
        return result

    def update_board(self, board_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update a board through the canonical owner."""

        self.require_board(board_id)
        result = self._apply(
            "board.update",
            {"board_id": str(board_id), "updates": dict(updates or {})},
        )
        self._append_event(str(board_id), "board.updated", {"updates": dict(updates or {})})
        board = self.get_board(board_id)
        if board is None:
            raise KanbanNotFoundError("board not found: " + str(board_id))
        del result
        return board

    def list_columns(self, board_id: str) -> list[dict[str, Any]]:
        """Return columns ordered by their canonical position."""

        board = self._raw_board(board_id)
        return sorted(
            (_legacy_column(item) for item in board.get("columns", {}).values()),
            key=lambda item: (int(item.get("position") or 0), str(item.get("id") or "")),
        )

    def ensure_default_columns(self, board_id: str) -> list[dict[str, Any]]:
        """Return the owner's columns, creating defaults only if absent."""

        columns = self.list_columns(board_id)
        if columns:
            return columns
        board = self._raw_board(board_id)
        for position, title in enumerate(DEFAULT_COLUMNS):
            self._apply(
                "column.upsert",
                {
                    "board_id": str(board_id),
                    "record": {
                        "id": f"{board_id}.column.{position}",
                        "title": title,
                        "position": position,
                        "done": is_done_column(title),
                    },
                },
            )
        del board
        self._append_event(str(board_id), "board.bootstrap", {})
        return self.list_columns(board_id)

    def create_column(
        self,
        board_id: str,
        title: str,
        *,
        position: int | None = None,
        done: bool | None = None,
    ) -> dict[str, Any]:
        """Create a column through the owner."""

        self.require_board(board_id)
        clean_title = str(title or "").strip()
        if not clean_title:
            raise KanbanValidationError("title is required")
        columns = self.list_columns(board_id)
        target_position = len(columns) if position is None else max(0, int(position))
        record = {
            "id": gen_id("kcol_"),
            "title": clean_title,
            "position": target_position,
            "done": is_done_column(clean_title) if done is None else bool(done),
        }
        result = self._apply(
            "column.upsert",
            {"board_id": str(board_id), "record": record},
        )
        self._append_event(str(board_id), "column.created", {"column_id": record["id"]})
        return _legacy_column(result.get("column") or record)

    def require_column(self, column_id: str) -> dict[str, Any]:
        """Return one column or raise a compatibility not-found error."""

        found = self.owner.find_column(str(column_id))
        if not isinstance(found, Mapping):
            raise KanbanNotFoundError("column not found: " + str(column_id))
        column = _legacy_column(found.get("column") or {})
        column["board_id"] = str(found.get("board_id") or "")
        return column

    def update_column(self, column_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update one column through the canonical owner."""

        current = self.require_column(column_id)
        merged = {**current, **dict(updates or {}), "id": str(column_id)}
        if not str(merged.get("title") or "").strip():
            raise KanbanValidationError("title is required")
        result = self._apply(
            "column.upsert",
            {"board_id": current["board_id"], "record": merged},
        )
        self._append_event(
            str(current["board_id"]),
            "column.updated",
            {"column_id": str(column_id), "updates": dict(updates or {})},
        )
        return _legacy_column(result.get("column") or merged)

    def delete_column(self, column_id: str) -> dict[str, Any]:
        """Delete one empty column through the canonical owner."""

        current = self.require_column(column_id)
        board = self._raw_board(str(current["board_id"]))
        if any(
            card.get("column_id") == str(column_id)
            for card in board.get("cards", {}).values()
            if isinstance(card, Mapping)
        ):
            raise KanbanValidationError("column contains cards")
        self._apply(
            "column.delete",
            {"board_id": current["board_id"], "record_id": str(column_id), "record": current},
        )
        self._append_event(
            str(current["board_id"]),
            "column.deleted",
            {"column_id": str(column_id)},
        )
        return current

    def create_card(self, board_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one card in the canonical owner."""

        board = self._raw_board(board_id)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise KanbanValidationError("title is required")
        column_id = self._resolve_column_id(board, payload)
        card_id = str(payload.get("card_id") or payload.get("id") or gen_id("kcard_"))
        record = _card_record(payload, card_id=card_id, column_id=column_id)
        result = self._apply("card.upsert", {"board_id": str(board_id), "record": record})
        card = _legacy_card(result.get("card") or record, str(board_id))
        self._append_event(
            str(board_id),
            "card.created",
            {"card_id": card["card_id"], "title": card["title"]},
            card_id=card["card_id"],
        )
        return self.require_card(card["card_id"])

    def require_card(self, card_id: str) -> dict[str, Any]:
        """Return one card or raise a compatibility not-found error."""

        found = self.owner.find_card(str(card_id))
        if not isinstance(found, Mapping):
            raise KanbanNotFoundError("card not found: " + str(card_id))
        return _legacy_card(found.get("card") or {}, str(found.get("board_id") or ""))

    def update_card(
        self,
        card_id: str,
        updates: dict[str, Any],
        *,
        event_type: str = "card.updated",
    ) -> dict[str, Any]:
        """Update one card and append a bounded audit event."""

        current = self.require_card(card_id)
        merged = {**current, **dict(updates or {})}
        if "notes" in updates and "description" not in updates:
            merged["description"] = updates["notes"]
        if not str(merged.get("title") or "").strip():
            raise KanbanValidationError("title is required")
        board_id = str(current["board_id"])
        board = self._raw_board(board_id)
        if merged.get("column_id"):
            column_id = self._resolve_column_id(board, merged)
        else:
            column_id = str(current["column_id"])
        record = _card_record(merged, card_id=str(card_id), column_id=column_id)
        result = self._apply("card.upsert", {"board_id": board_id, "record": record})
        card = self.require_card(str(card_id))
        self._append_event(
            board_id,
            event_type,
            {"card_id": str(card_id), "updates": dict(updates or {})},
            card_id=str(card_id),
        )
        del result
        return card

    def move_card(
        self,
        card_id: str,
        updates: dict[str, Any],
        *,
        event_type: str = "card.moved",
    ) -> dict[str, Any]:
        """Move one card to a canonical column."""

        current = self.require_card(card_id)
        board = self._raw_board(str(current["board_id"]))
        column_id = self._resolve_column_id(board, updates)
        target_column = next(
            item
            for item in board.get("columns", {}).values()
            if str(item.get("id") or "") == column_id
        )
        status = {
            "backlog": "backlog",
            "doing": "doing",
            "review": "review",
            "done": "done",
        }.get(str(target_column.get("title") or "").casefold(), current.get("status"))
        merged = {
            **current,
            **dict(updates or {}),
            "column_id": column_id,
            "status": status,
        }
        record = _card_record(merged, card_id=str(card_id), column_id=column_id)
        for key in ("before_card_id", "after_card_id"):
            value = str(updates.get(key) or "").strip()
            if value:
                record[key] = value
        self._apply(
            "card.move",
            {"board_id": str(current["board_id"]), "record": record},
        )
        card = self.require_card(str(card_id))
        self._append_event(
            str(current["board_id"]),
            event_type,
            {"card_id": str(card_id), "updates": dict(updates or {})},
            card_id=str(card_id),
        )
        return card

    def delete_card(self, card_id: str) -> dict[str, Any]:
        """Delete one card through the canonical owner."""

        current = self.require_card(card_id)
        board_id = str(current["board_id"])
        self._apply(
            "card.delete",
            {"board_id": board_id, "record_id": str(card_id), "record": current},
        )
        self._append_event(
            board_id,
            "card.deleted",
            {"card_id": str(card_id)},
        )
        return current

    def list_cards(self, board_id: str) -> list[dict[str, Any]]:
        """Return cards from one board in display order."""

        return list(self.board_snapshot(board_id).get("cards") or [])

    def add_event(
        self,
        board_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one compatibility audit event and return its projection."""

        self._append_event(str(board_id), event_type, payload or {})
        events = self.list_events(str(board_id))
        return events[-1] if events else {}

    def list_events(self, board_id: str) -> list[dict[str, Any]]:
        """Return the board's canonical events in append order."""

        board = self._raw_board(board_id)
        return [_legacy_event(item) for item in board.get("events", [])]

    def board_snapshot(self, board_id: str) -> dict[str, Any]:
        """Return the complete legacy-shaped board projection."""

        board = self._raw_board(board_id)
        return _legacy_board(board)

    def _raw_board(self, board_id: str) -> dict[str, Any]:
        value = self.owner.get(str(board_id))
        if not isinstance(value, Mapping):
            raise KanbanNotFoundError("board not found: " + str(board_id))
        return dict(value)

    def _resolve_column_id(self, board: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
        requested = str(payload.get("column_id") or payload.get("column") or "").strip()
        columns = list(board.get("columns", {}).values())
        if requested:
            for column in columns:
                if str(column.get("id") or "") == requested:
                    return requested
                if str(column.get("title") or "").casefold() == requested.casefold():
                    return str(column["id"])
            raise KanbanNotFoundError("column not found: " + requested)
        first = min(columns, key=lambda item: int(item.get("position") or 0), default=None)
        if not isinstance(first, Mapping):
            raise KanbanValidationError("board has no columns")
        return str(first.get("id") or "")

    def _apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply an optimistic canonical transition with bounded retry."""

        normalized_arguments = dict(arguments)
        if name.startswith(("column.", "card.")):
            record = normalized_arguments.get("record")
            if "record_id" not in normalized_arguments and isinstance(record, Mapping):
                normalized_arguments["record_id"] = str(record.get("id") or "")
        last_conflict: Exception | None = None
        for _ in range(8):
            revision = int(self.owner.snapshot().get("revision") or 0)
            try:
                return self.owner.apply(
                    name,
                    {"expected_revision": revision, **normalized_arguments},
                )
            except Exception as exc:
                if not _is_conflict(exc):
                    raise
                last_conflict = exc
        if last_conflict is not None:
            raise last_conflict
        raise RuntimeError("Kanban owner transition did not complete")
    def _append_event(
        self,
        board_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        card_id: str | None = None,
    ) -> None:
        record = {
            "id": gen_id("kevent_"),
            "type": str(event_type),
            "card_id": str(card_id or ""),
            "actor_type": "compatibility_adapter",
            "actor_id": "defaultspack.kanban",
            "payload": dict(payload),
            "created_at_ms": now_ms(),
        }
        self._apply("event.append", {"board_id": str(board_id), "record": record})


def _is_conflict(error: Exception) -> bool:
    """Identify the canonical conflict without importing its owner pack."""

    return any(cls.__name__ == "KanbanConflict" for cls in type(error).__mro__)


def _create_owner(
    factory: StateStoreFactory,
    profile_id: str,
    root: Path | None,
) -> Any:
    """Call injected factories with compatibility support for simple callbacks."""

    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return factory(profile_id, root=root)
    accepts_root = any(
        parameter.name == "root"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if root is None and not accepts_root:
        return factory(profile_id)  # type: ignore[call-arg]
    return factory(profile_id, root=root)


def _card_record(
    value: Mapping[str, Any],
    *,
    card_id: str,
    column_id: str,
) -> dict[str, Any]:
    """Convert a legacy card payload to the canonical card record."""

    description = value.get("description")
    if description is None:
        description = value.get("notes")
    return {
        "id": card_id,
        "column_id": column_id,
        "position": max(0, int(value.get("position") or 0)),
        "title": str(value.get("title") or "").strip(),
        "description": str(description or ""),
        "status": _status_for(value.get("status"), value.get("column_title")),
        "priority": str(value.get("priority") or "normal"),
        "assignee": str(value.get("assignee") or ""),
        "due_at": str(value.get("due_at") or ""),
        "source_type": str(value.get("source_type") or "manual"),
        "source_id": str(value.get("source_id") or ""),
        "conversation_id": str(value.get("conversation_id") or ""),
        "workspace_id": str(value.get("workspace_id") or ""),
        "company_id": str(value.get("company_id") or ""),
        "agent_run_id": str(value.get("agent_run_id") or ""),
        "agent_session_id": str(value.get("agent_session_id") or ""),
        "agent_status": str(value.get("agent_status") or ""),
        "branch": str(value.get("branch") or ""),
        "pr_url": str(value.get("pr_url") or ""),
        "labels": string_list(value.get("labels")),
        "checklist": list(value.get("checklist") or [])
        if isinstance(value.get("checklist"), list)
        else [],
        "depends_on": string_list(value.get("depends_on")),
        "blocked_by": string_list(value.get("blocked_by")),
        "metadata": dict(value.get("metadata") or {})
        if isinstance(value.get("metadata"), Mapping)
        else {},
        "archived_at_ms": value.get("archived_at_ms") or value.get("archived_at"),
    }


def _status_for(status: Any, column_title: Any) -> str:
    """Normalize a legacy status without inventing a second state model."""

    value = str(status or "").strip().casefold()
    if value in {"backlog", "doing", "review", "done", "blocked"}:
        return value
    title = str(column_title or "").strip().casefold()
    return {
        "backlog": "backlog",
        "doing": "doing",
        "review": "review",
        "done": "done",
    }.get(title, "backlog")


def _legacy_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    scope = value.get("scope") if isinstance(value.get("scope"), Mapping) else {}
    return {
        **dict(value),
        "board_id": value.get("id"),
        "scope_type": scope.get("type"),
        "scope_id": scope.get("id"),
    }


def _legacy_board(value: Mapping[str, Any]) -> dict[str, Any]:
    columns = sorted(
        (_legacy_column(item) for item in value.get("columns", {}).values()),
        key=lambda item: (int(item.get("position") or 0), str(item.get("id") or "")),
    )
    column_positions = {str(item.get("id")): int(item.get("position") or 0) for item in columns}
    cards = sorted(
        (
            _legacy_card(item, str(value.get("id") or ""))
            for item in value.get("cards", {}).values()
        ),
        key=lambda item: (
            column_positions.get(str(item.get("column_id") or ""), 0),
            int(item.get("position") or 0),
            int(item.get("created_at_ms") or 0),
        ),
    )
    return {
        "board": _legacy_summary(value),
        "columns": columns,
        "cards": cards,
        "events": [_legacy_event(item) for item in value.get("events", [])],
    }


def _legacy_column(value: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(value), "column_id": value.get("id")}


def _legacy_card(value: Mapping[str, Any], board_id: str) -> dict[str, Any]:
    return {**dict(value), "card_id": value.get("id"), "board_id": board_id}


def _legacy_event(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(value),
        "event_id": value.get("id"),
        "event_type": value.get("type"),
    }
