"""Dependency-free saved-turn initial input validation for Host and guest."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .canonical import canonical_json, strict_loads
from .saved_tools import validate_tool_selection

SAVED_CONVERSATION_CONTRACT = "conversation.saved-turn.v1"
SAVED_CONVERSATION_OPERATION = "saved_complete"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def is_saved_text_content(value: Any) -> bool:
    """Recognize final text or exact text blocks without granting authority."""
    return bool(value) and (
        isinstance(value, str)
        or isinstance(value, list) and all(
            isinstance(part, dict) and set(part) == {"type", "text"}
            and part["type"] == "text" and isinstance(part["text"], str)
            for part in value
        )
    )


def validate_saved_conversation_context(conversation: Mapping[str, Any]) -> None:
    """Reject unresolved owned context; this pure check grants no authority."""
    metadata = conversation.get("metadata") or {}
    tags = conversation.get("tags") or []
    if not isinstance(metadata, Mapping) or not isinstance(tags, list):
        raise ValueError("saved bridge owned context is invalid")
    if (
        conversation.get("system_prompt_id") or conversation.get("agent_id")
        or conversation.get("conversation_kind") not in (None, "", "chat")
        or conversation.get("group_id")
        or any(metadata.get(key) for key in (
            "group_id", "groupId", "workspace_id", "workspaceId",
            "workspace_root", "workspaceRoot", "rootPath",
            "rumi_data_path", "rumiDataPath", "rumi_dp_path", "shared_read_only",
        ))
        or metadata.get("mode") not in (None, "", "chat")
        or metadata.get("profile_id") in (
            "defaultspack.operations_company", "defaultspack.mimo_coding_company",
        )
        or any(tag in tags for tag in ("operations-company", "mimo-coding-company"))
    ):
        raise ValueError("saved bridge context resolution is required")


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
    if not isinstance(request, dict) or set(request) - {"tool_selection"} != {
        "turn_id", "conversation_id", "conversation_revision", "content",
    }:
        raise ValueError("saved turn request fields are invalid")
    if "tool_selection" in request:
        validate_tool_selection(request["tool_selection"])
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
