import json
import time
from pathlib import Path


from blocks._common import error, ok
from domain.chat.message_builder import build_assistant_message
from domain.chat.store import ChatStore
from domain.kanban.chat_sync import sync_conversation_kanban


def _normalize_content(content):
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _normalize_user_message(message):
    data = dict(message) if isinstance(message, dict) else {"content": message}
    data.setdefault("role", "user")
    data["content"] = _normalize_content(data.get("content"))
    return data


def _assistant_model(route_model, ai_response):
    for candidate in (route_model, ai_response):
        if not isinstance(candidate, dict):
            continue
        value = (
            candidate.get("selected_model")
            or candidate.get("model")
            or candidate.get("model_id")
            or candidate.get("profile_id")
        )
        if value:
            return str(value)
    return "default"


def _assistant_response(ai_response):
    if isinstance(ai_response, dict):
        response = dict(ai_response)
    else:
        response = {"content": ai_response}
    response["content"] = _normalize_content(response.get("content"))
    response.setdefault("finish_reason", "stop")
    response.setdefault("usage", {})
    return response


def _persist_audit_jsonl(data):
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    user_data_dir_raw = str(workspace.get("user_data_dir") or "").strip()
    if not user_data_dir_raw:
        return None
    user_data_dir = Path(user_data_dir_raw)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    event_path = user_data_dir / "chat_turns.jsonl"
    record = {
        "ts": int(time.time()),
        "conversation_id": data.get("conversation_id"),
        "message": data.get("message"),
        "ai_response": data.get("ai_response"),
        "route_model": data.get("route_model"),
    }
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(event_path)


def _existing_or_add_user_message(store, conversation_id, message):
    message_id = message.get("id")
    if message_id:
        existing = store.get_message(conversation_id, str(message_id))
        if existing is not None:
            return existing
    return store.add_message(conversation_id, message)


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    store = ChatStore()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return error("Conversation not found", "NOT_FOUND")

    user_message = _existing_or_add_user_message(
        store,
        conversation_id,
        _normalize_user_message(data.get("message")),
    )
    if user_message is None:
        return error("Failed to persist user message", "INTERNAL_ERROR")

    ai_response = _assistant_response(data.get("ai_response"))
    route_model = data.get("route_model") if isinstance(data.get("route_model"), dict) else {}
    assistant_message = build_assistant_message(
        conversation_id=conversation_id,
        parent_id=user_message.get("id"),
        sequence_number=int(user_message.get("sequence_number", 1) or 1) + 1,
        response=ai_response,
        model=_assistant_model(route_model, ai_response),
    )
    assistant_message = store.add_message(conversation_id, assistant_message)
    if assistant_message is None:
        return error("Failed to persist assistant message", "INTERNAL_ERROR")
    sync_conversation_kanban(conversation_id, reason="turn_persisted")

    audit_path = _persist_audit_jsonl(data)
    return ok(
        {
            "persisted": True,
            "conversation_id": conversation_id,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "audit_log_path": audit_path,
        }
    )
