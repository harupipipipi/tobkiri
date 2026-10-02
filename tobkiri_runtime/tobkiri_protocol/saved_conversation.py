"""Dependency-free saved-turn initial input validation for Host and guest."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import re
from typing import Any

from .canonical import canonical_json, strict_loads
from .saved_tools import validate_tool_selection
from .saved_context import saved_prompt_reference

SAVED_CONVERSATION_CONTRACT = "conversation.saved-turn.v1"
SAVED_CONVERSATION_OPERATION = "saved_complete"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SAVED_IMAGE_URL = re.compile(
    r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})\Z"
)

# Saved turns deliberately carry only small, self-contained images.  The
# content crosses four authenticated continuation hops, so these per-turn
# limits include enough headroom for the base64 representation without
# changing the generic PackVM or canonical-JSON budgets.
MAX_SAVED_TEXT_BYTES = 60 * 1024
MAX_SAVED_IMAGE_COUNT = 2
MAX_SAVED_IMAGE_BYTES = 1024 * 1024
MAX_SAVED_IMAGE_BASE64_CHARS = ((MAX_SAVED_IMAGE_BYTES + 2) // 3) * 4
MAX_SAVED_INPUT_BYTES = 3 * 1024 * 1024
MAX_SAVED_FRAME_BYTES = 4 * 1024 * 1024
MAX_SAVED_CHAIN_BYTES = 16 * 1024 * 1024


def is_saved_text_content(value: Any) -> bool:
    """Recognize final text or exact text blocks without granting authority."""
    return bool(value) and (
        isinstance(value, str)
        or isinstance(value, list)
        and all(
            isinstance(part, dict)
            and set(part) == {"type", "text"}
            and part["type"] == "text"
            and isinstance(part["text"], str)
            for part in value
        )
    )


def _saved_text(value: Any) -> bool:
    """Validate bounded nonempty text used in saved user content."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.encode("utf-8")) <= MAX_SAVED_TEXT_BYTES
    )


def _saved_image_url(value: Any) -> bool:
    """Accept only a bounded inline raster data URL, never an external URL."""
    if not isinstance(value, str):
        return False
    match = _SAVED_IMAGE_URL.fullmatch(value)
    if match is None:
        return False
    encoded = match.group(2)
    if len(encoded) % 4 or len(encoded) > MAX_SAVED_IMAGE_BASE64_CHARS:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return False
    return 0 < len(decoded) <= MAX_SAVED_IMAGE_BYTES


def is_saved_user_content(value: Any) -> bool:
    """Recognize bounded text plus inline raster blocks for one saved turn."""
    if _saved_text(value):
        return True
    if not isinstance(value, list) or not 1 <= len(value) <= 1 + MAX_SAVED_IMAGE_COUNT:
        return False
    text = value[0]
    if (
        not isinstance(text, Mapping)
        or set(text) != {"type", "text"}
        or text.get("type") != "text"
        or not _saved_text(text.get("text"))
    ):
        return False
    return all(
        isinstance(part, Mapping)
        and set(part) == {"type", "image_url"}
        and part.get("type") == "image_url"
        and isinstance(part.get("image_url"), Mapping)
        and set(part["image_url"]) == {"url"}
        and _saved_image_url(part["image_url"].get("url"))
        for part in value[1:]
    )


def saved_user_text(value: Any) -> str:
    """Return saved user text while keeping prior inline images out of history."""
    if not is_saved_user_content(value):
        raise ValueError("saved turn user content is invalid")
    if isinstance(value, str):
        return value
    image_count = len(value) - 1
    suffix = "\n[Earlier inline image omitted from saved context.]" * image_count
    return value[0]["text"] + suffix


def validate_saved_conversation_context(conversation: Mapping[str, Any]) -> None:
    """Validate prompt references and reject other unresolved owned context.

    A prompt reference still needs an authenticated owner read before claiming
    a new turn. Syntax validation is not evidence that the prompt is available.
    """
    saved_prompt_reference(conversation)
    metadata = conversation.get("metadata") or {}
    tags = conversation.get("tags") or []
    if not isinstance(metadata, Mapping) or not isinstance(tags, list):
        raise ValueError("saved bridge owned context is invalid")
    if (
        conversation.get("agent_id")
        or conversation.get("conversation_kind") not in (None, "", "chat")
        or conversation.get("group_id")
        or any(
            metadata.get(key)
            for key in (
                "group_id",
                "groupId",
                "workspace_id",
                "workspaceId",
                "workspace_root",
                "workspaceRoot",
                "rootPath",
                "rumi_data_path",
                "rumiDataPath",
                "rumi_dp_path",
                "shared_read_only",
            )
        )
        or metadata.get("mode") not in (None, "", "chat")
        or metadata.get("profile_id")
        in (
            "defaultspack.operations_company",
            "defaultspack.mimo_coding_company",
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
        "turn_id",
        "conversation_id",
        "conversation_revision",
        "content",
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
    if not is_saved_user_content(request["content"]):
        raise ValueError("saved turn content is invalid")
    if len(canonical_json(request)) > MAX_SAVED_INPUT_BYTES:
        raise ValueError("saved turn input exceeds byte limit")
    return initial
