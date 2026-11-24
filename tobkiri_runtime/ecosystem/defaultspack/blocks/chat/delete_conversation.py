from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore


def run(input_data, context):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    result = store.delete_conversation(conversation_id)
    if not result:
        return error("Conversation not found", "NOT_FOUND")
    return ok({"success": True})
