

from blocks._common import error, ok
from domain.chat.steer import ConversationSteerStore


def run(input_data, context=None):
    payload = input_data if isinstance(input_data, dict) else {}
    action = str(payload.get("action") or "enqueue").strip().lower()
    store = ConversationSteerStore()
    try:
        if action in {"enqueue", "create", "queue"}:
            return ok(store.enqueue(payload))
        if action == "list":
            return ok({"items": store.list(status=payload.get("status"), target_id=payload.get("target_id") or payload.get("conversation_id"))})
        if action == "cancel":
            item_id = str(payload.get("id") or payload.get("steer_id") or "").strip()
            if not item_id:
                return error("id is required", "INVALID_INPUT")
            item = store.cancel(item_id)
            return ok({"cancelled": item is not None, "item": item})
        if action == "process":
            return ok({"processed": store.process(
                target_type=str(payload.get("target_type") or "conversation"),
                target_id=str(payload.get("target_id") or payload.get("conversation_id") or ""),
                conversation_id=str(payload.get("conversation_id") or ""),
                context=context or {},
            )})
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
    return error("unsupported action", "INVALID_INPUT")
