"""Dependency-free saved-turn initial input validation for Host and guest."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .canonical import canonical_json, strict_loads
from .saved_tools import validate_tool_selection
from .saved_context import saved_prompt_reference
from .saved_task_context import validate_saved_task_context
from .conversation_context import validate_context_binding

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


@dataclass(frozen=True)
class SavedWorkspaceResolution:
    """Host-local owner facts; never a request field or authority grant."""

    profile_id: str
    conversation_id: str
    conversation_revision: int
    workspace_id: str
    mount_revision: int
    root_st_dev: int
    root_st_ino: int


def validate_saved_conversation_context(
    conversation: Mapping[str, Any], *,
    workspace_resolution: SavedWorkspaceResolution | None = None,
) -> None:
    """Validate prompt references and reject other unresolved owned context.

    A prompt reference still needs an authenticated owner read before claiming
    a new turn. Syntax validation is not evidence that the prompt is available.
    """
    saved_prompt_reference(conversation)
    metadata = conversation.get("metadata") or {}
    tags = conversation.get("tags") or []
    if not isinstance(metadata, Mapping) or not isinstance(tags, list):
        raise ValueError("saved bridge owned context is invalid")
    workspace_id = metadata.get("workspace_id")
    if workspace_id and (
        type(workspace_resolution) is not SavedWorkspaceResolution
        or workspace_resolution.conversation_id != conversation.get("id")
        or workspace_resolution.conversation_revision != conversation.get(
            "conversation_revision"
        )
        or workspace_resolution.workspace_id != workspace_id
        or type(workspace_resolution.conversation_revision) is not int
        or workspace_resolution.conversation_revision < 1
        or any(type(value) is not int or value < 0 for value in (
            workspace_resolution.mount_revision,
            workspace_resolution.root_st_dev,
            workspace_resolution.root_st_ino,
        ))
        or type(workspace_resolution.profile_id) is not str
        or type(workspace_resolution.workspace_id) is not str
        or not _ID.fullmatch(workspace_resolution.profile_id)
        or not _ID.fullmatch(workspace_resolution.workspace_id)
    ):
        raise ValueError("saved bridge workspace resolution is required")
    if (
        conversation.get("agent_id")
        or conversation.get("conversation_kind") not in (None, "", "chat")
        or conversation.get("group_id")
        or any(
            metadata.get(key)
            for key in (
                "group_id",
                "groupId",
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


def validate_chat_references(
    value: Any, *, profile_id: str | None = None
) -> list[dict[str, Any]]:
    """Validate explicit lean confirmed references, never infer them from text."""
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("saved chat references are invalid")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "profile_id", "id"}:
            raise ValueError("saved chat reference fields are invalid")
        if not isinstance(item["kind"], str) or item["kind"] not in {"chat", "group"}:
            raise ValueError("saved chat reference kind is invalid")
        for field in ("profile_id", "id"):
            if not isinstance(item[field], str) or _ID.fullmatch(item[field]) is None:
                raise ValueError("saved chat reference identity is invalid")
        if profile_id is not None and item["profile_id"] != profile_id:
            raise ValueError("saved chat reference profile mismatch")
        key = (item["kind"], item["profile_id"], item["id"])
        if key in seen:
            raise ValueError("duplicate saved chat reference")
        seen.add(key)
    return strict_loads(canonical_json(value))


def saved_guidance_context(
    guidance: Mapping[str, Any], *, profile_id: str
) -> dict[str, Any]:
    """Normalize finite saved-input preferences, never metadata execution grants."""
    result: dict[str, Any] = {}
    if "chat_references" in guidance:
        result["chat_references"] = validate_chat_references(
            guidance["chat_references"], profile_id=profile_id
        )
    if "tool_selection" in guidance:
        result["tool_selection"] = validate_tool_selection(guidance["tool_selection"])
    if "action_approval_mode" in guidance:
        mode = guidance["action_approval_mode"]
        if type(mode) is not str or mode not in {"ask", "agent", "full"}:
            raise ValueError("saved guidance action approval mode is invalid")
        result["action_approval_mode"] = mode
    return result


def validate_resolved_chat_references(value: Any, references: Any) -> dict[str, Any]:
    """Validate owner-resolved metadata; this syntax check grants no authority."""
    lean = validate_chat_references(references)
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "profile_id",
        "store_revision",
        "project_revision",
        "references",
        "snapshot_time", "expires_at", "next_cursor", "truncated",
    }:
        raise ValueError("resolved chat reference snapshot fields are invalid")
    if value["kind"] != "tobkiri.chat.reference.snapshot.v1":
        raise ValueError("resolved chat reference snapshot kind is invalid")
    if (
        not isinstance(value["profile_id"], str)
        or _ID.fullmatch(value["profile_id"]) is None
    ):
        raise ValueError("resolved chat reference profile is invalid")
    validate_chat_references(lean, profile_id=value["profile_id"])
    for field in ("store_revision", "project_revision"):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError("resolved chat reference revision is invalid")
    if value["next_cursor"] is not None or value["truncated"] is not False:
        raise ValueError("resolved chat reference snapshot is incomplete")
    rows = value["references"]
    if not isinstance(rows, list) or len(rows) != len(lean):
        raise ValueError("resolved chat references do not match")
    for row, requested in zip(rows, lean):
        if (
            not isinstance(row, dict)
            or set(row)
            != {"kind", "id", "label", "conversation_ids", "snapshot_digest",
                "member_count", "membership_complete"}
            or any(row[field] != requested[field] for field in ("kind", "id"))
        ):
            raise ValueError("resolved chat reference identity mismatch")
        if (
            not isinstance(row["label"], str)
            or not row["label"].strip()
            or len(row["label"]) > 256
            or len(row["label"].encode()) > 1024
        ):
            raise ValueError("resolved chat reference label is invalid")
        members = row["conversation_ids"]
        if not isinstance(members, list) or not 0 <= len(members) <= 256:
            raise ValueError("resolved chat reference members are invalid")
        if any(
            not isinstance(member, str) or _ID.fullmatch(member) is None
            for member in members
        ):
            raise ValueError("resolved chat reference member identity is invalid")
        if (type(row["member_count"]) is not int
            or not 0 <= row["member_count"] <= 4096
            or row["membership_complete"] is not True
            or row["member_count"] != len(members)):
            raise ValueError("resolved chat reference membership is incomplete")
        if len(set(members)) != len(members):
            raise ValueError("duplicate resolved chat reference member")
        if row["kind"] == "chat" and members != [row["id"]]:
            raise ValueError("resolved chat reference members do not match chat")
        if (
            not isinstance(row["snapshot_digest"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", row["snapshot_digest"]) is None
        ):
            raise ValueError("resolved chat reference digest is invalid")
    for field in ("snapshot_time", "expires_at"):
        if field in value and (
            type(value[field]) is not int or value[field] < 0
        ):
            raise ValueError("resolved chat reference timestamp is invalid")
    if "snapshot_time" in value and "expires_at" in value and not (
        value["snapshot_time"] < value["expires_at"]
        == value["snapshot_time"] + 600_000
    ):
        raise ValueError("resolved chat reference expiry is invalid")
    if len(canonical_json(value)) > 64 * 1024:
        raise ValueError("resolved chat reference snapshot exceeds byte limit")
    return strict_loads(canonical_json(value))


def saved_chat_reference_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Display confirmed IDs and membership as reference data, never send authority."""
    if "resolved_chat_references" not in request:
        return []
    snapshot = validate_resolved_chat_references(
        request["resolved_chat_references"], request.get("chat_references", [])
    )
    return [
        {
            "role": "user",
            "content": ("Confirmed chat/group references (reference data only; "
                        "no send authority; no other chat content):\n")
            + canonical_json(snapshot).decode("utf-8"),
        }
    ]


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
        "action_approval_mode",
        "task_context",
        "context_binding",
        "chat_references",
        "resolved_chat_references",
    } != {
        "turn_id",
        "conversation_id",
        "conversation_revision",
        "content",
    }:
        raise ValueError("saved turn request fields are invalid")
    if "chat_references" in request:
        validate_chat_references(request["chat_references"])
    if "resolved_chat_references" in request:
        validate_resolved_chat_references(
            request["resolved_chat_references"], request.get("chat_references", [])
        )
    if "tool_selection" in request:
        validate_tool_selection(request["tool_selection"])
    if "task_context" in request:
        validate_saved_task_context(
            request["task_context"],
            conversation_id=request["conversation_id"],
            turn_id=request["turn_id"],
        )
    if "context_binding" in request:
        validate_context_binding(request["context_binding"])
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
    # Preference data only: native Host policy must independently authorize it.
    if "action_approval_mode" in request and (
        type(request["action_approval_mode"]) is not str
        or request["action_approval_mode"] not in {"ask", "agent", "full"}
    ):
        raise ValueError("saved turn action approval mode is invalid")
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
