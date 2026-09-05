"""Normalize executor values into one redacted tool result envelope."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

_SECRET_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}


def create_normalize_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a stable result normalizer with secret-field redaction."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"normalize", "result"}:
            raise ValueError(f"unknown tool result operation: {name}")
        raw = payload.get("value")
        executor_error = payload.get("executor_error")
        if executor_error:
            code = str(payload.get("error_code") or "executor_error")
            return _envelope(
                payload,
                value=None,
                is_error=True,
                error={"code": code, "message": str(executor_error)[:1000]},
            )
        if isinstance(raw, Mapping):
            mapping = dict(raw)
            is_error = bool(mapping.get("is_error", False))
            error = mapping.get("error")
            value = mapping.get("result", mapping.get("value", mapping))
            widget = mapping.get("widget")
        else:
            is_error = False
            error = None
            value = raw
            widget = None
        result = _envelope(
            payload,
            value=_safe(value),
            is_error=is_error,
            error=_safe(error) if error is not None else None,
        )
        if isinstance(widget, Mapping):
            result["widget"] = _safe(dict(widget))
        return result

    return operation


def _envelope(
    payload: Mapping[str, Any],
    *,
    value: Any,
    is_error: bool,
    error: Any,
) -> dict[str, Any]:
    return {
        "tool_call_id": str(payload.get("tool_call_id") or ""),
        "tool_id": str(payload.get("tool_id") or ""),
        "status": "error" if is_error else "success",
        "is_error": is_error,
        "result": value,
        "error": error,
        "widget": None,
        "executor": {
            "provider_instance_id": str(
                payload.get("executor_provider_instance_id") or ""
            ),
            "content_hash": str(payload.get("executor_content_hash") or ""),
        },
    }


def _safe(value: Any) -> Any:
    copied = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    return _redact(copied)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).lower() in _SECRET_KEYS
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value

