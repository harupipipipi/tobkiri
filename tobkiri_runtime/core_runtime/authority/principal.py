"""Principal helpers for authority checks."""

from __future__ import annotations

UNATTRIBUTED_PRINCIPAL_ID = "host:unattributed"


def build_principal_id(
    *,
    profile_id: str | None = None,
    graph_id: str | None = None,
    node_id: str | None = None,
    tool_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    profile_id = str(profile_id or "").strip()
    graph_id = str(graph_id or "").strip()
    node_id = str(node_id or "").strip()
    tool_id = str(tool_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if profile_id:
        parts = [f"profile:{profile_id}"]
        if graph_id:
            parts.append(f"graph:{graph_id}")
        if node_id:
            parts.append(f"node:{node_id}")
        if tool_id:
            parts.append(f"tool:{tool_id}")
        return "__".join(parts)
    if conversation_id:
        return f"conversation:{conversation_id}"
    return UNATTRIBUTED_PRINCIPAL_ID


def principal_scope_candidates(
    principal_id: str,
    *,
    conversation_id: str | None = None,
) -> list[str]:
    """Return authority grant candidates in priority order."""
    candidates: list[str] = []

    def add(value: str | None) -> None:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    principal_id = str(principal_id or "").strip() or UNATTRIBUTED_PRINCIPAL_ID
    add(principal_id)

    if "__" in principal_id:
        parts = principal_id.split("__")
        for index in range(len(parts) - 1, 0, -1):
            add("__".join(parts[:index]))

    if principal_id.startswith("profile:"):
        add(principal_id.split("__", 1)[0])
        return candidates

    conversation_id = str(conversation_id or "").strip()
    if conversation_id:
        add(f"conversation:{conversation_id}")
    if principal_id.startswith("conversation:"):
        add(principal_id)
    add("global")
    return candidates


def parse_principal_parts(principal_id: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for segment in str(principal_id or "").split("__"):
        if ":" not in segment:
            continue
        key, value = segment.split(":", 1)
        if key and value:
            parts[key] = value
    return parts
