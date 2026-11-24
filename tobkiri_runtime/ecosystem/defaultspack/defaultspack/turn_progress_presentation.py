"""Normalize the captured Chat progress read without accepting producer claims."""

from __future__ import annotations

import re
from typing import Mapping

from tobkiri_protocol.turn_progress_v1 import MAX_EVENTS

TURN_PROGRESS_TARGET = (
    "defaults.conversations.turn.progress",
    "tobkiri.resource.turn.progress.v1",
    "rumi_turn_runtime_pack.turn-progress-resource",
    "rumi_turn_runtime_pack.turn-runtime.progress-resource",
    "rumi_turn_runtime_pack.turn-runtime.progress-resource",
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_PROGRESS_ID = re.compile(r"[0-9a-f]{64}\Z")


def normalize_turn_progress_read(payload: Mapping[str, object]) -> dict[str, object]:
    """Convert only the bounded GET cursor and validate immutable read scope."""
    if set(payload) - {"progress_id"} != {"turn_id", "conversation_id", "cursor"}:
        raise ValueError("turn progress read fields are invalid")
    for field in ("turn_id", "conversation_id"):
        value = payload[field]
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("turn progress identity is invalid")
    cursor = payload["cursor"]
    if isinstance(cursor, str) and re.fullmatch(r"0|[1-9][0-9]{0,3}", cursor):
        cursor = int(cursor)
    if type(cursor) is not int or not 0 <= cursor <= MAX_EVENTS:
        raise ValueError("turn progress cursor is invalid")
    progress_id = payload.get("progress_id")
    if "progress_id" in payload and (
        not isinstance(progress_id, str) or _PROGRESS_ID.fullmatch(progress_id) is None
    ):
        raise ValueError("turn progress stage identity is invalid")
    return {**payload, "cursor": cursor}
