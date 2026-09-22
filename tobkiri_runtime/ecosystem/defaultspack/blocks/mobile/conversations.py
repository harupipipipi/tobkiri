"""blocks.mobile.conversations — モバイル向け会話facade.

PC上の会話をスマホから操作するための安定したAPI。
既存の ChatStore / ChatRunEngine への薄いラッパー。

ルート:
  GET    /api/mobile/v1/conversations           → list
  POST   /api/mobile/v1/conversations           → create
  GET    /api/mobile/v1/conversations/{id}      → get
  POST   /api/mobile/v1/conversations/{id}/stream → send + SSE stream
  POST   /api/mobile/v1/conversations/{id}/stop   → stop run
  POST   /api/mobile/v1/conversations/{id}/branch → branch
  POST   /api/mobile/v1/conversations/import-branch → import from mobile
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from typing import Any


from blocks._common import error, ok


# The mobile drawer only needs a short, human-readable hint.  Keep this
# boundary in the mobile facade rather than teaching the canonical owner about
# one consumer's display policy.
MAX_PREVIEW_LENGTH = 160

_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_FIELD_NAME_RE = (
    r"(?:[a-z][a-z0-9_-]*[_-])?"
    r"(?:api[_-]?key|x-api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|proxy-authorization|bearer|"
    r"credential|password|secret|private[_-]?key|encryption[_-]?key|"
    r"token|key)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?ix)"
    rf"(?P<prefix>['\"]?\b{_SECRET_FIELD_NAME_RE}\b['\"]?\s*[:=]\s*)"
    r"(?P<value>[^\s,;}\]\[\)\(\"'`]+)"
)
_SECRET_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?ix)"
    rf"(?P<prefix>['\"]?\b{_SECRET_FIELD_NAME_RE}\b['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
)
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(?P<scheme>bearer|basic|token)\s+"
    r"(?P<value>[A-Za-z0-9._~+/=-]{8,})\b"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{6,}\b"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk|sk_live|sk_test|ghp|gho|ghs|ghr|github_pat|xox[baprs]-|"
    r"AIza|AKIA|ya29|hf|npm|pypi)[-_A-Za-z0-9]{8,}\b",
    re.IGNORECASE,
)
_PRIVATE_MARKER_KEYS = {
    "hidden",
    "is_hidden",
    "private",
    "is_private",
    "sensitive",
    "is_sensitive",
    "internal",
    "is_internal",
    "redact",
    "redacted",
    "exclude_from_preview",
    "exclude_from_mobile",
    "not_for_display",
    "do_not_display",
}
_PRIVATE_MARKER_VALUES = {
    "hidden",
    "private",
    "sensitive",
    "secret",
    "internal",
    "system",
    "tool",
    "tool_call",
    "tool_result",
}
_TEXT_BLOCK_TYPES = {"", "text", "plain_text", "markdown"}
_TOOL_FIELDS = {
    "tool_call",
    "tool_call_id",
    "tool_calls",
    "tool_logs",
    "tool_result",
    "tool_results",
}


def _merged(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        return {}
    merged: dict = {}
    for container_key in ("query_params", "params", "body", "path_params", "query"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in input_data.items():
        if key in {"query_params", "params", "body", "path_params", "query"}:
            continue
        merged[key] = value
    return merged


def _store():
    from domain.chat.store import ChatStore
    return ChatStore()


def _summary(convo: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deliberately small, privacy-safe mobile conversation row."""
    messages = convo.get("messages")
    messages = messages if isinstance(messages, list) else []
    message_count = convo.get("message_count")
    if message_count is None:
        message_count = len(messages)

    return {
        "id": str(convo.get("id") or ""),
        "title": str(convo.get("title") or ""),
        # This is the canonical count, not the number of displayable messages.
        "message_count": _non_negative_int(message_count),
        "updated_at": _timestamp(convo.get("updated_at")),
        "created_at": _timestamp(convo.get("created_at")),
        "pinned": bool(convo.get("pinned", convo.get("is_pinned", False))),
        "revision": _non_negative_int(
            convo.get("revision", convo.get("conversation_revision", 0))
        ),
        "preview": _latest_safe_preview(messages),
    }


def _non_negative_int(value: Any) -> int:
    """Return a stable non-negative integer for mobile scalar fields."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _timestamp(value: Any) -> int:
    """Normalize owner timestamps without changing their canonical meaning."""
    return _non_negative_int(value)


def _latest_safe_preview(messages: list[Any]) -> str:
    """Return the newest ordinary user/assistant text, or an empty string."""
    for message in reversed(messages):
        if not isinstance(message, Mapping) or _message_is_private(message):
            continue
        text = _ordinary_message_text(message)
        if not text:
            continue
        return _redact_preview(text)
    return ""


def _message_is_private(message: Mapping[str, Any]) -> bool:
    """Reject internal, hidden, sensitive, and tool-bearing message records."""
    role = str(message.get("role") or "").strip().casefold()
    if role not in {"user", "assistant"}:
        return True

    if any(message.get(field) for field in _TOOL_FIELDS):
        return True

    if _contains_private_marker(message):
        return True
    metadata = message.get("metadata")
    return isinstance(metadata, Mapping) and _contains_private_marker(metadata)


def _contains_private_marker(value: Mapping[str, Any]) -> bool:
    """Recognize common privacy markers without inspecting arbitrary text."""
    for key, marker in value.items():
        normalized_key = str(key).strip().casefold().replace("-", "_")
        if normalized_key in _PRIVATE_MARKER_KEYS and _is_true_marker(marker):
            return True
        if normalized_key in {
            "visibility",
            "classification",
            "security",
            "message_type",
            "content_type",
            "kind",
            "type",
            "source",
            "audience",
        } and str(marker or "").strip().casefold().replace("-", "_") in _PRIVATE_MARKER_VALUES:
            return True
        if normalized_key in {"metadata", "privacy", "policy"} and isinstance(
            marker, Mapping
        ) and _contains_private_marker(marker):
            return True
    return False


def _is_true_marker(value: Any) -> bool:
    """Interpret only affirmative privacy marker values as enabled."""
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "hidden",
        "private",
        "sensitive",
        "secret",
        "internal",
    }


def _ordinary_message_text(message: Mapping[str, Any]) -> str:
    """Extract text only from ordinary text content blocks."""
    if "content" in message:
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                    continue
                if not isinstance(block, Mapping):
                    return ""
                block_type = str(block.get("type") or "").strip().casefold()
                if block_type not in _TEXT_BLOCK_TYPES:
                    return ""
                if _contains_private_marker(block):
                    return ""
                block_text = block.get("text", block.get("content", ""))
                if not isinstance(block_text, str):
                    return ""
                text_parts.append(block_text)
            text = " ".join(text_parts)
        elif content in (None, ""):
            text = str(message.get("raw_text") or "")
        else:
            return ""
    else:
        text = str(message.get("raw_text") or "")

    text = _normalize_preview_text(text)
    return text


def _normalize_preview_text(value: str) -> str:
    """Collapse whitespace and remove non-printing control characters."""
    cleaned = _CONTROL_RE.sub(" ", str(value or ""))
    # Zero-width characters can otherwise join a secret to a harmless-looking
    # prefix and defeat the boundary-aware redaction patterns below.
    cleaned = cleaned.replace("\u200b", " ").replace("\ufeff", " ")
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _redact_preview(value: str) -> str:
    """Redact obvious credentials before applying the display-size cap."""
    text = _normalize_preview_text(value)
    text = _SECRET_QUOTED_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}[redacted]", text
    )
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}[redacted]", text
    )
    text = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group('scheme')} [redacted]", text
    )
    text = _JWT_RE.sub("[redacted]", text)
    text = _KNOWN_TOKEN_RE.sub("[redacted]", text)
    text = _normalize_preview_text(text)
    if len(text) > MAX_PREVIEW_LENGTH:
        return text[: MAX_PREVIEW_LENGTH - 1].rstrip() + "…"
    return text


def list_conversations(input_data: Any, context: Any = None) -> dict[str, Any]:
    """List mobile-safe summaries from the canonical conversation owner."""
    del input_data, context
    try:
        store = _store()
        # Full messages are read only inside this Pack v4 boundary so the
        # privacy filter can choose the latest *ordinary* message.  _summary
        # emits an allowlisted row and never returns the message list.
        page, total = store.list_conversations(include_messages=True)
        if not isinstance(page, list):
            raise TypeError("conversation owner returned an invalid list")
        summaries = [
            _summary(conversation)
            for conversation in page
            if isinstance(conversation, Mapping)
        ]
        return ok({"conversations": summaries, "count": _non_negative_int(total)})
    except Exception:
        # Do not fall back to legacy storage or expose owner exception details.
        return error("conversation list unavailable", "CONVERSATION_LIST_FAILED")


def create_conversation(input_data, context=None):
    args = _merged(input_data)
    store = _store()
    title = str(args.get("title") or "").strip()
    convo = store.create_conversation(metadata={"title": title} if title else None)
    if title and isinstance(convo, dict):
        store.update_conversation(convo.get("id", ""), {"title": title})
        convo["title"] = title
    return ok({"conversation": convo})


def get_conversation(input_data, context=None):
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    store = _store()
    convo = store.get_conversation(convo_id)
    if convo is None:
        return error("conversation not found", "NOT_FOUND")
    return ok({"conversation": convo})


def stream_message(input_data, context=None):
    """Send a message and return the conversation for streaming.

    The actual SSE streaming is handled by the transport layer via the
    existing chat_stream_turn flow. This block prepares the message and
    returns conversation state. The transport layer's SSE handler picks
    up the stream from the flow_id.
    """
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    text = str(args.get("text") or args.get("message") or "").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    if not text:
        return error("text is required", "INVALID_INPUT")
    # The actual streaming is delegated to the existing chat stream flow.
    # This endpoint is a marker; the transport layer routes stream requests
    # to the chat_stream_turn flow.
    return ok({"conversation_id": convo_id, "text": text, "streaming": True})


def stop_run(input_data, context=None):
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    # Delegate to existing stop logic
    from domain.chat.stream_engine import ChatRunEngine
    try:
        engine = ChatRunEngine()
        engine.stop(convo_id)
    except Exception:
        pass
    return ok({"stopped": True, "conversation_id": convo_id})


def branch_conversation(input_data, context=None):
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    fork_at = str(args.get("forked_at_message_id") or args.get("fork_at") or "").strip()
    reason = str(args.get("reason") or "manual_branch").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    store = _store()
    try:
        new_convo = store.branch(convo_id, fork_at or None)
        new_id = new_convo.get("id", "") if isinstance(new_convo, dict) else str(new_convo)
        lineage = {
            "parent_conversation_id": convo_id,
            "forked_at_message_id": fork_at,
            "reason": reason,
        }
        store.update_conversation(new_id, {"lineage": lineage})
        return ok({"conversation": new_convo, "lineage": lineage})
    except Exception as exc:
        return error(str(exc), "BRANCH_FAILED")


def import_branch(input_data, context=None):
    """Import a mobile conversation as a new PC conversation branch."""
    args = _merged(input_data)
    messages = args.get("messages", [])
    title = str(args.get("title") or "Imported from mobile").strip()
    parent_authority = str(args.get("parent_authority") or "local").strip()
    if not isinstance(messages, list):
        return error("messages must be a list", "INVALID_INPUT")
    store = _store()
    try:
        convo = store.create_conversation(metadata={"title": title})
        convo_id = convo.get("id", "")
        for msg in messages:
            if isinstance(msg, dict):
                store.add_message(convo_id, {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "id": msg.get("id", ""),
                })
        lineage = {
            "parent_authority": parent_authority,
            "imported_from": "mobile",
        }
        store.update_conversation(convo_id, {"lineage": lineage})
        return ok({"conversation": store.get_conversation(convo_id), "lineage": lineage})
    except Exception as exc:
        return error(str(exc), "IMPORT_FAILED")


def run(input_data, context=None):
    args = _merged(input_data)
    action = str(args.get("action") or "").strip().lower()
    handlers = {
        "list": list_conversations,
        "create": create_conversation,
        "get": get_conversation,
        "stream": stream_message,
        "stop": stop_run,
        "branch": branch_conversation,
        "import_branch": import_branch,
    }
    handler = handlers.get(action)
    if handler is None:
        return error(f"unknown conversation action: {action}", "UNKNOWN_ACTION")
    return handler(input_data, context)
