from __future__ import annotations

from typing import Any


EVENT_SCHEMA_VERSION = 1


def run_event(
    event_type: str,
    *,
    run_id: str,
    conversation_id: str,
    seq: int,
    data: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "type": str(event_type or "").strip(),
        "run_id": str(run_id or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "seq": int(seq or 0),
        "data": dict(data or {}),
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _merged_payload(event: dict[str, Any]) -> dict[str, Any]:
    merged = dict(event.get("data") or {})
    for key, value in event.items():
        if key in {"schema_version", "type", "data"}:
            continue
        if key not in merged and value is not None:
            merged[key] = value
    return merged


def _message_from_event(event: dict[str, Any]) -> Any:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return data.get("message", event.get("message"))


def _error_from_event(event: dict[str, Any]) -> Any:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    error_value = data.get("error", event.get("error"))
    if isinstance(error_value, dict) or isinstance(error_value, str):
        return error_value
    message = data.get("message") or event.get("message") or "defaultspack stream failed"
    return {"message": str(message)}


def to_legacy_chat_stream_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "").strip()
    merged = _merged_payload(event)
    identity = {
        "run_id": str(event.get("run_id") or "").strip(),
        "conversation_id": str(event.get("conversation_id") or "").strip(),
        "seq": int(event.get("seq") or 0),
    }

    if event_type == "content_delta":
        return {
            "type": "delta",
            "delta": str(merged.get("delta") or ""),
            **identity,
        }
    if event_type == "thinking_delta":
        return {
            "type": "thinking_delta",
            "delta": str(merged.get("delta") or ""),
            **identity,
        }
    if event_type == "user_message_committed":
        return {
            "type": "user_message",
            "message": _message_from_event(event),
            **identity,
        }
    if event_type == "assistant_message_completed":
        return {
            "type": "message",
            "message": _message_from_event(event),
            **identity,
        }
    if event_type == "done":
        return {"type": "done", "message": _message_from_event(event), **identity}
    if event_type == "error":
        return {"type": "error", "error": _error_from_event(event), **identity}
    if event_type == "cancelled":
        return {"type": "error", "error": "cancelled", **identity}
    if event_type in {"run_started", "assistant_message_started"}:
        return None
    if event_type in {
        "status",
        "tool_call_started",
        "tool_call_delta",
        "tool_call_completed",
        "browser_state_invalidated",
        "browser_state_snapshot",
        "browser_dom_snapshot",
        "browser_screenshot",
        "approval_requested",
        "ai_retry_scheduled",
        "task_failed",
    }:
        return {"type": event_type, **merged, **identity}
    return {"type": event_type, **merged, **identity}
