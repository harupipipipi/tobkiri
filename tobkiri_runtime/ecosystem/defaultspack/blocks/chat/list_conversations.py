from blocks._common import ok, error, gen_id, timestamp

from domain.chat.public_metadata import compact_conversation_for_response
from domain.chat.store import ChatStore


def _merged_input(input_data):
    merged = {}
    for container_key in ("query_params", "params", "body"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    if isinstance(input_data.get("query"), dict):
        merged.update(input_data["query"])
    for key, value in input_data.items():
        if key in {"query_params", "params", "body"}:
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


def _bool_with_default(value, default=False):
    parsed = _optional_bool(value)
    return default if parsed is None else parsed


def _int_with_default(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _blank_to_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def run(input_data, context):
    store = ChatStore()
    params = _merged_input(input_data or {})
    limit = max(0, _int_with_default(params.get("limit"), 50))
    offset = max(0, _int_with_default(params.get("offset"), 0))
    conversations, total = store.list_conversations(
        limit=limit,
        offset=offset,
        tag=_blank_to_none(params.get("tag")),
        tags=params.get("tags"),
        is_starred=_optional_bool(params.get("is_starred")),
        is_pinned=_optional_bool(params.get("is_pinned")),
        is_archived=_optional_bool(params.get("is_archived")),
        company_id=_blank_to_none(params.get("company_id")),
        workspace_id=_blank_to_none(params.get("workspace_id")),
        conversation_kind=_blank_to_none(params.get("conversation_kind")),
        group_id=_blank_to_none(params.get("group_id")),
        query=_blank_to_none(params.get("query")),
        include_messages=_bool_with_default(params.get("include_messages"), False),
    )
    if _bool_with_default(params.get("include_messages"), False):
        conversations = [compact_conversation_for_response(conversation) for conversation in conversations]
    return ok({"conversations": conversations, "total": total})
