from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore
from domain.chat.cancellation import get_chat_cancellation_registry


def _is_streaming_assistant(message):
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    thinking = metadata.get("thinking") if isinstance(metadata.get("thinking"), dict) else {}
    state = str(thinking.get("state") or "").lower()
    finish_reason = str(message.get("finish_reason") or "").lower()
    return (
        finish_reason == "streaming"
        or metadata.get("streaming") is True
        or metadata.get("draft") is True
        or state in {"streaming", "running"}
    )


def _mark_latest_streaming_assistant_cancelled(store, conversation_id):
    conv = store.get_conversation(conversation_id)
    if not conv:
        return False
    for message in reversed(conv.get("messages") or []):
        if not _is_streaming_assistant(message):
            continue
        metadata = dict(message.get("metadata") or {})
        metadata.pop("streaming", None)
        metadata.pop("draft", None)
        thinking = dict(metadata.get("thinking") or {})
        thinking["state"] = "cancelled"
        metadata["thinking"] = thinking
        metadata["cancelled"] = True
        raw_text = message.get("raw_text") if isinstance(message.get("raw_text"), str) else ""
        content = message.get("content") if isinstance(message.get("content"), list) else []
        if not raw_text.strip():
            raw_text = "停止しました。"
            content = [{"type": "text", "text": raw_text}]
        updated = store.update_message(
            conversation_id,
            message.get("id"),
            {
                "content": content,
                "raw_text": raw_text,
                "finish_reason": "cancelled",
                "usage": message.get("usage") or {},
                "metadata": metadata,
                "events": message.get("events") or [],
                "tool_logs": message.get("tool_logs") or [],
                "model": message.get("model"),
            },
        )
        return updated is not None
    return False


def run(input_data, context):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return error("Conversation not found", "NOT_FOUND")
    cancellation_registry = get_chat_cancellation_registry()
    if cancellation_registry.has_active_callbacks(conversation_id):
        cancellation_registry.request_cancel(conversation_id)
    persisted_cancelled = _mark_latest_streaming_assistant_cancelled(store, conversation_id)
    stream_id = input_data.get("stream_id")
    call_handler = context.get("call_handler") if context else None
    if call_handler is not None and stream_id:
        try:
            call_handler("defaults.ai.stop", {"stream_id": stream_id})
        except Exception:
            pass
    return ok({"success": True, "conversation_id": conversation_id, "cancelled": True, "persisted_cancelled": persisted_cancelled})
