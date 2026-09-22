"""blocks.mobile.conversations — モバイル向け会話facade.

PC上の会話をスマホから操作するための安定したAPI。
既存の ChatStore / ChatRunEngine への薄いラッパー。

ルート:
  GET    /api/mobile/v1/conversations           → list
  POST   /api/mobile/v1/conversations           → create
  GET    /api/mobile/v1/conversations/{id}      → get
  POST   /api/mobile/v1/conversations/{id}/stream → send + SSE stream
  POST   /api/mobile/v1/conversations/{id}/stop   → stop run
  POST   /api/mobile/v1/conversations/{id}/branch → branch
  POST   /api/mobile/v1/conversations/import-branch → import from mobile
"""

from __future__ import annotations



from blocks._common import error, ok


def _merged(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        return {}
    merged: dict = {}
    for container_key in ("query_params", "params", "body", "path_params", "query"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in input_data.items():
        if key in {"query_params", "params", "body", "path_params", "query"}:
            continue
        merged[key] = value
    return merged


def _store():
    from domain.chat.store import ChatStore
    return ChatStore()


def _summary(convo: dict) -> dict:
    messages = convo.get("messages", [])
    return {
        "id": convo.get("id", ""),
        "title": convo.get("title", ""),
        "message_count": len(messages),
        "updated_at": convo.get("updated_at", ""),
        "created_at": convo.get("created_at", ""),
        "pinned": convo.get("pinned", False),
        "revision": convo.get("revision", 0),
        "preview": convo.get("preview", ""),
    }


def list_conversations(input_data, context=None):
    del input_data, context
    store = _store()
    page, total = store.list_conversations(include_messages=False)
    return ok({"conversations": page, "count": total})


def create_conversation(input_data, context=None):
    args = _merged(input_data)
    store = _store()
    title = str(args.get("title") or "").strip()
    convo = store.create_conversation(metadata={"title": title} if title else None)
    if title and isinstance(convo, dict):
        store.update_conversation(convo.get("id", ""), {"title": title})
        convo["title"] = title
    return ok({"conversation": convo})


def get_conversation(input_data, context=None):
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    store = _store()
    convo = store.get_conversation(convo_id)
    if convo is None:
        return error("conversation not found", "NOT_FOUND")
    return ok({"conversation": convo})


def stream_message(input_data, context=None):
    """Send a message and return the conversation for streaming.

    The actual SSE streaming is handled by the transport layer via the
    existing chat_stream_turn flow. This block prepares the message and
    returns conversation state. The transport layer's SSE handler picks
    up the stream from the flow_id.
    """
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    text = str(args.get("text") or args.get("message") or "").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    if not text:
        return error("text is required", "INVALID_INPUT")
    # The actual streaming is delegated to the existing chat stream flow.
    # This endpoint is a marker; the transport layer routes stream requests
    # to the chat_stream_turn flow.
    return ok({"conversation_id": convo_id, "text": text, "streaming": True})


def stop_run(input_data, context=None):
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    # Delegate to existing stop logic
    from domain.chat.stream_engine import ChatRunEngine
    try:
        engine = ChatRunEngine()
        engine.stop(convo_id)
    except Exception:
        pass
    return ok({"stopped": True, "conversation_id": convo_id})


def branch_conversation(input_data, context=None):
    args = _merged(input_data)
    convo_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    fork_at = str(args.get("forked_at_message_id") or args.get("fork_at") or "").strip()
    reason = str(args.get("reason") or "manual_branch").strip()
    if not convo_id:
        return error("conversation_id is required", "INVALID_INPUT")
    store = _store()
    try:
        new_convo = store.branch(convo_id, fork_at or None)
        new_id = new_convo.get("id", "") if isinstance(new_convo, dict) else str(new_convo)
        lineage = {
            "parent_conversation_id": convo_id,
            "forked_at_message_id": fork_at,
            "reason": reason,
        }
        store.update_conversation(new_id, {"lineage": lineage})
        return ok({"conversation": new_convo, "lineage": lineage})
    except Exception as exc:
        return error(str(exc), "BRANCH_FAILED")


def import_branch(input_data, context=None):
    """Import a mobile conversation as a new PC conversation branch."""
    args = _merged(input_data)
    messages = args.get("messages", [])
    title = str(args.get("title") or "Imported from mobile").strip()
    parent_authority = str(args.get("parent_authority") or "local").strip()
    if not isinstance(messages, list):
        return error("messages must be a list", "INVALID_INPUT")
    store = _store()
    try:
        convo = store.create_conversation(metadata={"title": title})
        convo_id = convo.get("id", "")
        for msg in messages:
            if isinstance(msg, dict):
                store.add_message(convo_id, {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "id": msg.get("id", ""),
                })
        lineage = {
            "parent_authority": parent_authority,
            "imported_from": "mobile",
        }
        store.update_conversation(convo_id, {"lineage": lineage})
        return ok({"conversation": store.get_conversation(convo_id), "lineage": lineage})
    except Exception as exc:
        return error(str(exc), "IMPORT_FAILED")


def run(input_data, context=None):
    args = _merged(input_data)
    action = str(args.get("action") or "").strip().lower()
    handlers = {
        "list": list_conversations,
        "create": create_conversation,
        "get": get_conversation,
        "stream": stream_message,
        "stop": stop_run,
        "branch": branch_conversation,
        "import_branch": import_branch,
    }
    handler = handlers.get(action)
    if handler is None:
        return error(f"unknown conversation action: {action}", "UNKNOWN_ACTION")
    return handler(input_data, context)
