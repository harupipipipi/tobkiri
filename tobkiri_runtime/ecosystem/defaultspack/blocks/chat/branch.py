from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore


def run(input_data, context):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    message_id = input_data.get("message_id")
    if not message_id:
        return error("message_id is required", "INVALID_INPUT")
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return error("Conversation not found", "NOT_FOUND")
    msg = store.get_message(conversation_id, message_id)
    if msg is None:
        return error("Message not found", "NOT_FOUND")
    new_conv = store.branch(conversation_id, message_id)
    if new_conv is None:
        return error("Failed to create branch", "INTERNAL_ERROR")
    return ok(new_conv)
