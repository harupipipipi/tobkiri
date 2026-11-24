from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore
from domain.kanban.chat_sync import sync_conversation_kanban


def run(input_data, context):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    message_id = input_data.get("message_id")
    if not message_id:
        return error("message_id is required", "INVALID_INPUT")
    result = store.delete_message(conversation_id, message_id)
    if not result:
        return error("Message not found", "NOT_FOUND")
    sync_conversation_kanban(conversation_id, reason="message_deleted")
    return ok({"success": True})
