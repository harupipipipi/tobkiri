"""Pure legacy Kanban prompt-note compatibility helper."""

from __future__ import annotations

from typing import Any, Mapping


def append_kanban_system_prompt_note(
    prompt: str,
    conv: Mapping[str, Any] | None,
) -> str:
    """Append an existing persisted note without accessing Kanban state."""

    metadata = conv.get("metadata") if isinstance(conv, Mapping) else None
    kanban = metadata.get("kanban") if isinstance(metadata, Mapping) else None
    note = (
        str(kanban.get("system_prompt_note") or "").strip()
        if isinstance(kanban, Mapping)
        else ""
    )
    if not note:
        return prompt
    normalized = str(prompt or "").strip()
    return f"{normalized}\n\n{note}" if normalized else note
