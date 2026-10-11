

from blocks._common import error, ok
from domain.share.audit import record_share_event
from domain.share.store import ShareStore


def run(input_data, context=None):
    token = str((input_data or {}).get("token") or "").strip()
    if not token:
        return error("'token' is required", code="INVALID_INPUT")
    store = ShareStore()
    record = store.get(token)
    if record is None:
        return error("Share link is missing, expired, or revoked", code="NOT_FOUND")
    if record.get("target_type") != "conversation":
        return error("Share does not contain a conversation", code="INVALID_INPUT")
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    event = record_share_event(
        "export", target_id=record.get("target_id"), mode="redacted_history_json",
        message_count=ShareStore._message_count(content),
    )
    store.append_audit(token, event)
    return ok({"conversation": content.get("conversation"), "audit": event})
