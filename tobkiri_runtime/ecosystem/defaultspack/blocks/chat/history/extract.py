"""
blocks/chat/history/extract.py - Extract messages to a new conversation.

Copies specified messages from a source conversation into a new (or existing)
conversation, optionally removing them from the source.

Input:
    conversation_id: str (required) - source conversation
    message_ids: list[str] (required) - message IDs to extract
    target_conversation_id: str (optional) - existing target conversation;
                            if omitted, a new conversation is created
    remove_from_source: bool (optional, default true) - whether to delete
                        extracted messages from the source

Returns:
    source_conversation, target_conversation, extracted_count,
    extracted_message_ids, missing_message_ids, target_conversation_id
"""

from blocks._common import ok, error

from domain.chat.store import ChatStore
from domain.chat.history_editor import extract_messages


def run(input_data, context):
    store = ChatStore()

    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    message_ids = input_data.get("message_ids")
    if not message_ids or not isinstance(message_ids, list):
        return error("message_ids (list) is required", "INVALID_INPUT")

    if len(message_ids) == 0:
        return error("message_ids must not be empty", "INVALID_INPUT")

    target_conversation_id = input_data.get("target_conversation_id")
    remove_from_source = input_data.get("remove_from_source", True)

    result, err = extract_messages(
        store, conversation_id, message_ids,
        target_conversation_id=target_conversation_id,
        remove_from_source=remove_from_source,
    )
    if err is not None:
        code = "NOT_FOUND" if "not found" in err.lower() else "INVALID_INPUT"
        return error(err, code)

    return ok(result)
