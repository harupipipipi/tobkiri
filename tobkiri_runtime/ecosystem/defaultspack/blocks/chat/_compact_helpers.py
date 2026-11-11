import time

from domain.chat.history_editor import (
    identify_compactable_segments,
    replace_range_with_message,
)
from domain.chat.message_converter import convert_to_standard
from blocks.chat._prompt_helpers import (
    build_summarize_prompt,
    extract_text as _shared_extract_text,
)


def now_ms():
    return int(time.time() * 1000)


def int_with_default(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def optional_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def approved_for_apply(input_data, context=None):
    """Verify and consume existing server approval for these exact apply args."""
    from blocks.coding._approval import is_server_approved

    return is_server_approved(context, "chat.auto_compact", input_data)


def request_apply_approval(input_data):
    """Create a normal request for explicit API replay, without a fake tool ID."""
    from urllib.parse import quote
    from domain.safety import approval

    arguments = dict(input_data)
    for key in (
        "approval_token", "approved_token", "approved", "_headers", "_method",
        "_raw_body", "_raw_body_base64",
    ):
        arguments.pop(key, None)
    conversation_id = str(arguments["conversation_id"])
    request = approval.create_approval_request(
        "chat.auto_compact", "high", arguments,
        details={
            "action": "chat.auto_compact",
            "arguments": arguments,
            "conversation_id": conversation_id,
            "http_method": "POST",
            "http_path": "/api/chat/conversations/"
            + quote(conversation_id, safe="") + "/auto-compact",
        },
    )
    return {
        "approval_required": True,
        "operation": "chat.auto_compact",
        "approval_request_id": request["request_id"],
        "args_hash": request["args_hash"],
        "expires_at": request["expires_at"],
        "display_summary": request["display_summary"],
    }


def normalize_protect_last_messages(input_data, default=12):
    value = int_with_default(input_data.get("protect_last_messages"), default)
    return max(0, value)


def extract_text(response):
    return _shared_extract_text(response)


def build_summary_prompt(standard_messages, instruction=None):
    return build_summarize_prompt(
        standard_messages,
        instruction,
        persona="compactor",
        user_prefix="Compact this conversation range:",
    )


def call_summary_ai(context, model, range_messages, instruction=None):
    standard_messages = convert_to_standard(range_messages)
    call_handler = context.get("call_handler") if context else None
    if call_handler is not None:
        try:
            response = call_handler(
                "defaults.ai.complete",
                {
                    "model": model,
                    "messages": build_summary_prompt(standard_messages, instruction),
                    "tools": [],
                    "params": {},
                },
            )
            text = extract_text(response).strip()
            if text:
                return text
        except Exception as exc:
            print("[chat.compact] AI call failed: " + str(exc))
    return "[Summary of " + str(len(range_messages)) + " messages]"


def protected_start_index(messages, protect_last_messages):
    if protect_last_messages <= 0:
        return len(messages)
    return max(len(messages) - protect_last_messages, 0)


def message_index(messages):
    return {str(msg.get("id") or ""): idx for idx, msg in enumerate(messages)}


def segment_for_range(messages, start_idx, end_idx, reason="oldest_safe_range", summary_preview=""):
    range_messages = messages[start_idx:end_idx + 1]
    original_ids = [msg.get("id") for msg in range_messages]
    return {
        "start_id": range_messages[0].get("id"),
        "end_id": range_messages[-1].get("id"),
        "start_index": start_idx,
        "end_index": end_idx,
        "reason": reason,
        "summary_preview": summary_preview,
        "message_count": len(range_messages),
        "original_message_ids": original_ids,
    }


def select_oldest_safe_segment(messages, protect_last_messages, start_message_id=None, end_message_id=None):
    if len(messages) < 2:
        return None, "At least 2 messages are required for compaction"

    protected_start = protected_start_index(messages, protect_last_messages)
    indexes = message_index(messages)
    if start_message_id or end_message_id:
        start_id = str(start_message_id or "")
        end_id = str(end_message_id or "")
        if start_id not in indexes or end_id not in indexes:
            return None, "Invalid message range: start or end message not found"
        start_idx = indexes[start_id]
        end_idx = indexes[end_id]
        if start_idx > end_idx:
            start_idx, end_idx = end_idx, start_idx
        if end_idx >= protected_start:
            return None, "Selected range overlaps the protected tail"
        if end_idx - start_idx + 1 < 2:
            return None, "At least 2 messages are required for compaction"
        return segment_for_range(messages, start_idx, end_idx, reason="explicit_range"), None

    end_idx = protected_start - 1
    if end_idx < 1:
        return None, "No compactable range outside the protected tail"
    return segment_for_range(messages, 0, end_idx), None


def select_auto_segment(messages, protect_last_messages):
    if len(messages) < 2:
        return None, "At least 2 messages are required for compaction"

    protected_start = protected_start_index(messages, protect_last_messages)
    indexes = message_index(messages)
    for segment in identify_compactable_segments(messages):
        start_idx = indexes.get(str(segment.get("start_id") or ""))
        end_idx = indexes.get(str(segment.get("end_id") or ""))
        if start_idx is None or end_idx is None:
            continue
        if start_idx > end_idx:
            start_idx, end_idx = end_idx, start_idx
        if end_idx >= protected_start or end_idx - start_idx + 1 < 2:
            continue
        return segment_for_range(
            messages,
            start_idx,
            end_idx,
            reason=str(segment.get("reason") or "heuristic"),
            summary_preview=str(segment.get("summary_preview") or ""),
        ), None
    return select_oldest_safe_segment(messages, protect_last_messages)


def content_ref_for(conversation_id, segment):
    return (
        "chat://conversations/"
        + str(conversation_id)
        + "/messages/"
        + str(segment.get("start_id"))
        + ".."
        + str(segment.get("end_id"))
    )


def compact_selected_segment(store, conversation_id, conversation, segment, context, input_data, protect_last_messages):
    range_result = store.get_messages_range(conversation_id, segment["start_id"], segment["end_id"])
    if range_result is None:
        return None, "Invalid message range: start or end message not found"

    range_messages, _start_idx = range_result
    if len(range_messages) < 2:
        return None, "At least 2 messages are required for compaction"

    model = str(input_data.get("model") or "default")
    if model == "default":
        model = conversation.get("model", "stub/default")
    instruction = input_data.get("instruction")
    compacted_at = now_ms()
    original_message_ids = [msg["id"] for msg in range_messages]
    content_ref = content_ref_for(conversation_id, segment)
    summary_text = call_summary_ai(context, model, range_messages, instruction)

    summary_msg_dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": summary_text}],
        "metadata": {
            "is_summary": True,
            "compact": True,
            "edit_type": "compact",
            "compacted_at": compacted_at,
            "original_message_count": len(range_messages),
            "original_message_ids": original_message_ids,
            "model": model,
            "protect_last_messages": protect_last_messages,
            "content_ref": content_ref,
            "content_ref_details": {
                "conversation_id": conversation_id,
                "start_message_id": segment["start_id"],
                "end_message_id": segment["end_id"],
            },
            "summary_instruction": instruction,
        },
    }

    result, err = replace_range_with_message(
        store,
        conversation_id,
        segment["start_id"],
        segment["end_id"],
        summary_msg_dict,
    )
    if err is not None:
        return None, err
    replacement = result.get("replacement_message") or {}
    return {
        "conversation": result.get("conversation"),
        "summary_message": replacement,
        "deleted_message_ids": result.get("deleted_message_ids", original_message_ids),
        "deleted_count": result.get("deleted_count", len(original_message_ids)),
        "protect_last_messages": protect_last_messages,
        "compacted_range": segment,
        "content_ref": content_ref,
    }, None


def plan_payload(conversation_id, conversation, segment, protect_last_messages, message=None):
    segments = [segment] if segment else []
    return {
        "conversation_id": conversation_id,
        "total_messages": len(conversation.get("messages", [])),
        "protect_last_messages": protect_last_messages,
        "trim_plan": {"segments": segments},
        "compactable": bool(segment),
        "would_delete_message_ids": segment.get("original_message_ids", []) if segment else [],
        "message": message,
    }
