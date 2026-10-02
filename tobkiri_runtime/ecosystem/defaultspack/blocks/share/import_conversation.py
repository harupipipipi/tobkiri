

from blocks._common import error, ok
from domain.share.conversation_bundle import import_shared_conversation
from domain.share.audit import record_share_event
from domain.share.store import ShareStore


def run(input_data, context=None):
    token = str((input_data or {}).get("token") or "").strip()
    if not token:
        return error("'token' is required", code="INVALID_INPUT")
    share_store = ShareStore()
    record = share_store.get(token)
    if record is None:
        return error("Share link is missing, expired, or revoked", code="NOT_FOUND")
    if record.get("target_type") != "conversation":
        return error("Share does not contain a conversation", code="INVALID_INPUT")
    try:
        import_mode = str((input_data or {}).get("import_mode") or "continue_copy")
        conversation = import_shared_conversation(
            record.get("content") or {}, source_url=str((input_data or {}).get("source_url") or record.get("share_url") or ""),
            import_mode=import_mode,
        )
    except PermissionError as exc:
        return error(str(exc), code="PERMISSION_DENIED")
    except (TypeError, ValueError) as exc:
        return error(str(exc), code="INVALID_INPUT")
    event = record_share_event(
        "import", target_id=record.get("target_id"), mode=import_mode,
        message_count=len(conversation.get("messages") or []),
    )
    share_store.append_audit(token, event)
    return ok({"conversation": conversation, "conversation_id": conversation["id"], "import_mode": import_mode, "audit": event})
