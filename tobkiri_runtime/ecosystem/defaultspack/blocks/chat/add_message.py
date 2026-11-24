from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore
from domain.kanban.chat_sync import sync_conversation_kanban


def run(input_data, context):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return error("Conversation not found", "NOT_FOUND")
    metadata = conv.get("metadata") if isinstance(conv.get("metadata"), dict) else {}
    if metadata.get("shared_read_only") is True:
        return error("This imported conversation is read-only", "PERMISSION_DENIED")
    message = input_data.get("message")
    if not message or not isinstance(message, dict):
        return error("message dict is required", "INVALID_INPUT")
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
        message["content"] = content
    msg = store.add_message(conversation_id, message)
    if msg is None:
        return error("Failed to add message", "INTERNAL_ERROR")
    sync_conversation_kanban(conversation_id, reason="message_added")
    return ok(msg)
