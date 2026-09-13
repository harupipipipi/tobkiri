"""Finite Kanban conversation and agent adapter over the canonical owner."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import KanbanNotFoundError, KanbanValidationError, gen_id
from .prompt_note import append_kanban_system_prompt_note
from .store import KanbanStore, StateStoreFactory

KANBAN_SYSTEM_PROMPT_NOTE = (
    "この会話はKanbanに追加されています。会話内のタスク、期限、担当、優先度が変わった場合は、"
    "対応するKanbanカードを更新対象として扱ってください。"
)

__all__ = [
    "KANBAN_SYSTEM_PROMPT_NOTE",
    "KanbanService",
    "append_kanban_system_prompt_note",
]

_TASK_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*(?:\[[ xX]\]\s*)?|"
    r"(?:todo|task|action|next|fix|bug)\s*[:：]|"
    r"(?:\d+|[a-zA-Z])[\.)]\s+)(.+)$",
    re.IGNORECASE,
)
_DIAGNOSTIC_TASK_PREFIX_RE = re.compile(
    r"^\s*(?:モデル|model|原因|reason|cause|内容|detail|details|message|"
    r"error|エラー|次に試すこと|next\s*(?:step|steps|to\s+try)|try\s+next)\s*[:：]",
    re.IGNORECASE,
)
_DIAGNOSTIC_TASK_CONTENT_RE = re.compile(
    r"(?:ai provider http \d+|invalid_api_key|incorrect api key|"
    r"platform\.openai\.com/account/api-keys|model invocation:|provider api key use:|モデル/api)",
    re.IGNORECASE,
)


class KanbanService:
    """Project conversation imports and agent transitions onto Kanban state."""

    def __init__(
        self,
        store: KanbanStore | None = None,
        *,
        db_path: str | Path | None = None,
        state_store_factory: StateStoreFactory | None = None,
    ) -> None:
        self.store = store if store is not None else KanbanStore(
            db_path,
            state_store_factory=state_store_factory,
        )

    def list_boards(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """List boards, optionally bootstrapping the requested scope."""

        values = payload or {}
        scope_type, scope_id = _scope_from_payload(values, required=False)
        if _truthy(values.get("bootstrap")):
            if not scope_type or not scope_id:
                raise KanbanValidationError("scope_type and scope_id are required")
            return self.bootstrap_board({"scope_type": scope_type, "scope_id": scope_id, **values})
        return {"boards": self.store.list_boards(scope_type=scope_type, scope_id=scope_id)}

    def bootstrap_board(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or return a scoped board snapshot."""

        scope_type, scope_id = _scope_from_payload(payload, required=True)
        board = self.store.get_or_create_board(
            str(scope_type),
            str(scope_id),
            title=_optional_text(payload.get("title")),
        )
        self.store.ensure_default_columns(board["board_id"])
        return self.get_board(board["board_id"])

    def get_board(self, board_id: str) -> dict[str, Any]:
        """Return a complete board snapshot."""

        if not board_id:
            raise KanbanValidationError("board_id is required")
        return self.store.board_snapshot(str(board_id))

    def update_board(self, board_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update one board and return its new snapshot."""

        if not board_id:
            raise KanbanValidationError("board_id is required")
        self.store.update_board(str(board_id), _updates_from_payload(payload))
        return self.get_board(str(board_id))

    def create_card(self, board_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one card in a board."""

        if not board_id:
            raise KanbanValidationError("board_id is required")
        return self.store.create_card(str(board_id), _without_control_keys(payload))

    def update_card(self, card_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update one card."""

        if not card_id:
            raise KanbanValidationError("card_id is required")
        return self.store.update_card(str(card_id), _updates_from_payload(payload))

    def delete_card(self, card_id: str) -> dict[str, Any]:
        """Delete one card and return its former projection."""

        if not card_id:
            raise KanbanValidationError("card_id is required")
        card = self.store.delete_card(str(card_id))
        return {"deleted": True, "card_id": card["card_id"], "card": card}

    def move_card(self, card_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Move one card and return the containing board snapshot."""

        if not card_id:
            raise KanbanValidationError("card_id is required")
        card = self.store.move_card(str(card_id), _without_control_keys(payload))
        return self.get_board(card["board_id"])

    def create_column(self, board_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one column."""

        if not board_id:
            raise KanbanValidationError("board_id is required")
        return self.store.create_column(
            str(board_id),
            str(payload.get("title") or ""),
            position=_optional_int(payload.get("position")),
            done=_optional_bool(payload.get("done")),
        )

    def update_column(self, column_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update one column."""

        if not column_id:
            raise KanbanValidationError("column_id is required")
        return self.store.update_column(str(column_id), _updates_from_payload(payload))

    def delete_column(self, column_id: str) -> dict[str, Any]:
        """Delete one column."""

        if not column_id:
            raise KanbanValidationError("column_id is required")
        column = self.store.delete_column(str(column_id))
        return {"deleted": True, "column_id": column["column_id"], "column": column}

    def sync_runs(
        self,
        board_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record that run synchronization was intentionally a no-op."""

        if not board_id:
            raise KanbanValidationError("board_id is required")
        values = payload or {}
        self.store.add_event(
            str(board_id),
            "runs.sync.noop",
            {"source": values.get("source") or "kanban_api"},
        )
        return self.get_board(str(board_id))

    def import_conversation(self, board_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Project conversation tasks into idempotent Kanban cards."""

        if not board_id:
            raise KanbanValidationError("board_id is required")
        board = self.store.require_board(str(board_id))
        conversation_id = str(
            payload.get("conversation_id") or payload.get("source_id") or ""
        ).strip()
        if not conversation_id:
            raise KanbanValidationError("conversation_id is required")

        from domain.chat.store import ChatStore

        chat_store = ChatStore()
        conversation = chat_store.get_conversation(conversation_id)
        if conversation is None:
            raise KanbanNotFoundError("conversation not found: " + conversation_id)

        tasks, extraction = _conversation_tasks(conversation, payload)
        if not tasks:
            tasks = _fallback_conversation_tasks(conversation, payload)
            extraction = {"source": "fallback", "error": "empty task extraction"}

        existing_cards = [
            card
            for card in self.store.list_cards(board["board_id"])
            if str(card.get("conversation_id") or "") == conversation_id
            and str(card.get("source_type") or "") == "conversation"
        ]
        existing_cards.sort(
            key=lambda card: (
                int(
                    ((card.get("metadata") or {}).get("conversation_import") or {}).get(
                        "task_index"
                    )
                    or 9999
                ),
                int(card.get("created_at_ms") or card.get("created_at") or 0),
            )
        )

        saved_cards: list[dict[str, Any]] = []
        for index, task in enumerate(tasks[:8]):
            card_payload = _task_card_payload(
                board,
                conversation,
                task,
                index=index,
                extraction=extraction,
                request_payload=payload,
            )
            if index < len(existing_cards):
                saved_cards.append(
                    self.store.update_card(
                        existing_cards[index]["card_id"],
                        card_payload,
                        event_type="conversation.import.updated",
                    )
                )
            else:
                saved_cards.append(self.store.create_card(board["board_id"], card_payload))
        for stale_card in existing_cards[len(saved_cards) :]:
            self.store.delete_card(stale_card["card_id"])

        self.store.add_event(
            board["board_id"],
            "conversation.imported",
            {
                "conversation_id": conversation_id,
                "card_ids": [card["card_id"] for card in saved_cards],
                "task_count": len(saved_cards),
                "extraction": extraction,
            },
        )
        updated_conversation = _mark_conversation_in_kanban(
            chat_store,
            conversation,
            board=board,
            cards=saved_cards,
            extraction=extraction,
        )
        snapshot = self.get_board(board["board_id"])
        snapshot["imported"] = {
            "conversation_id": conversation_id,
            "card_ids": [card["card_id"] for card in saved_cards],
            "conversation": {
                "id": updated_conversation.get("id", conversation_id),
                "title": updated_conversation.get("title", conversation.get("title")),
                "metadata": updated_conversation.get(
                    "metadata", conversation.get("metadata")
                ),
            },
            "extraction": extraction,
        }
        return snapshot

    def agent_status(self, card_id: str) -> dict[str, Any]:
        """Return the current agent card projection."""

        if not card_id:
            raise KanbanValidationError("card_id is required")
        return self.store.require_card(str(card_id))

    def agent_start(self, card_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Mark a card running and move it to Doing."""

        card = self.store.require_card(str(card_id))
        updates = self._agent_updates(card, payload, "running", "started")
        updates["agent_run_id"] = updates.get("agent_run_id") or gen_id("krun_")
        updates["agent_session_id"] = updates.get("agent_session_id") or gen_id("ksess_")
        self.store.update_card(str(card_id), updates, event_type="agent.started")
        return self._move_card_to_column_title(str(card_id), "Doing", "agent.moved_to_doing")

    def agent_ready(
        self,
        card_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark a card ready and move it to Review."""

        card = self.store.require_card(str(card_id))
        self.store.update_card(
            str(card_id),
            self._agent_updates(card, payload or {}, "ready", "ready"),
            event_type="agent.ready",
        )
        return self._move_card_to_column_title(str(card_id), "Review", "agent.moved_to_review")

    def agent_apply(
        self,
        card_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark a card applied and move it to Done."""

        card = self.store.require_card(str(card_id))
        self.store.update_card(
            str(card_id),
            self._agent_updates(card, payload or {}, "applied", "applied"),
            event_type="agent.applied",
        )
        return self._move_card_to_column_title(str(card_id), "Done", "agent.moved_to_done")

    def agent_dismiss(
        self,
        card_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mark a card dismissed and move it back to Review."""

        card = self.store.require_card(str(card_id))
        self.store.update_card(
            str(card_id),
            self._agent_updates(card, payload or {}, "dismissed", "dismissed"),
            event_type="agent.dismissed",
        )
        return self._move_card_to_column_title(str(card_id), "Review", "agent.dismissed_to_review")

    def _move_card_to_column_title(
        self,
        card_id: str,
        title: str,
        event_type: str,
    ) -> dict[str, Any]:
        card = self.store.require_card(str(card_id))
        target = next(
            (
                column
                for column in self.store.list_columns(card["board_id"])
                if str(column.get("title") or "").casefold() == title.casefold()
            ),
            None,
        )
        if target is None:
            return card
        return self.store.move_card(
            str(card_id),
            {"column_id": target["column_id"]},
            event_type=event_type,
        )

    def _agent_updates(
        self,
        card: dict[str, Any],
        payload: dict[str, Any],
        status: str,
        action: str,
    ) -> dict[str, Any]:
        metadata = dict(card.get("metadata") or {})
        agent_meta = dict(metadata.get("agent") or {})
        agent_meta.update(
            {"last_action": action, "last_action_payload": _public_payload(payload)}
        )
        metadata["agent"] = agent_meta
        return {
            "agent_status": status,
            "agent_run_id": payload.get("agent_run_id")
            or payload.get("run_id")
            or card.get("agent_run_id"),
            "agent_session_id": payload.get("agent_session_id")
            or payload.get("session_id")
            or card.get("agent_session_id"),
            "branch": payload.get("branch") or card.get("branch"),
            "pr_url": payload.get("pr_url") or card.get("pr_url"),
            "conversation_id": payload.get("conversation_id")
            or card.get("conversation_id"),
            "workspace_id": payload.get("workspace_id") or card.get("workspace_id"),
            "company_id": payload.get("company_id") or card.get("company_id"),
            "metadata": metadata,
        }


def _scope_from_payload(
    payload: dict[str, Any],
    *,
    required: bool,
) -> tuple[str | None, str | None]:
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    scope_type = payload.get("scope_type") or payload.get("type") or scope.get(
        "scope_type"
    ) or scope.get("type")
    scope_id = payload.get("scope_id") or payload.get("id") or scope.get(
        "scope_id"
    ) or scope.get("id")
    normalized_type = str(scope_type).strip().casefold() if scope_type is not None else None
    normalized_id = str(scope_id).strip() if scope_id is not None else None
    if required and (not normalized_type or not normalized_id):
        raise KanbanValidationError("scope_type and scope_id are required")
    return normalized_type or None, normalized_id or None


def _updates_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    updates = payload.get("updates")
    return _without_control_keys(updates if isinstance(updates, dict) else payload)


def _without_control_keys(payload: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "action",
        "board_id",
        "card_id",
        "column_id_path",
        "column_id_param",
        "_headers",
        "_handler",
        "_method",
        "_actual_method",
        "_raw_body",
        "_raw_body_base64",
    }
    return {
        str(key): value
        for key, value in (payload or {}).items()
        if not str(key).startswith("_") and str(key) not in blocked
    }


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in _without_control_keys(payload).items()
        if key not in {"metadata", "checklist"}
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _conversation_tasks(
    conversation: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        normalized = [_normalize_task(item) for item in tasks]
        return [item for item in normalized if item], {"source": "provided"}
    if str(payload.get("use_ai", "true")).strip().casefold() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return _fallback_conversation_tasks(conversation, payload), {
            "source": "fallback",
            "reason": "ai_disabled",
        }
    model = str(payload.get("model") or payload.get("model_id") or "").strip()
    if not model:
        return _fallback_conversation_tasks(conversation, payload), {
            "source": "fallback",
            "reason": "model_missing",
        }
    authority_context = payload.get("_authority_context")
    if not isinstance(authority_context, dict) or not authority_context:
        return _fallback_conversation_tasks(conversation, payload), {
            "source": "fallback",
            "model": model,
            "reason": "authority_context_missing",
        }
    try:
        timeout = _ai_extract_timeout_seconds(payload)
        from domain.ai_client.client import AIClient

        response = AIClient().complete(
            model,
            [
                {
                    "role": "system",
                    "content": (
                        "Extract a concise Kanban task list from the conversation. "
                        "Return only JSON with key tasks."
                    ),
                },
                {"role": "user", "content": _conversation_excerpt(conversation, limit=9000)},
            ],
            [],
            {
                "temperature": 0,
                "max_tokens": 900,
                "request_timeout": timeout,
                "timeout": timeout,
                "_authority_context": authority_context,
            },
        )
        parsed = _parse_task_json(_response_text(response))
        if parsed:
            return parsed, {"source": "ai", "model": model}
        return _fallback_conversation_tasks(conversation, payload), {
            "source": "fallback",
            "model": model,
            "reason": "ai_json_empty",
        }
    except Exception as exc:
        return _fallback_conversation_tasks(conversation, payload), {
            "source": "fallback",
            "model": model,
            "error": str(exc),
        }


def _ai_extract_timeout_seconds(payload: dict[str, Any]) -> float:
    raw = payload.get("ai_timeout_seconds") or "8"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 8.0
    return max(0.05, min(20.0, value))


def _fallback_conversation_tasks(
    conversation: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    title = str(payload.get("title") or conversation.get("title") or "Conversation task").strip()
    lines = _task_like_lines(conversation)
    if lines:
        return [
            {
                "title": _compact_text(line, 96),
                "description": "Imported from conversation: " + title,
                "priority": "normal",
                "labels": ["conversation"],
                "checklist": [],
            }
            for line in lines[:6]
        ]
    description = _conversation_card_description(conversation, limit=800)
    return [
        {
            "title": _compact_text(
                title if title != "New Conversation" else "Review conversation tasks",
                96,
            ),
            "description": description or "Imported from conversation",
            "priority": "normal",
            "labels": ["conversation"],
            "checklist": _fallback_checklist(conversation),
        }
    ]


def _task_card_payload(
    board: dict[str, Any],
    conversation: dict[str, Any],
    task: dict[str, Any],
    *,
    index: int,
    extraction: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    conversation_metadata = conversation.get("metadata") or {}
    group_id = conversation.get("group_id") or conversation_metadata.get("group_id")
    metadata = {
        "conversation_import": {
            "task_index": index,
            "conversation_title": conversation.get("title"),
            "conversation_group_id": group_id,
            "board_scope_type": board.get("scope_type"),
            "board_scope_id": board.get("scope_id"),
            "extraction": extraction,
        },
        "conversation_title": conversation.get("title"),
        "conversation_group_id": group_id,
    }
    column = str(request_payload.get("column_id") or request_payload.get("column") or "").strip()
    return {
        "title": task.get("title") or conversation.get("title") or "Conversation task",
        "description": task.get("description"),
        "priority": task.get("priority") or "normal",
        "labels": task.get("labels") or ["conversation"],
        "checklist": task.get("checklist") or [],
        "source_type": "conversation",
        "source_id": f"{conversation.get('id')}:{index}",
        "conversation_id": conversation.get("id"),
        "workspace_id": request_payload.get("workspace_id") or conversation_metadata.get("workspace_id"),
        "company_id": request_payload.get("company_id") or conversation_metadata.get("company_id"),
        "column_id": column or None,
        "metadata": metadata,
    }


def _mark_conversation_in_kanban(
    chat_store: Any,
    conversation: dict[str, Any],
    *,
    board: dict[str, Any],
    cards: list[dict[str, Any]],
    extraction: dict[str, Any],
) -> dict[str, Any]:
    metadata = dict(conversation.get("metadata") or {})
    existing = metadata.get("kanban") if isinstance(metadata.get("kanban"), dict) else {}
    boards = [item for item in existing.get("boards", []) if isinstance(item, dict)]
    boards = [item for item in boards if item.get("board_id") != board.get("board_id")]
    boards.append(
        {
            "board_id": board.get("board_id"),
            "scope_type": board.get("scope_type"),
            "scope_id": board.get("scope_id"),
            "card_ids": [card.get("card_id") for card in cards],
        }
    )
    metadata["kanban"] = {
        **existing,
        "added": True,
        "board_id": board.get("board_id"),
        "card_ids": [card.get("card_id") for card in cards],
        "boards": boards[-12:],
        "last_extraction": extraction,
        "system_prompt_note": KANBAN_SYSTEM_PROMPT_NOTE,
    }
    return chat_store.update_conversation(
        str(conversation.get("id") or ""),
        {"metadata": metadata},
    ) or conversation


def _normalize_task(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        title = item.strip()
        return (
            {
                "title": title,
                "description": "",
                "priority": "normal",
                "labels": ["conversation"],
                "checklist": [],
            }
            if title
            else None
        )
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or item.get("name") or "").strip()
    if not title:
        return None
    labels = item.get("labels")
    return {
        "title": _compact_text(title, 120),
        "description": _compact_text(str(item.get("description") or item.get("notes") or ""), 1200),
        "priority": _priority(item.get("priority")),
        "labels": [str(label).strip() for label in labels if str(label).strip()]
        if isinstance(labels, list)
        else ["conversation"],
        "checklist": _normalize_checklist(item.get("checklist")),
    }


def _normalize_checklist(value: Any) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(value if isinstance(value, list) else []):
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("text") or "").strip()
            done = bool(item.get("done") or item.get("checked"))
        else:
            title = str(item).strip()
            done = False
        if title:
            result.append({"id": f"import-{index + 1}", "title": _compact_text(title, 140), "done": done})
    return result[:12]


def _priority(value: Any) -> str:
    normalized = str(value or "normal").strip().casefold()
    return normalized if normalized in {"urgent", "high", "normal", "low"} else "normal"


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    if isinstance(response.get("text"), str):
        return response["text"]
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    return str(response.get("message") or "")


def _parse_task_json(text: str) -> list[dict[str, Any]]:
    candidates = [str(text or "").strip()]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidates[0], re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    bracket = re.search(r"(\{.*\}|\[.*\])", candidates[0], re.DOTALL)
    if bracket:
        candidates.append(bracket.group(1).strip())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        raw_tasks = parsed.get("tasks") if isinstance(parsed, dict) else parsed
        if isinstance(raw_tasks, list):
            return [task for task in (_normalize_task(item) for item in raw_tasks) if task]
    return []


def _conversation_excerpt(conversation: dict[str, Any], *, limit: int) -> str:
    messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
    parts = ["Title: " + str(conversation.get("title") or "")]
    for message in messages[-30:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        raw = str(message.get("raw_text") or _content_text(message.get("content")) or "").strip()
        if raw:
            parts.append(f"{role}: {raw}")
    text = "\n".join(parts).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _task_like_lines(conversation: dict[str, Any]) -> list[str]:
    lines = []
    for raw_line in _conversation_excerpt(conversation, limit=9000).splitlines():
        line = re.sub(r"^\s*(?:user|assistant|system|tool)\s*:\s*", "", raw_line, flags=re.IGNORECASE)
        match = _TASK_LINE_RE.match(line)
        if match:
            task_line = match.group(1).strip()
            if task_line and not _is_diagnostic_task_line(task_line):
                lines.append(task_line)
    return lines


def _is_diagnostic_task_line(line: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(line or "")).strip()
    lowered = normalized.casefold()
    return bool(
        _DIAGNOSTIC_TASK_PREFIX_RE.search(normalized)
        or _DIAGNOSTIC_TASK_CONTENT_RE.search(normalized)
        or "apiエラーでこのタスクを終了しました" in lowered
    )


def _conversation_card_description(conversation: dict[str, Any], *, limit: int) -> str:
    lines = []
    for raw_line in _conversation_excerpt(conversation, limit=9000).splitlines():
        content = re.sub(
            r"^\s*(?:user|assistant|system|tool)\s*:\s*", "", str(raw_line or ""), flags=re.IGNORECASE
        ).strip()
        content = re.sub(r"^\s*[-*•]\s*", "", content).strip()
        if content and not _is_conversation_import_noise_line(content):
            lines.append(content)
    return _compact_text("\n".join(lines), limit)


def _is_conversation_import_noise_line(line: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(line or "")).strip()
    lowered = normalized.casefold()
    return _is_diagnostic_task_line(normalized) or any(
        token in lowered
        for token in (
            "モデル/api の使用許可が必要",
            "ユーザーがモデル/api の使用を許可",
            "承認済みのリクエストとして続行",
            "apiエラーでこのタスクを終了しました",
        )
    )


def _fallback_checklist(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": f"import-{index + 1}", "title": _compact_text(line, 120), "done": False}
        for index, line in enumerate(_task_like_lines(conversation)[:8])
    ]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or item.get("content") or "")
        for item in content
        if isinstance(item, dict)
    )


def _compact_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"
