"""Project one conversation into the global Kanban contracts."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
CONVERSATION_RESOURCE = "rumi.resource.conversation.v1"
KANBAN_RESOURCE = "rumi.resource.kanban.v1"
KANBAN_ACTION = "rumi.action.kanban.v1"
SERVICE_PACK_ID = "rumi_kanban_conversation_adapter_pack"
STATE_PACK_ID = "rumi_kanban_state_store_pack"
_TASK = re.compile(
    r"^\s*(?:[-*•]\s*(?:\[[ xX]\]\s*)?|"
    r"(?:todo|task|action|next|fix|bug)\s*[:：]|(?:\d+|[a-zA-Z])[.)]\s+)(.+)$",
    re.IGNORECASE,
)


class KanbanConversationAdapter:
    """Extract deterministic task cards without importing chat or Kanban code."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def import_conversation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Import public task lines from one global conversation into one board."""

        profile_id = str(payload.get("profile_id") or "default")
        board_id = _identifier(payload.get("board_id"), "board_id")
        conversation_id = _identifier(
            payload.get("conversation_id") or payload.get("source_id"),
            "conversation_id",
        )
        board = self._board(profile_id, board_id)
        conversation = self.client.invoke(
            CONVERSATION_RESOURCE,
            "get",
            {"profile_id": profile_id, "conversation_id": conversation_id},
        )
        if not isinstance(conversation, Mapping):
            raise KeyError("conversation is unknown")
        tasks = _tasks(conversation, payload)
        if not tasks:
            return {
                "status": "skipped",
                "reason": "conversation contains no task lines",
                "board_id": board_id,
                "conversation_id": conversation_id,
                "card_ids": [],
                "changed": 0,
            }
        column_id = _first_column(board)
        existing = _conversation_cards(board, conversation_id)
        saved: list[str] = []
        changed = 0
        for index, title in enumerate(tasks):
            card_id = _existing_card_id(existing, index) or (
                "conversation-" + _hash(f"{conversation_id}\0{index}")[:40]
            )
            record = {
                "id": card_id,
                "column_id": column_id,
                "position": index,
                "title": title,
                "source_type": "conversation",
                "source_id": conversation_id,
                "conversation_id": conversation_id,
                "metadata": {
                    "conversation_import": {
                        "task_index": index,
                        "conversation_revision": int(
                            conversation.get("conversation_revision") or 0
                        ),
                    }
                },
            }
            current = existing.pop(card_id, None)
            if not _same_card(current, record):
                self._state_action(
                    profile_id,
                    "card.upsert",
                    {"board_id": board_id, "record": record},
                )
                changed += 1
            saved.append(card_id)
        for card_id in sorted(existing):
            self._state_action(
                profile_id,
                "card.delete",
                {"board_id": board_id, "record_id": card_id},
            )
            changed += 1
        event_id = "conversation-import-" + _hash(
            conversation_id + "\0" + "\0".join(tasks)
        )[:40]
        event = self._state_action(
            profile_id,
            "event.append",
            {
                "board_id": board_id,
                "record": {
                    "id": event_id,
                    "type": "conversation.imported",
                    "payload": {
                        "conversation_id": conversation_id,
                        "card_ids": saved,
                        "task_count": len(saved),
                    },
                },
            },
        )
        return {
            "status": "accepted",
            "board_id": board_id,
            "conversation_id": conversation_id,
            "card_ids": saved,
            "changed": changed,
            "event_deduplicated": bool(event.get("deduplicated")),
        }

    def _board(self, profile_id: str, board_id: str) -> dict[str, Any]:
        value = self.client.invoke(
            KANBAN_RESOURCE,
            "get",
            {"profile_id": profile_id, "board_id": board_id},
        )
        if not isinstance(value, Mapping):
            raise KeyError("Kanban board is unknown")
        return dict(value)

    def _state_action(
        self,
        profile_id: str,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = self.client.invoke(
            KANBAN_RESOURCE,
            "list",
            {"profile_id": profile_id},
        )
        exact = {
            "expected_revision": int(state.get("revision") or 0),
            **arguments,
        }
        scope = {
            "service_pack_id": STATE_PACK_ID,
            "operation": f"kanban.state.{name}",
            "authority": "kanban.state.manage",
            "caller_id": "kanban.conversation.adapter",
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"kanban.conversation.{name}",
            "profile_id": profile_id,
            "workspace_id": "",
            "session_id": "",
            "arguments": exact,
            "approval_required": False,
        }
        issued = self.client.invoke(AUTHORITY, "authorize", scope)
        if not issued.get("authorized"):
            raise PermissionError(str(issued.get("reason") or "Kanban state denied"))
        result = self.client.invoke(
            KANBAN_ACTION,
            name,
            {
                **exact,
                "profile_id": profile_id,
                "authority_receipt": str(issued.get("receipt") or ""),
                "caller_id": scope["caller_id"],
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": scope["caller_function_id"],
                "session_id": "",
            },
        )
        return dict(result) if isinstance(result, Mapping) else {}


def create_kanban_conversation_import(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the contract-only conversation import operation."""

    adapter = KanbanConversationAdapter(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "import":
            raise ValueError(f"unknown Kanban conversation operation: {name}")
        return adapter.import_conversation(payload)

    return operation


def _tasks(conversation: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    messages = conversation.get("messages")
    messages = messages if isinstance(messages, list) else []
    tasks: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        for line in content.splitlines():
            match = _TASK.match(line)
            if match:
                tasks.append(_text(match.group(1), 500))
    if not tasks:
        title = _text(payload.get("fallback_title"), 500)
        if title:
            tasks.append(title)
    return list(dict.fromkeys(item for item in tasks if item))[:8]


def _first_column(board: Mapping[str, Any]) -> str:
    columns = board.get("columns")
    if not isinstance(columns, Mapping):
        raise ValueError("Kanban board has no columns")
    candidates = [item for item in columns.values() if isinstance(item, Mapping)]
    if not candidates:
        raise ValueError("Kanban board has no columns")
    first = min(
        candidates,
        key=lambda item: (int(item.get("position") or 0), str(item.get("id") or "")),
    )
    return _identifier(first.get("id"), "column_id")


def _conversation_cards(
    board: Mapping[str, Any],
    conversation_id: str,
) -> dict[str, Mapping[str, Any]]:
    cards = board.get("cards")
    if not isinstance(cards, Mapping):
        return {}
    return {
        str(card_id): card
        for card_id, card in cards.items()
        if isinstance(card, Mapping)
        and card.get("conversation_id") == conversation_id
        and card.get("source_type") == "conversation"
    }


def _existing_card_id(
    cards: Mapping[str, Mapping[str, Any]],
    task_index: int,
) -> str | None:
    for card_id, card in cards.items():
        metadata = card.get("metadata")
        imported = (
            metadata.get("conversation_import")
            if isinstance(metadata, Mapping)
            else None
        )
        if isinstance(imported, Mapping) and imported.get("task_index") == task_index:
            return card_id
    return None


def _same_card(current: Mapping[str, Any] | None, desired: Mapping[str, Any]) -> bool:
    if not isinstance(current, Mapping):
        return False
    return all(current.get(key) == value for key, value in desired.items())


def _identifier(value: Any, label: str) -> str:
    identifier = _text(value, 255)
    if not identifier:
        raise ValueError(f"{label} is required")
    return identifier


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

