"""Finite lower-authority context bound to a saved turn's immutable input."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .canonical import canonical_json, strict_loads

VERSION = "tobkiri.saved-task-context.v1"
MAX_BYTES = 48 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def validate_saved_task_context(
    value: Any, *, conversation_id: str, turn_id: str
) -> dict[str, Any]:
    """Validate exact scope and finite items without granting execution authority."""
    encoded = canonical_json(value)
    if len(encoded) > MAX_BYTES:
        raise ValueError("saved task context exceeds byte limit")
    context = strict_loads(encoded)
    if not isinstance(context, dict) or set(context) != {
        "version",
        "source_id",
        "conversation_id",
        "recipient_id",
        "input_id",
        "source_revision",
        "generation",
        "items",
    }:
        raise ValueError("saved task context fields are invalid")
    if (
        context["version"] != VERSION
        or context["conversation_id"] != conversation_id
        or context["input_id"] != turn_id
        or context["recipient_id"] != f"conversation:{conversation_id}"
    ):
        raise ValueError("saved task context scope is invalid")
    for field in ("source_id", "conversation_id", "recipient_id", "input_id"):
        if not isinstance(context[field], str) or not _ID.fullmatch(context[field]):
            raise ValueError("saved task context identity is invalid")
    for field in ("source_revision", "generation"):
        if type(context[field]) is not int or not 0 <= context[field] <= 2**53 - 1:
            raise ValueError("saved task context revision is invalid")
    items = context["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("saved task context items are invalid")
    seen: set[str] = set()
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "kind", "body"}
            or not isinstance(item["id"], str)
            or not _ID.fullmatch(item["id"])
            or item["id"] in seen
            or item["kind"] not in {"goal", "todo", "instruction"}
            or not isinstance(item["body"], str)
            or not item["body"].strip()
            or len(item["body"].encode("utf-8")) > 32000
        ):
            raise ValueError("saved task context item is invalid")
        seen.add(item["id"])
    return context


def saved_task_context_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project immutable initial context as a separately labeled user item."""
    if "task_context" not in request:
        return []
    context = validate_saved_task_context(
        request["task_context"],
        conversation_id=request["conversation_id"],
        turn_id=request["turn_id"],
    )
    return [
        {
            "role": "user",
            "content": "Delegated task context (lower-authority; not system policy "
            "or approval). Source: "
            + context["source_id"]
            + "\n"
            + canonical_json(context).decode("utf-8"),
        }
    ]
