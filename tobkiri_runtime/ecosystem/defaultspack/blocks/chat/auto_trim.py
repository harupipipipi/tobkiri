import json
from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore
from blocks.chat.send import _ai_direct_complete
from blocks.chat._prompt_helpers import (
    build_analysis_prompt,
    build_text_from_content,
    extract_text,
)


def _build_text_from_content(content):
    """Backwards-compatible alias for the shared build_text_from_content helper."""
    return build_text_from_content(content)


def _build_analysis_prompt(messages_with_ids, max_context_tokens=None):
    """AI にトリム対象を判断させるプロンプトを構築する。"""
    return build_analysis_prompt(
        messages_with_ids,
        max_context_tokens=max_context_tokens,
        persona="trim_analyst",
        truncate_at=200,
    )


def _parse_trim_plan(response_text):
    """AI 応答の JSON をパースして trim_plan segments を返す。"""
    text = response_text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    json_str = text[start:end + 1]
    try:
        segments = json.loads(json_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(segments, list):
        return []
    valid_segments = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if "start_id" not in seg or "end_id" not in seg:
            continue
        valid_segments.append({
            "start_id": str(seg["start_id"]),
            "end_id": str(seg["end_id"]),
            "reason": str(seg.get("reason", "")),
            "summary_preview": str(seg.get("summary_preview", "")),
        })
    return valid_segments


def _extract_text_from_response(response):
    """Backwards-compatible alias for the shared extract_text helper."""
    return extract_text(response)


def _unavailable_analysis_response():
    """Structured error used by legacy callers if AI analysis is unavailable."""
    return {
        "success": False,
        "error": "AI analysis is unavailable",
        "error_type": "not_implemented",
    }


def run(input_data, context):
    store = ChatStore()

    # --- バリデーション ---
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    conv = store.get_conversation(conversation_id)
    if conv is None:
        return error("Conversation not found", "NOT_FOUND")

    messages = conv.get("messages", [])
    if not messages:
        return ok({"trim_plan": {"segments": []}, "message": "No messages to analyze"})

    # --- メッセージ ID 付きリストを元の messages から直接構築 ---
    # convert_to_standard は 1:N 変換するため使わない（インデックスずれ防止）
    messages_with_ids = []
    for msg in messages:
        msg_id = msg.get("id") or gen_id()
        role = msg.get("role", "unknown")
        content_text = _build_text_from_content(msg.get("content", ""))
        messages_with_ids.append({
            "id": msg_id,
            "role": role,
            "content": content_text,
        })

    # --- AI に分析させる ---
    model = input_data.get("model", "default")
    if model == "default":
        model = conv.get("model", "stub/default")
    max_context_tokens = input_data.get("max_context_tokens")
    analysis_messages = _build_analysis_prompt(messages_with_ids, max_context_tokens)

    call_handler = context.get("call_handler") if context else None
    if call_handler is not None:
        try:
            ai_params = {
                "model": model,
                "messages": analysis_messages,
                "tools": [],
                "params": {},
            }
            response = call_handler("defaults.ai.complete", ai_params)
        except Exception as exc:
            return error("AI request failed: " + str(exc), "AI_ERROR")
        if isinstance(response, dict) and response.get("status") == "error":
            err = response.get("error", {})
            message = err.get("message") if isinstance(err, dict) else None
            return error(str(message or "AI request failed"), "AI_ERROR")
        if isinstance(response, dict) and response.get("status") == "ok":
            response = response.get("data", {})
    else:
        response, ai_error = _ai_direct_complete(model, analysis_messages, [], {})
        if ai_error is not None:
            return error(ai_error, "AI_ERROR")
    if not isinstance(response, dict):
        return error("AI provider returned an invalid response", "AI_ERROR")

    response_text = _extract_text_from_response(response)
    segments = _parse_trim_plan(response_text)

    # --- segments のバリデーション（存在するメッセージIDか確認） ---
    msg_id_set = {m["id"] for m in messages}
    validated_segments = []
    for seg in segments:
        if seg["start_id"] in msg_id_set and seg["end_id"] in msg_id_set:
            validated_segments.append(seg)

    return ok({
        "trim_plan": {
            "segments": validated_segments,
        },
        "conversation_id": conversation_id,
        "total_messages": len(messages),
    })
