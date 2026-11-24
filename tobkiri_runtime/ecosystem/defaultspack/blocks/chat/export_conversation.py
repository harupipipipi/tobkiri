from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore
from domain.share.audit import record_share_event


def run(input_data, context):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    fmt = str(input_data.get("format", "markdown")).strip().lower()
    if fmt not in ("markdown", "json"):
        return error("format must be 'markdown' or 'json'", "INVALID_INPUT")
    content = store.export_conversation(conversation_id, fmt=fmt)
    if content is None:
        return error("Conversation not found", "NOT_FOUND")
    audit = record_share_event("export", target_id=conversation_id, mode=str(fmt))
    return ok({
        "conversation_id": conversation_id,
        "format": fmt,
        "content": content,
        "audit": audit,
    })
