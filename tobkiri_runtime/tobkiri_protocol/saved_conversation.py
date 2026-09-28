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

# These larger limits apply only to the saved-turn chain. Generic PackVM
# continuation limits stay unchanged.
MAX_SAVED_TEXT_BYTES = 60 * 1024
MAX_SAVED_IMAGE_COUNT = 2
MAX_SAVED_IMAGE_BYTES = 1024 * 1024
MAX_SAVED_IMAGE_BASE64_CHARS = ((MAX_SAVED_IMAGE_BYTES + 2) // 3) * 4
MAX_SAVED_INPUT_BYTES = 3 * 1024 * 1024
MAX_SAVED_FRAME_BYTES = 4 * 1024 * 1024
MAX_SAVED_CHAIN_BYTES = 16 * 1024 * 1024
# A saved turn can broker an admitted strategy or bounded tool chain. This is
# the common end-to-end ceiling, not a strategy- or Pack-specific allowance.
MAX_SAVED_TURN_TIMEOUT_MS = 300_000
MAX_SAVED_TURN_LIFETIME_SECONDS = MAX_SAVED_TURN_TIMEOUT_MS / 1000


def _matches_saved_image_type(media_type: str, decoded: bytes) -> bool:
    """Verify the claimed raster type from its magic bytes."""
    if media_type == "image/png":
        return decoded.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return decoded.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return decoded.startswith((b"GIF87a", b"GIF89a"))
    return (
        media_type == "image/webp"
        and len(decoded) >= 12
        and decoded[:4] == b"RIFF"
        and decoded[8:12] == b"WEBP"
    )


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
    except (TypeError, ValueError):
        return False
    return (
        0 < len(decoded) <= MAX_SAVED_IMAGE_BYTES
        and _matches_saved_image_type(match.group(1), decoded)
    )


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


def _saved_user_text_bytes(value: Any) -> int | None:
    """Return the user text byte count when the text slot is well formed."""
    if isinstance(value, str) and bool(value.strip()):
        return len(value.encode("utf-8"))
    if (
        isinstance(value, list)
        and 1 <= len(value) <= 1 + MAX_SAVED_IMAGE_COUNT
        and isinstance(value[0], Mapping)
        and set(value[0]) == {"type", "text"}
        and value[0].get("type") == "text"
        and isinstance(value[0].get("text"), str)
        and bool(value[0]["text"].strip())
        and all(
            isinstance(part, Mapping)
            and set(part) == {"type", "image_url"}
            and part.get("type") == "image_url"
            and isinstance(part.get("image_url"), Mapping)
            and set(part["image_url"]) == {"url"}
            and _saved_image_url(part["image_url"].get("url"))
            for part in value[1:]
        )
    ):
        return len(value[0]["text"].encode("utf-8"))
    return None


def saved_user_text(value: Any) -> str:
    """Flatten saved user content for text-only readiness checks."""
    if not is_saved_user_content(value):
        raise ValueError("saved turn user content is invalid")
    if isinstance(value, str):
        return value
    suffix = "\n[Earlier inline image omitted from saved context.]" * (len(value) - 1)
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
    if not isinstance(request, dict) or set(request) - {
        "tool_selection",
        "strategy_reference",
        "strategy_maximum_cost_microusd",
        "thinking_level",
    } != {
        "turn_id",
        "conversation_id",
        "conversation_revision",
        "content",
    }:
        raise ValueError("saved turn request fields are invalid")
    if "tool_selection" in request:
        validate_tool_selection(request["tool_selection"])
    strategy_reference = request.get("strategy_reference")
    if strategy_reference is not None and (
        not isinstance(strategy_reference, str)
        or _ID.fullmatch(strategy_reference) is None
    ):
        raise ValueError("saved turn strategy reference is invalid")
    strategy_cost = request.get("strategy_maximum_cost_microusd")
    if strategy_cost is not None and (
        type(strategy_cost) is not int
        or not 1 <= strategy_cost <= 1_000_000
    ):
        raise ValueError("saved turn strategy maximum cost is invalid")
    if "thinking_level" in request and (
        not isinstance(request["thinking_level"], str)
        or request["thinking_level"] not in {
            "none", "low", "medium", "high", "xhigh"
        }
    ):
        raise ValueError("saved turn thinking level is invalid")
    for field in ("turn_id", "conversation_id"):
        if not isinstance(request[field], str) or _ID.fullmatch(request[field]) is None:
            raise ValueError("saved turn identity is invalid")
    revision = request["conversation_revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("saved turn revision is invalid")
    content = request["content"]
    text_bytes = _saved_user_text_bytes(content)
    if text_bytes is not None and text_bytes > MAX_SAVED_TEXT_BYTES:
        raise ValueError("saved turn input exceeds byte limit")
    if not is_saved_user_content(content):
        raise ValueError("saved turn content is invalid")
    if len(canonical_json(request)) > MAX_SAVED_INPUT_BYTES:
        raise ValueError("saved turn input exceeds byte limit")
    return initial
