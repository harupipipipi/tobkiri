

from blocks._common import error, ok
from domain.chat.store import ChatStore


def run(input_data, context=None):
    payload = input_data if isinstance(input_data, dict) else {}
    prompt = str(payload.get("prompt") or payload.get("message") or "").strip()
    store = ChatStore()
    conv = store.create_conversation(
        model=str(payload.get("model") or "") or None,
        system_prompt_id=str(payload.get("system_prompt_id") or "") or None,
        agent_id=str(payload.get("agent_id") or "") or None,
        parent_conversation_id=str(payload.get("parent_conversation_id") or payload.get("conversation_id") or "") or None,
        conversation_kind="handoff",
        tags=["handoff"],
        metadata={
            "handoff": True,
            "source_conversation_id": str(payload.get("conversation_id") or ""),
            "external_provider": str(payload.get("external_provider") or ""),
        },
    )
    send_result = None
    if prompt and payload.get("send", True) is not False:
        from blocks.chat.send import run as send_chat

        send_result = send_chat(
            {
                "conversation_id": conv["id"],
                "message": {
                    "role": "user",
                    "content": prompt,
                    "metadata": {"source": "conversation_handoff"},
                },
            },
            {"run_source": "conversation_handoff", "_conversation_handoff_initial": True, **(context or {})},
        )
    deep_link = "rumi://conversation/{}".format(conv["id"])
    path = "?chat={}".format(conv["id"])
    return ok({
        "conversation": conv,
        "conversation_id": conv["id"],
        "deep_link": deep_link,
        "url_path": path,
        "send_result": send_result,
        "external_reply": {
            "text": "新しい会話へ移動しました: {}".format(path),
            "deep_link": deep_link,
            "handoff_token": conv["id"],
        },
        "widget": {
            "kind": "conversation_handoff",
            "title": "移動先",
            "conversation_id": conv["id"],
            "deep_link": deep_link,
            "url_path": path,
            "model": conv.get("model"),
        },
    })
