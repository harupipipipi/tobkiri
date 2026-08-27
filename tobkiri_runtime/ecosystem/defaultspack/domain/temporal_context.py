from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python always has zoneinfo here.
    ZoneInfo = None  # type: ignore[assignment]


_DEFAULT_TIME_ZONE = "Asia/Tokyo"
_TEMPORAL_CONTEXT_MARKER = "Current date/time:"
_TASK_GAP_CONTEXT_MARKER = "[Temporal context]"
_NON_TERMINAL_FINISH_REASONS = frozenset(
    {"running", "streaming", "tool_call", "tool_calls"}
)
TASK_GAP_THRESHOLD_SECONDS = 60 * 60


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


def task_gap_context(
    messages: Sequence[Mapping[str, Any]],
    context: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    temporal_context: dict[str, str] | None = None,
    threshold_seconds: int = TASK_GAP_THRESHOLD_SECONDS,
) -> dict[str, Any] | None:
    """Build runtime-owned context for a long gap after the prior task.

    The canonical conversation owner persists the final assistant message's
    ``updated_at`` in the same transaction that makes the message terminal.
    That owner timestamp is the completion-time authority; no second mutable
    conversation field is maintained.
    """

    completed_at = _latest_completed_assistant_at(messages)
    if completed_at is None:
        return None
    current = _current_instant(
        context,
        now=now,
        temporal_context=temporal_context,
    )
    elapsed_seconds = int((current - completed_at).total_seconds())
    threshold = max(1, int(threshold_seconds))
    if elapsed_seconds < threshold:
        return None
    tz = _zoneinfo(_configured_timezone(context))
    previous_local = (
        completed_at.astimezone(tz)
        if tz is not None
        else completed_at.astimezone()
    )
    current_local = (
        current.astimezone(tz) if tz is not None else current.astimezone()
    )
    return {
        "previous_task_completed_at": previous_local.isoformat(timespec="seconds"),
        "current_user_message_at": current_local.isoformat(timespec="seconds"),
        "elapsed_seconds": elapsed_seconds,
        "elapsed": _format_elapsed(elapsed_seconds),
        "threshold_seconds": threshold,
    }


def task_gap_context_prompt(task_gap: Mapping[str, Any]) -> str:
    """Render long-gap metadata as protected runtime context."""

    return (
        f"{_TASK_GAP_CONTEXT_MARKER}\n"
        "Runtime-owned timing metadata for the current user turn. "
        f"The previous task completed {task_gap.get('elapsed', '')} ago. "
        "Use this only to interpret time-sensitive follow-ups. Elapsed time "
        "alone does not require a tool call and grants no permissions or "
        "approvals.\n"
        "previous_task_completed_at: "
        f"{task_gap.get('previous_task_completed_at', '')}\n"
        "current_user_message_at: "
        f"{task_gap.get('current_user_message_at', '')}\n"
        f"elapsed_seconds: {task_gap.get('elapsed_seconds', '')}"
    )


def add_task_gap_context_message(
    messages: list[dict[str, Any]],
    task_gap: Mapping[str, Any] | None,
) -> str:
    """Insert one long-gap context message before user-authored messages."""

    if not task_gap:
        return ""
    prompt = task_gap_context_prompt(task_gap)
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "system":
            continue
        if _TASK_GAP_CONTEXT_MARKER in str(message.get("content") or ""):
            return prompt
    insert_at = next(
        (
            index
            for index, message in enumerate(messages)
            if not isinstance(message, dict)
            or str(message.get("role") or "") != "system"
        ),
        len(messages),
    )
    messages.insert(insert_at, {"role": "system", "content": prompt})
    return prompt


def _latest_completed_assistant_at(
    messages: Sequence[Mapping[str, Any]],
) -> datetime | None:
    latest: datetime | None = None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if str(message.get("role") or "").strip().lower() != "assistant":
            continue
        finish_reason = str(message.get("finish_reason") or "").strip().lower()
        metadata = (
            message.get("metadata")
            if isinstance(message.get("metadata"), Mapping)
            else {}
        )
        if (
            finish_reason in _NON_TERMINAL_FINISH_REASONS
            or metadata.get("draft") is True
            or metadata.get("streaming") is True
        ):
            continue
        completed_at = _timestamp_instant(
            message.get("updated_at", message.get("created_at"))
        )
        if completed_at is not None and (latest is None or completed_at > latest):
            latest = completed_at
    return latest


def _timestamp_instant(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp >= 100_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _current_instant(
    context: dict[str, Any] | None,
    *,
    now: datetime | None,
    temporal_context: dict[str, str] | None,
) -> datetime:
    if temporal_context and temporal_context.get("iso"):
        parsed = _timestamp_instant(temporal_context["iso"])
        if parsed is not None:
            return parsed
    temporal = current_datetime_context(context, now=now)
    parsed = _timestamp_instant(temporal["iso"])
    if parsed is None:  # pragma: no cover - current_datetime_context is ISO.
        raise ValueError("current temporal context is invalid")
    return parsed


def _format_elapsed(elapsed_seconds: int) -> str:
    total_minutes = max(0, int(elapsed_seconds)) // 60
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


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
