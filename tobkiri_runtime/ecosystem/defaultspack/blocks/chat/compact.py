from blocks._common import ok, error

from blocks.chat._compact_helpers import (
    compact_selected_segment,
    normalize_protect_last_messages,
    select_oldest_safe_segment,
)
from domain.chat.store import ChatStore


def run(input_data, context):
    store = ChatStore()
    input_data = input_data or {}

    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return error("Conversation not found", "NOT_FOUND")

    protect_last_messages = normalize_protect_last_messages(input_data)
    segment, err = select_oldest_safe_segment(
        conversation.get("messages", []),
        protect_last_messages,
        start_message_id=input_data.get("start_message_id"),
        end_message_id=input_data.get("end_message_id"),
    )
    if err is not None:
        return ok({
            "conversation": conversation,
            "summary_message": None,
            "deleted_message_ids": [],
            "deleted_count": 0,
            "protect_last_messages": protect_last_messages,
            "message": err,
        })

    result, err = compact_selected_segment(
        store,
        conversation_id,
        conversation,
        segment,
        context or {},
        input_data,
        protect_last_messages,
    )
    if err is not None:
        return error(err, "INTERNAL_ERROR")
    return ok(result)
