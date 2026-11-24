from blocks._common import ok, error

from blocks.chat._compact_helpers import (
    approved_for_apply,
    compact_selected_segment,
    normalize_protect_last_messages,
    plan_payload,
    select_auto_segment,
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
    segment, plan_message = select_auto_segment(conversation.get("messages", []), protect_last_messages)

    mode = str(input_data.get("mode") or "suggest").strip().lower()
    if mode in {"", "suggest"}:
        return ok(plan_payload(conversation_id, conversation, segment, protect_last_messages, plan_message))

    if mode != "apply":
        return error("mode must be 'suggest' or 'apply'", "INVALID_INPUT")

    if not approved_for_apply(input_data):
        return error("approved=True or approval_token is required for apply mode", "APPROVAL_REQUIRED")

    if segment is None:
        return ok({
            **plan_payload(conversation_id, conversation, None, protect_last_messages, plan_message),
            "conversation": conversation,
            "summary_message": None,
            "deleted_message_ids": [],
            "deleted_count": 0,
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
    return ok({
        **result,
        "mode": "apply",
        "trim_plan": {"segments": [segment]},
    })
