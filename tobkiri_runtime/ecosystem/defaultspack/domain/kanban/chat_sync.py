from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import KanbanOwnerUnavailable, StateStoreFactory


def sync_conversation_kanban(
    conversation_id: str,
    *,
    reason: str = "chat_changed",
    db_path: str | Path | None = None,
    state_store_factory: StateStoreFactory | None = None,
) -> dict[str, Any] | None:
    """Re-project one conversation through the canonical Kanban adapter."""

    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return None
    from domain.chat.store import ChatStore

    conversation = ChatStore().get_conversation(conversation_id)
    if conversation is None:
        return None
    metadata = conversation.get("metadata") if isinstance(conversation, dict) else {}
    kanban = metadata.get("kanban") if isinstance(metadata, dict) else {}
    board_id = str(kanban.get("board_id") or "").strip() if isinstance(kanban, dict) else ""

    from .service import KanbanService

    try:
        service = KanbanService(
            db_path=db_path,
            state_store_factory=state_store_factory,
        )
    except KanbanOwnerUnavailable:
        # Chat remains usable when the selected profile does not expose the
        # optional Kanban owner.  This is a fail-closed projection skip, not a
        # legacy-storage fallback.
        return {
            "status": "skipped",
            "code": "KANBAN_OWNER_UNAVAILABLE",
            "conversation_id": conversation_id,
            "reason": "canonical Kanban owner is unavailable",
        }
    if not board_id:
        group_id = str(
            conversation.get("group_id")
            or (metadata.get("group_id") if isinstance(metadata, dict) else "")
            or ""
        ).strip()
        scope_type = "group" if group_id else "conversation"
        scope_id = group_id or conversation_id
        board_id = service.bootstrap_board(
            {"scope_type": scope_type, "scope_id": scope_id}
        )["board"]["board_id"]
    snapshot = service.import_conversation(
        board_id,
        {
            "conversation_id": conversation_id,
            "use_ai": False,
            "sync_reason": str(reason or "chat_changed"),
        },
    )
    snapshot["sync"] = {
        "conversation_id": conversation_id,
        "reason": str(reason or "chat_changed"),
    }
    return snapshot
