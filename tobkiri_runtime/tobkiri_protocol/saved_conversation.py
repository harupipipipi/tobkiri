"""Dependency-free saved-turn initial input validation for Host and guest."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .canonical import canonical_json, strict_loads

SAVED_CONVERSATION_CONTRACT = "conversation.saved-turn.v1"
SAVED_CONVERSATION_OPERATION = "saved_complete"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def validate_saved_conversation_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fresh initial request, never guest-owned continuation state.

    This finite check mirrors saved_conversation_input_v1.schema.json without
    requiring jsonschema or filesystem access inside the guest interpreter.
    It also bounds encoded request bytes; character counts alone are not a
    wire budget. Validation cannot authorize or start any execution.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("saved turn input must be an object")
    initial = strict_loads(canonical_json(dict(payload)))
    if set(initial) != {"request"}:
        raise ValueError("saved turn initial fields are invalid")
    request = initial["request"]
    if not isinstance(request, dict) or set(request) != {
        "turn_id", "conversation_id", "conversation_revision", "content",
    }:
        raise ValueError("saved turn request fields are invalid")
    for field in ("turn_id", "conversation_id"):
        if not isinstance(request[field], str) or _ID.fullmatch(request[field]) is None:
            raise ValueError("saved turn identity is invalid")
    revision = request["conversation_revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("saved turn revision is invalid")
    if not isinstance(request["content"], str) or not request["content"].strip():
        raise ValueError("saved turn text is empty or invalid")
    if len(canonical_json(request)) > 60 * 1024:
        raise ValueError("saved turn input exceeds byte limit")
    return initial
