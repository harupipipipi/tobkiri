from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

from tobkiri_protocol.conversation_lifecycle import task_gap_prompt

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python always has zoneinfo here.
    ZoneInfo = None  # type: ignore[assignment]


_DEFAULT_TIME_ZONE = "Asia/Tokyo"
_TEMPORAL_CONTEXT_MARKER = "Current date/time:"


def current_datetime_context(
    context: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    tz_name = _configured_timezone(context)
    tz = _zoneinfo(tz_name)
    if now is None:
        local_now = datetime.now(tz).astimezone(tz) if tz is not None else datetime.now().astimezone()
    else:
        base_now = now
        if base_now.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            base_now = base_now.replace(tzinfo=tz or local_tz)
        local_now = base_now.astimezone(tz) if tz is not None else base_now.astimezone()
    offset = _utc_offset(local_now)
    resolved_tz_name = tz_name if tz is not None else str(local_now.tzinfo or "local")
    return {
        "iso": local_now.isoformat(timespec="seconds"),
        "date": local_now.date().isoformat(),
        "time": local_now.strftime("%H:%M:%S"),
        "timezone": resolved_tz_name,
        "utc_offset": offset,
    }


def temporal_context_prompt(
    context: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    temporal_context: dict[str, str] | None = None,
) -> str:
    temporal = dict(temporal_context or current_datetime_context(context, now=now))
    date = temporal.get("date", "")
    return (
        "Current date/time: {iso} ({timezone}, UTC{offset}). "
        "Interpret relative dates such as today, yesterday, and tomorrow using this date. "
        "Today is {date}."
    ).format(
        iso=temporal.get("iso", ""),
        timezone=temporal.get("timezone", ""),
        offset=temporal.get("utc_offset", ""),
        date=date,
    )


def add_temporal_context_message(
    messages: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    temporal_context: dict[str, str] | None = None,
) -> str:
    prompt = temporal_context_prompt(context, now=now, temporal_context=temporal_context)
    if not prompt:
        return ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "system":
            continue
        if _TEMPORAL_CONTEXT_MARKER in str(message.get("content") or ""):
            return prompt
    insert_at = 1 if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system" else 0
    messages.insert(insert_at, {"role": "system", "content": prompt})
    return prompt


def _configured_timezone(context: dict[str, Any] | None) -> str:
    for source in (
        context if isinstance(context, dict) else {},
        (
            (context or {}).get("user_preferences")
            if isinstance(context, dict) and isinstance(context.get("user_preferences"), dict)
            else {}
        ),
        (
            (context or {}).get("settings")
            if isinstance(context, dict) and isinstance(context.get("settings"), dict)
            else {}
        ),
    ):
        if not isinstance(source, dict):
            continue
        for key in ("user_timezone", "time_zone", "timezone", "tz"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    for key in ("RUMI_USER_TIMEZONE", "RUMI_DEFAULT_TIMEZONE", "TZ"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return _DEFAULT_TIME_ZONE


def _zoneinfo(tz_name: str):
    if ZoneInfo is None or not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def _utc_offset(value: datetime) -> str:
    raw = value.strftime("%z")
    if len(raw) == 5:
        return raw[:3] + ":" + raw[3:]
    return raw or "+00:00"


def add_task_gap_context_message(
    messages: list[dict[str, Any]], task_gap: Mapping[str, Any] | None,
) -> str:
    """Insert runtime gap metadata as system context without editing user content."""
    if task_gap is None:
        return ""
    prompt = task_gap_prompt(task_gap)
    insert_at = next(
        (index for index, message in enumerate(messages)
         if message.get("role") != "system"), len(messages),
    )
    messages.insert(insert_at, {"role": "system", "content": prompt})
    return prompt
