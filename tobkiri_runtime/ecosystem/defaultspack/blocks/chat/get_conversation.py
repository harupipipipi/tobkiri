import os
import json
from pathlib import Path
from blocks._common import ok, error, gen_id, timestamp

from domain.chat.public_metadata import (
    DEFAULT_CONVERSATION_MESSAGE_LIMIT,
    DEFAULT_MESSAGE_TEXT_LIMIT,
    MAX_CONVERSATION_MESSAGE_LIMIT,
    MAX_MESSAGE_TEXT_LIMIT,
    compact_conversation_for_response,
)
from domain.chat.store import ChatStore

_MIMO_CODING_COMPANY_ID = "mimo-coding-company"
_MIMO_CODING_COMPANY_PROFILE_ID = "defaultspack.mimo_coding_company"


def _read_json_object(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _mimo_state_path_candidates():
    override = os.environ.get("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", "").strip()
    if override:
        yield Path(override)
    ecosystem_root = Path(__file__).resolve().parents[3]
    yield (
        ecosystem_root
        / "rumi_operations_company_pack"
        / "user_data"
        / "shared"
        / "mimo_coding_company"
        / "state.json"
    )
    yield (
        ecosystem_root
        / "defaultspack"
        / "user_data"
        / "shared"
        / "mimo_coding_company"
        / "state.json"
    )


def _active_mimo_conversation_id():
    for path in _mimo_state_path_candidates():
        state = _read_json_object(path)
        conversation_id = str(state.get("conversation_id") or "").strip()
        if conversation_id:
            return conversation_id
    return ""


def _is_mimo_company_chat(conversation):
    metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    kind = str(conversation.get("conversation_kind") or "").strip()
    if kind == "mimo_coding_company":
        return True
    if kind == "mimo_coding_company_loop":
        return False
    tags = conversation.get("tags") if isinstance(conversation.get("tags"), list) else []
    tag_values = {str(tag or "").strip() for tag in tags}
    title = str(conversation.get("title") or "").strip().lower()
    return (
        title.startswith("[stale]")
        and (
            str(metadata.get("profile_id") or "") == _MIMO_CODING_COMPANY_PROFILE_ID
            or str(metadata.get("company_id") or "") == _MIMO_CODING_COMPANY_ID
            or _MIMO_CODING_COMPANY_PROFILE_ID in tag_values
            or _MIMO_CODING_COMPANY_ID in tag_values
        )
    )


def _with_mimo_stale_redirect_metadata(conversation, requested_id):
    active_conversation_id = _active_mimo_conversation_id()
    if not active_conversation_id or active_conversation_id == str(requested_id or "").strip():
        return conversation
    if not _is_mimo_company_chat(conversation):
        return conversation

    metadata = dict(conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {})
    if str(metadata.get("active_conversation_id") or "").strip() == active_conversation_id and metadata.get("superseded") is True:
        return conversation
    updated = dict(conversation)
    updated["metadata"] = {
        **metadata,
        "profile_id": metadata.get("profile_id") or _MIMO_CODING_COMPANY_PROFILE_ID,
        "company_id": metadata.get("company_id") or _MIMO_CODING_COMPANY_ID,
        "superseded": True,
        "superseded_reason": metadata.get("superseded_reason") or "mimo_coding_company_inactive_chat",
        "active_conversation_id": active_conversation_id,
        "replacement_conversation_id": active_conversation_id,
        "stale_notice": (
            "This MiMo Coding Company conversation is no longer the active harness chat. "
            f"Use active conversation {active_conversation_id}."
        ),
    }
    return updated


def _merged_input(input_data):
    merged = {}
    for container_key in ("query_params", "params", "body"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    if isinstance(input_data.get("query"), dict):
        merged.update(input_data["query"])
    if isinstance(input_data.get("_query_params"), dict):
        merged.update(input_data["_query_params"])
    for key, value in input_data.items():
        if key in {"query_params", "params", "body", "_query_params"}:
            continue
        if key == "query" and isinstance(value, dict):
            continue
        merged[key] = value
    return merged


def _optional_bool(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_present(params, *keys):
    for key in keys:
        if key in params:
            return params.get(key)
    return None


def _resolve_message_window(params):
    include_messages = _optional_bool(params.get("include_messages"))
    if include_messages is False:
        return 0, 0
    if _optional_bool(
        _first_present(params, "include_full_messages", "full_messages", "include_all_messages")
    ):
        return None, None
    raw_limit = _first_present(params, "message_limit", "messages_limit")
    if isinstance(raw_limit, str) and raw_limit.strip().lower() in {"all", "full"}:
        return None, None
    limit = _optional_int(raw_limit)
    if limit is None:
        limit = DEFAULT_CONVERSATION_MESSAGE_LIMIT
    limit = max(0, min(MAX_CONVERSATION_MESSAGE_LIMIT, limit))
    offset = _optional_int(_first_present(params, "message_offset", "messages_offset"))
    if offset is not None:
        offset = max(0, offset)
    return limit, offset


def _resolve_message_text_limit(params):
    raw_limit = _first_present(params, "message_text_limit", "messages_text_limit")
    limit = _optional_int(raw_limit)
    if limit is None:
        return DEFAULT_MESSAGE_TEXT_LIMIT
    return max(0, min(MAX_MESSAGE_TEXT_LIMIT, limit))


def run(input_data, context):
    store = ChatStore()
    params = _merged_input(input_data or {})
    conversation_id = params.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    message_limit, message_offset = _resolve_message_window(params)
    conv, messages_window = store.get_conversation_window(
        conversation_id,
        message_limit=message_limit,
        message_offset=message_offset,
    )
    if conv is None:
        return error("Conversation not found", "NOT_FOUND")
    conv = _with_mimo_stale_redirect_metadata(conv, conversation_id)
    return ok(
        compact_conversation_for_response(
            conv,
            messages_window=messages_window,
            message_text_limit=_resolve_message_text_limit(params),
        )
    )
