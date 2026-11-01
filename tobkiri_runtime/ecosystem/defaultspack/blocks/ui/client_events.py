from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from typing import Any

from blocks._common import error, ok

from domain.safety.audit import append_record


SCHEMA_VERSION = "rumi.client_diagnostic.v2"
MAX_PAYLOAD_BYTES = 16 * 1024
RATE_WINDOW_SECONDS = 60.0
RATE_LIMIT = 30

_ALLOWED_FIELDS = {
    "schema_version",
    "event_id",
    "session_id",
    "source",
    "category",
    "level",
    "message",
    "fingerprint",
    "context_id",
    "privacy_mode",
    "detail",
}
_APP_FRAME_MARKERS = ("/src/", "/assets/", "/static/", "/webapp/")
_recent_events: deque[float] = deque()


def _safe_string(value: Any) -> str:
    try:
        return str(value or "")
    except Exception:
        return ""


def _redact_text(value: Any, *, max_length: int = 400) -> str | None:
    text = _safe_string(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", text)
    substitutions = (
        (r"\bdata:[^\s,;]+,[^\s]+", "[data-url]"),
        (r"\b(?:https?|wss?|file)://[^\s<>'\"\])}]+", "[url]"),
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]"),
        (r"\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n,;]+", "[auth-header]"),
        (r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", "[credential]"),
        (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[credential]"),
        (r"\b(?:sk|rk|pk|ghp|github_pat|xox[baprs]|ya29)[-_][A-Za-z0-9_-]{8,}\b", "[credential]"),
        (r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|password|passwd|credential|session[_-]?id|csrf[_-]?token)\b\s*[:=]\s*[\"']?[^\s,;\"'\]}]+", "[credential]"),
        (r"\b[A-Za-z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n\s):]*", "[path]"),
        (r"(?:/Users/|/home/|/var/|/tmp/|/private/|/opt/|/etc/)[^\s):\]}]+", "[path]"),
        (r"\b(?:[A-Fa-f0-9]{40,}|[A-Za-z0-9+/=_-]{80,})\b", "[opaque]"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length] or None


def _slug(value: Any, fallback: str, *, max_length: int = 80) -> str:
    text = (_redact_text(value, max_length=max_length) or fallback).lower()
    text = re.sub(r"[^a-z0-9._-]+", "_", text).strip("_")
    return (text or fallback)[:max_length]


def _opaque_identifier(prefix: str, value: Any) -> str | None:
    text = _safe_string(value).strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _integer(value: Any, *, minimum: int = 0, maximum: int = 10_000_000) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if minimum <= number <= maximum else None


def _app_route(value: Any) -> str | None:
    text = _safe_string(value).strip()
    if not text:
        return None
    match = re.search(r"(/(?:src|assets|static|webapp)/[^?\s#)]*)", text)
    if match:
        return match.group(1)[:240]
    if text.startswith("/") and not text.startswith("//"):
        return text.split("?", 1)[0].split("#", 1)[0][:240]
    if re.match(r"^[A-Za-z]:\\", text) or text.startswith(("/Users/", "/home/", "/tmp/", "/private/")):
        return "[path]"
    if "://" in text:
        return "[url]"
    return _redact_text(text, max_length=240)


def _normalize_stack(value: Any) -> str | None:
    frames: list[str] = []
    for raw_line in _safe_string(value).splitlines()[:40]:
        if re.search(r"node_modules|chrome-extension:|moz-extension:", raw_line, flags=re.IGNORECASE):
            continue
        line = re.sub(
            r"\bhttps?://[^/\s)]+(/(?:src|assets|static|webapp)/[^?\s#)]*)(?:[?#][^\s)]*)?",
            r"\1",
            raw_line,
            flags=re.IGNORECASE,
        )
        if not any(marker in line for marker in _APP_FRAME_MARKERS):
            component = re.match(r"^\s*at\s+([A-Za-z0-9_$.[\]-]{1,100})(?:\s|$)", line)
            if not component:
                continue
            line = f"at {component.group(1)}"
        sanitized = _redact_text(line, max_length=240)
        if sanitized and sanitized not in frames:
            frames.append(sanitized)
        if len(frames) >= 12:
            break
    return "\n".join(frames) or None


def _normalize_component_stack(value: Any) -> str | None:
    frames: list[str] = []
    for line in _safe_string(value).splitlines()[:40]:
        match = re.match(r"^\s*at\s+([A-Za-z0-9_$.[\]-]{1,100})", line)
        if not match:
            continue
        frame = f"at {match.group(1)}"
        if frame not in frames:
            frames.append(frame)
        if len(frames) >= 12:
            break
    return "\n".join(frames) or None


def _normalize_detail(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    detail: dict[str, Any] = {}
    error_name = _slug(source.get("error_name"), "", max_length=80) if source.get("error_name") else None
    error_code = _slug(source.get("error_code"), "", max_length=80) if source.get("error_code") else None
    route = _app_route(source.get("route"))
    line = _integer(source.get("line"))
    column = _integer(source.get("column"))
    stack = _normalize_stack(source.get("stack"))
    component_stack = _normalize_component_stack(source.get("component_stack"))
    reason_type = _slug(source.get("reason_type"), "", max_length=80) if source.get("reason_type") else None
    http_status = _integer(source.get("http_status"), minimum=100, maximum=599)
    frame_count = _integer(source.get("frame_count"), maximum=10_000)

    for key, item in (
        ("error_name", error_name),
        ("error_code", error_code),
        ("route", route),
        ("line", line),
        ("column", column),
        ("stack", stack),
        ("component_stack", component_stack),
        ("reason_type", reason_type),
        ("http_status", http_status),
        ("frame_count", frame_count),
    ):
        if item is not None:
            detail[key] = item
    return detail


def _payload_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return MAX_PAYLOAD_BYTES + 1


def _consume_rate_limit() -> bool:
    now = time.monotonic()
    while _recent_events and now - _recent_events[0] > RATE_WINDOW_SECONDS:
        _recent_events.popleft()
    if len(_recent_events) >= RATE_LIMIT:
        return False
    _recent_events.append(now)
    return True


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    method = str(data.get("_method") or "POST").upper()
    if method != "POST":
        return error("unsupported method", "METHOD_NOT_ALLOWED")

    if _payload_size(data) > MAX_PAYLOAD_BYTES:
        return error("diagnostic payload is too large", "PAYLOAD_TOO_LARGE")

    unknown_fields = sorted(
        key for key in data
        if not str(key).startswith("_") and key not in _ALLOWED_FIELDS
    )
    if unknown_fields:
        return error("unsupported diagnostic fields", "INVALID_INPUT")

    if data.get("schema_version") != SCHEMA_VERSION:
        return error("unsupported diagnostic schema", "INVALID_SCHEMA")
    if str(data.get("privacy_mode") or "") != "standard":
        return error("remote diagnostic reporting is disabled for this privacy mode", "PRIVACY_MODE_BLOCKED")

    event_id = _opaque_identifier("event", data.get("event_id"))
    session_id = _opaque_identifier("session", data.get("session_id"))
    fingerprint = _opaque_identifier("diag", data.get("fingerprint"))
    if not event_id or not session_id or not fingerprint:
        return error("diagnostic identifiers are required", "INVALID_INPUT")

    message = _redact_text(data.get("message"), max_length=320)
    if not message:
        return error("message is required", "INVALID_INPUT")
    if not _consume_rate_limit():
        return error("diagnostic rate limit exceeded", "RATE_LIMITED")

    record = append_record(
        {
            "event": "client_diagnostic",
            "operation": "ui.client_diagnostic",
            "risk": "low",
            "decision": "recorded",
            "schema_version": SCHEMA_VERSION,
            "privacy_mode": "standard",
            "retention_class": "short",
            "contains_user_content": False,
            "event_id": event_id,
            "session_id": session_id,
            "source": _slug(data.get("source"), "webapp"),
            "category": _slug(data.get("category"), "frontend"),
            "level": _slug(data.get("level"), "error", max_length=24),
            "message": message,
            "fingerprint": fingerprint,
            "context_id": _opaque_identifier("ctx", data.get("context_id")),
            "details": _normalize_detail(data.get("detail")),
        }
    )
    return ok({"recorded": True, "diagnostic_id": record["id"]})
