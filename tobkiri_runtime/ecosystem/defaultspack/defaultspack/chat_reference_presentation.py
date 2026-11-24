"""Bind read-only chat reference routes to the captured Profile."""

from __future__ import annotations

import re
from typing import Mapping

_REFERENCE = "tobkiri.resource.chat.reference.v1"
_PROVIDER = "rumi_conversation_store_pack.chat-reference.resource"
_OPERATION = "rumi_conversation_store_pack.chat-reference-read"
CHAT_REFERENCE_LIST_TARGET = (
    "defaults.chat.references.list", _REFERENCE, _OPERATION, _PROVIDER, _PROVIDER,
)
CHAT_REFERENCE_RESOLVE_TARGET = (
    "defaults.chat.references.resolve", _REFERENCE, _OPERATION, _PROVIDER, _PROVIDER,
)
CHAT_REFERENCE_TARGETS = frozenset({
    CHAT_REFERENCE_LIST_TARGET, CHAT_REFERENCE_RESOLVE_TARGET,
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def normalize_chat_reference_read(
    identity: tuple[str, str, str, str, str],
    payload: Mapping[str, object],
    *,
    profile_id: str,
) -> dict[str, object]:
    """Accept bounded pagination or lean IDs without caller-owned identity."""
    if not profile_id or _ID.fullmatch(profile_id) is None:
        raise ValueError("chat references require a captured Profile")
    if identity == CHAT_REFERENCE_LIST_TARGET:
        if set(payload) - {"limit", "cursor"}:
            raise ValueError("chat reference list fields are invalid")
        result: dict[str, object] = {"profile_id": profile_id, "operation": "list"}
        if "limit" in payload:
            limit = payload["limit"]
            if isinstance(limit, str) and re.fullmatch(r"[1-9][0-9]{0,2}", limit):
                limit = int(limit)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("chat reference list limit is invalid")
            result["limit"] = limit
        if "cursor" in payload:
            cursor = payload["cursor"]
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 96:
                raise ValueError("chat reference list cursor is invalid")
            result["cursor"] = cursor
        return result
    if identity != CHAT_REFERENCE_RESOLVE_TARGET or set(payload) != {"references"}:
        raise ValueError("chat reference resolve fields are invalid")
    references = payload["references"]
    if not isinstance(references, list) or not 1 <= len(references) <= 16:
        raise ValueError("chat reference count is invalid")
    seen: set[tuple[str, str]] = set()
    checked = []
    for reference in references:
        if not isinstance(reference, Mapping) or set(reference) != {"kind", "id"}:
            raise ValueError("chat reference fields are invalid")
        kind, identifier = reference["kind"], reference["id"]
        if kind not in ("chat", "group") or not isinstance(identifier, str):
            raise ValueError("chat reference identity is invalid")
        if _ID.fullmatch(identifier) is None or (kind, identifier) in seen:
            raise ValueError("chat reference identity is invalid")
        seen.add((kind, identifier))
        checked.append({"kind": kind, "id": identifier})
    return {"profile_id": profile_id, "operation": "resolve", "references": checked}
