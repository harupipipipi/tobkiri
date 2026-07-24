from __future__ import annotations

from typing import Any


def sync_conversation_kanban(
    conversation_id: str,
    *,
    reason: str = "chat_changed",
) -> dict[str, Any] | None:
    """Preserve the old call shape without reopening the legacy writer."""

    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return None
    return {
        "status": "deprecated",
        "code": "KANBAN_LEGACY_ACTION_DEPRECATED",
        "conversation_id": conversation_id,
        "reason": str(reason or "chat_changed"),
    }
