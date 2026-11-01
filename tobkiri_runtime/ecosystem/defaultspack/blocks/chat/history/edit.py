"""
blocks/chat/history/edit.py - Range delete endpoint.

Deletes all messages in the specified range [start_message_id, end_message_id].
Can be called as an AI tool to clean up experiment logs, debug output, etc.

Input:
    conversation_id: str (required)
    start_message_id: str (required)
    end_message_id: str (required)

Returns:
    deleted_count, deleted_message_ids, conversation
"""

from blocks._common import ok, error

from domain.chat.store import ChatStore
from domain.chat.history_editor import delete_range


def run(input_data, context):
    store = ChatStore()

    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    start_message_id = input_data.get("start_message_id")
    if not start_message_id:
        return error("start_message_id is required", "INVALID_INPUT")

    end_message_id = input_data.get("end_message_id")
    if not end_message_id:
        return error("end_message_id is required", "INVALID_INPUT")

    result, err = delete_range(
        store, conversation_id, start_message_id, end_message_id
    )
    if err is not None:
        code = "NOT_FOUND" if "not found" in err.lower() else "INVALID_RANGE"
        return error(err, code)

    return ok(result)
