"""Audit helpers for generic runtime events."""

from __future__ import annotations

from typing import Any


SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(sensitive in key_text.lower() for sensitive in SENSITIVE_KEYS):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def audit_event(context: dict[str, Any] | None, event_type: str, payload: dict[str, Any] | None = None) -> None:
    try:
        from .audit_logger import AuditEntry, AuditLogger
    except Exception:
        return

    context = context if isinstance(context, dict) else {}
    details = redact_sensitive(payload or {})
    owner_pack = str(
        context.get("pack_id") or context.get("_source_pack_id") or ""
    ).strip()
    entry = AuditEntry(
        ts=_utc_now(),
        category="system",
        severity="info",
        action=event_type,
        success=True,
        owner_pack=owner_pack or None,
        details=details,
    )
    logger = context.get("audit_logger")
    if logger is None:
        logger = AuditLogger()
    try:
        logger.log(entry)
        logger.flush()
    except Exception:
        return


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
