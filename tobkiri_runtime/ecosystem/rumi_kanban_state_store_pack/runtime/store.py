"""Profile-scoped Kanban board state behind global contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
SERVICE_PACK_ID = "rumi_kanban_state_store_pack"
VERSION = "rumi.kanban-state.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DEFAULT_COLUMNS = ("Backlog", "Doing", "Review", "Done")


class KanbanConflict(RuntimeError):
    """Raised when a Kanban revision or migration boundary conflicts."""


class KanbanStateStore:
    """Own canonical boards, columns, cards, and audit events."""

    def __init__(self, profile_id: str, *, root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(root or USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "boards.json"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return redacted board summaries in deterministic order."""

        state = self._read()
        boards = [
            _board_summary(state["boards"][board_id])
            for board_id in sorted(state["boards"])
        ]
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "boards": boards,
        }

    def get(self, board_id: str) -> dict[str, Any] | None:
        """Return one complete board snapshot by exact identifier."""

        value = self._read()["boards"].get(_identifier(board_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def find_card(self, card_id: str) -> dict[str, Any] | None:
        """Return one card and its board without exposing another store."""

        target = _identifier(card_id)
        for board_id, board in self._read()["boards"].items():
            cards = board.get("cards") if isinstance(board, Mapping) else None
            card = cards.get(target) if isinstance(cards, Mapping) else None
            if isinstance(card, Mapping):
                return {"board_id": board_id, "card": _copy(card)}
        return None

    def find_column(self, column_id: str) -> dict[str, Any] | None:
        """Return one column and its board without exposing another store."""

        target = _identifier(column_id)
        for board_id, board in self._read()["boards"].items():
            columns = board.get("columns") if isinstance(board, Mapping) else None
            column = columns.get(target) if isinstance(columns, Mapping) else None
            if isinstance(column, Mapping):
                return {"board_id": board_id, "column": _copy(column)}
        return None

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one receipt-bound, revision-checked Kanban state transition."""

        with NamedLock(self.lock_root, "kanban"):
            state = self._read()
            _assert_revision(state, int(arguments["expected_revision"]))
            result = self._transition(state, name, arguments)
            if result.get("deduplicated"):
                return {**result, "revision": state["revision"]}
            state["revision"] += 1
            self._write(state)
            return {**result, "revision": state["revision"]}

    def _transition(
        self,
        state: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if name == "migration.import_snapshot":
            return self._import_snapshot(state, arguments)
        board_id = _identifier(arguments["board_id"])
        now_ms = _now_ms()
        if name == "board.create":
            if board_id in state["boards"]:
                raise KanbanConflict("Kanban board already exists")
            board = _new_board(arguments, now_ms)
            state["boards"][board_id] = board
            return {"board": _copy(board)}
        board = state["boards"].get(board_id)
        if not isinstance(board, dict):
            raise KeyError("Kanban board is unknown")
        if name == "board.update":
            board["title"] = (
                _text(arguments["updates"].get("title"), 200) or board["title"]
            )
            board["metadata"] = {
                **board["metadata"],
                **arguments["updates"].get("metadata", {}),
            }
            board["updated_at_ms"] = now_ms
            return {"board": _copy(board)}
        if name == "board.delete":
            if any(
                card["status"] in {"doing", "review"}
                for card in board["cards"].values()
            ):
                raise KanbanConflict("active Kanban cards must be resolved first")
            del state["boards"][board_id]
            return {"deleted_board_id": board_id}
        if name.startswith("column."):
            return _column_transition(board, name, arguments, now_ms)
        if name.startswith("card."):
            return _card_transition(board, name, arguments, now_ms)
        if name == "event.append":
            event = _event(arguments["record"], board, now_ms)
            if any(item["id"] == event["id"] for item in board["events"]):
                return {"event": _copy(event), "deduplicated": True}
            board["events"].append(event)
            del board["events"][:-10_000]
            board["updated_at_ms"] = now_ms
            return {"event": _copy(event), "deduplicated": False}
        raise ValueError(f"unknown Kanban action: {name}")

    def _import_snapshot(
        self,
        state: dict[str, Any],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Import one caller-supplied legacy snapshot without live fallback."""

        board = _legacy_board(arguments["snapshot"])
        source_hash = _canonical_hash(board)
        migration_id = "legacy-" + board["id"]
        previous = state["migrations"].get(migration_id)
        if isinstance(previous, Mapping):
            if previous.get("source_hash") != source_hash:
                raise KanbanConflict("Kanban migration source differs")
            existing = state["boards"].get(board["id"])
            if not isinstance(existing, Mapping):
                raise KanbanConflict("Kanban migration is incomplete")
            return {"board": _copy(existing), "deduplicated": True}
        if board["id"] in state["boards"]:
            raise KanbanConflict("Kanban board exists before migration")
        state["boards"][board["id"]] = board
        state["migrations"][migration_id] = {
            "id": migration_id,
            "board_id": board["id"],
            "source_hash": source_hash,
            "imported_at_ms": _now_ms(),
        }
        return {"board": _copy(board), "deduplicated": False}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "boards": {},
                "migrations": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != VERSION
            or value.get("profile_id") != self.profile_id
            or not isinstance(value.get("boards"), Mapping)
            or not isinstance(value.get("migrations", {}), Mapping)
        ):
            raise ValueError("Kanban state is invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "boards": _copy(value["boards"]),
            "migrations": _copy(value.get("migrations", {})),
        }

    def _write(self, state: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        _atomic_json(self.path, state)


def create_kanban_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create Kanban resource operations."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = KanbanStateStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("board_id") or ""))
        if name == "find_card":
            return store.find_card(str(payload.get("card_id") or ""))
        if name == "find_column":
            return store.find_column(str(payload.get("column_id") or ""))
        raise ValueError(f"unknown Kanban resource operation: {name}")

    return operation


def create_kanban_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated Kanban mutations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        _redeem(client, payload, name, arguments)
        return KanbanStateStore(_profile(payload)).apply(name, arguments)

    return operation


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "board.create",
        "board.update",
        "board.delete",
        "column.upsert",
        "column.delete",
        "card.upsert",
        "card.move",
        "card.delete",
        "event.append",
        "migration.import_snapshot",
    }
    if name not in allowed:
        raise ValueError(f"unknown Kanban action: {name}")
    arguments: dict[str, Any] = {
        "expected_revision": max(0, int(payload.get("expected_revision") or 0)),
        "board_id": str(payload.get("board_id") or ""),
    }
    if name == "board.create":
        arguments.update(
            {
                "title": _text(payload.get("title"), 200),
                "scope": _mapping(payload.get("scope")),
                "metadata": _mapping(payload.get("metadata")),
                "columns": payload.get("columns"),
            }
        )
    elif name == "board.update":
        arguments["updates"] = _mapping(payload.get("updates"))
    elif name.startswith("column.") or name.startswith("card."):
        arguments["record"] = _mapping(payload.get("record"))
        arguments["record_id"] = str(
            payload.get("record_id") or arguments["record"].get("id") or ""
        )
    elif name == "event.append":
        arguments["record"] = _mapping(payload.get("record"))
    elif name == "migration.import_snapshot":
        arguments["snapshot"] = _migration_snapshot(payload.get("snapshot"))
    return arguments


def _redeem(
    client: Any,
    payload: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": SERVICE_PACK_ID,
            "operation": f"kanban.state.{name}",
            "authority": "kanban.state.manage",
            "caller_id": str(payload.get("caller_id") or ""),
            "caller_pack_id": str(payload.get("caller_pack_id") or ""),
            "caller_function_id": str(payload.get("caller_function_id") or ""),
            "profile_id": _profile(payload),
            "workspace_id": "",
            "session_id": str(payload.get("session_id") or ""),
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "Kanban state denied"))


def _new_board(arguments: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    board_id = _identifier(arguments["board_id"])
    columns = arguments.get("columns")
    labels = columns if isinstance(columns, list) else list(_DEFAULT_COLUMNS)
    records = [
        _column(
            {
                "id": f"{board_id}.column.{index}",
                "title": label,
                "position": index,
                "done": str(label).strip().casefold()
                in {"done", "complete", "completed", "closed"},
            },
            now_ms,
        )
        for index, label in enumerate(labels[:32])
    ]
    return {
        "id": board_id,
        "title": _text(arguments["title"], 200) or board_id,
        "scope": _scope(arguments["scope"]),
        "metadata": _copy(arguments["metadata"]),
        "columns": {item["id"]: item for item in records},
        "cards": {},
        "events": [],
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
    }


def _column_transition(
    board: dict[str, Any],
    name: str,
    arguments: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    record_id = _identifier(arguments["record_id"])
    if name == "column.delete":
        if any(card["column_id"] == record_id for card in board["cards"].values()):
            raise KanbanConflict("Kanban column contains cards")
        if board["columns"].pop(record_id, None) is None:
            raise KeyError("Kanban column is unknown")
        board["updated_at_ms"] = now_ms
        return {"deleted_column_id": record_id}
    record = _column({**arguments["record"], "id": record_id}, now_ms)
    board["columns"][record_id] = record
    board["updated_at_ms"] = now_ms
    return {"column": _copy(record)}


def _card_transition(
    board: dict[str, Any],
    name: str,
    arguments: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    record_id = _identifier(arguments["record_id"])
    if name == "card.delete":
        if board["cards"].pop(record_id, None) is None:
            raise KeyError("Kanban card is unknown")
        board["updated_at_ms"] = now_ms
        return {"deleted_card_id": record_id}
    current = board["cards"].get(record_id, {})
    incoming = {**current, **arguments["record"], "id": record_id}
    card = _card(incoming, board, now_ms)
    board["cards"][record_id] = card
    board["updated_at_ms"] = now_ms
    return {"card": _copy(card)}


def _column(value: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    return {
        "id": _identifier(value.get("id") or uuid.uuid4().hex),
        "title": _text(value.get("title"), 200) or "Column",
        "position": max(0, min(10_000, int(value.get("position") or 0))),
        "done": bool(value.get("done", False)),
        "wip_limit": _optional_limit(value.get("wip_limit")),
        "created_at_ms": _time(
            value.get("created_at_ms") or value.get("created_at"),
            now_ms,
        ),
        "updated_at_ms": now_ms,
    }


def _card(
    value: Mapping[str, Any],
    board: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    column_id = _identifier(value.get("column_id") or "")
    if column_id not in board["columns"]:
        raise KeyError("Kanban card column is unknown")
    status = _text(value.get("status"), 40).casefold() or "backlog"
    if status not in {"backlog", "doing", "review", "done", "blocked"}:
        raise ValueError("Kanban card status is invalid")
    return {
        "id": _identifier(value.get("id") or uuid.uuid4().hex),
        "column_id": column_id,
        "position": max(0, min(100_000, int(value.get("position") or 0))),
        "title": _text(value.get("title"), 500) or "Card",
        "description": _text(value.get("description"), 100_000),
        "status": status,
        "priority": _text(value.get("priority"), 32) or "normal",
        "assignee": _text(value.get("assignee"), 255),
        "due_at": _text(value.get("due_at"), 100),
        "source_type": _text(value.get("source_type"), 100) or "manual",
        "source_id": _text(value.get("source_id"), 255),
        "conversation_id": _text(value.get("conversation_id"), 255),
        "workspace_id": _text(value.get("workspace_id"), 255),
        "company_id": _text(value.get("company_id"), 255),
        "agent_run_id": _text(value.get("agent_run_id"), 255),
        "agent_session_id": _text(value.get("agent_session_id"), 255),
        "agent_status": _text(value.get("agent_status"), 100),
        "branch": _text(value.get("branch"), 1_024),
        "pr_url": _text(value.get("pr_url"), 2_048),
        "labels": _strings(value.get("labels"), 100, 100),
        "checklist": _records(value.get("checklist"), 100),
        "depends_on": _strings(value.get("depends_on"), 100, 255),
        "blocked_by": _strings(value.get("blocked_by"), 100, 255),
        "metadata": _copy(_mapping(value.get("metadata"))),
        "created_at_ms": _time(
            value.get("created_at_ms") or value.get("created_at"),
            now_ms,
        ),
        "updated_at_ms": now_ms,
        "archived_at_ms": _optional_time(value.get("archived_at_ms")),
    }


def _event(
    value: Mapping[str, Any],
    board: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    card_id = _text(value.get("card_id"), 255)
    if card_id and card_id not in board["cards"]:
        raise KeyError("Kanban event card is unknown")
    return {
        "id": _identifier(value.get("id") or uuid.uuid4().hex),
        "type": _text(value.get("type"), 120) or "event",
        "card_id": card_id,
        "actor_type": _text(value.get("actor_type"), 100) or "user",
        "actor_id": _text(value.get("actor_id"), 255),
        "payload": _copy(_mapping(value.get("payload"))),
        "created_at_ms": _time(
            value.get("created_at_ms") or value.get("created_at"),
            now_ms,
        ),
    }


def _legacy_board(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("legacy Kanban snapshot is required")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("legacy Kanban snapshot is too large")
    now_ms = _now_ms()
    board_id = _identifier(value.get("id") or value.get("board_id") or "")
    columns_raw = value.get("columns") if isinstance(value.get("columns"), list) else []
    columns: dict[str, dict[str, Any]] = {}
    for item in columns_raw:
        if isinstance(item, Mapping):
            column = _column(
                {**item, "id": item.get("id") or item.get("column_id")},
                now_ms,
            )
            columns[column["id"]] = column
    if not columns:
        default_board = _new_board(
            {
                "board_id": board_id,
                "title": value.get("title"),
                "scope": {},
                "metadata": {},
                "columns": None,
            },
            now_ms,
        )
        columns = default_board["columns"]
    board = {
        "id": board_id,
        "title": _text(value.get("title"), 200) or board_id,
        "scope": _legacy_scope(value),
        "metadata": _copy(_mapping(value.get("metadata"))),
        "columns": columns,
        "cards": {},
        "events": [],
        "created_at_ms": _time(
            value.get("created_at_ms") or value.get("created_at"),
            now_ms,
        ),
        "updated_at_ms": _time(
            value.get("updated_at_ms") or value.get("updated_at"),
            now_ms,
        ),
    }
    cards_raw = value.get("cards") if isinstance(value.get("cards"), list) else []
    for item in cards_raw[:10_000]:
        if isinstance(item, Mapping):
            card = _card(
                {
                    **item,
                    "id": item.get("id") or item.get("card_id"),
                    "archived_at_ms": item.get("archived_at_ms")
                    or item.get("archived_at"),
                },
                board,
                now_ms,
            )
            board["cards"][card["id"]] = card
    for item in value.get("events") if isinstance(value.get("events"), list) else []:
        if isinstance(item, Mapping) and len(board["events"]) < 10_000:
            board["events"].append(
                _event(
                    {
                        **item,
                        "id": item.get("id") or item.get("event_id"),
                        "type": item.get("type") or item.get("event_type"),
                    },
                    board,
                    now_ms,
                )
            )
    return board


def _migration_snapshot(value: Any) -> Mapping[str, Any]:
    """Bound one migration source before its exact receipt is redeemed."""

    snapshot = _mapping(value)
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("legacy Kanban snapshot is too large")
    return snapshot


def _board_summary(board: Mapping[str, Any]) -> dict[str, Any]:
    cards = board.get("cards") if isinstance(board.get("cards"), Mapping) else {}
    return {
        "id": board["id"],
        "title": board["title"],
        "scope": _copy(board["scope"]),
        "column_count": len(board.get("columns") or {}),
        "card_count": len(cards),
        "updated_at_ms": board["updated_at_ms"],
    }


def _scope(value: Any) -> dict[str, str]:
    raw = _mapping(value)
    return {
        "type": _text(raw.get("type"), 100) or "profile",
        "id": _text(raw.get("id"), 255) or "default",
    }


def _legacy_scope(value: Mapping[str, Any]) -> dict[str, str]:
    scope = value.get("scope")
    if isinstance(scope, Mapping):
        return _scope(scope)
    return _scope(
        {
            "type": value.get("scope_type"),
            "id": value.get("scope_id"),
        }
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _strings(value: Any, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({_text(item, limit) for item in value if _text(item, limit)})[:count]


def _records(value: Any, count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_copy(item) for item in value[:count] if isinstance(item, Mapping)]


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _identifier(value: Any) -> str:
    identifier = _text(value, 255)
    if not _ID.fullmatch(identifier):
        raise ValueError("Kanban identifier is invalid")
    return identifier


def _optional_limit(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return max(1, min(10_000, int(value)))


def _optional_time(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return max(0, int(value))


def _time(value: Any, fallback: int) -> int:
    return max(0, int(value or fallback))


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise KanbanConflict("Kanban state revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".kanban-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
