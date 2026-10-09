"""Finite Calendar recurrence calculations; no timer, callback or authority."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _field(text: str, lower: int, upper: int) -> set[int]:
    result: set[int] = set()
    for item in text.split(","):
        base, separator, step_text = item.partition("/")
        step = int(step_text) if separator and step_text.isdecimal() else 1
        if separator and (not step_text.isdecimal() or step < 1):
            raise ValueError("cron step is invalid")
        if base == "*":
            first, last = lower, upper
        elif "-" in base:
            left, right = base.split("-", 1)
            if not left.isdecimal() or not right.isdecimal():
                raise ValueError("cron range is invalid")
            first, last = int(left), int(right)
        elif base.isdecimal():
            first = int(base)
            last = upper if separator else first
        else:
            raise ValueError("cron field is invalid")
        if not lower <= first <= last <= upper:
            raise ValueError("cron field exceeds its bounds")
        result.update(range(first, last + 1, step))
    return result


def normalize_recurrence(value: Any) -> dict[str, str] | None:
    """Validate a bounded five-field cron with explicit persisted timezone."""
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"kind", "expression", "timezone"}
        or value["kind"] != "cron"
    ):
        raise ValueError("schedule recurrence fields are invalid")
    expression, zone = value["expression"], value["timezone"]
    if (
        not isinstance(expression, str)
        or len(expression) > 256
        or len(expression.split()) != 5
    ):
        raise ValueError("cron requires five bounded fields")
    if not isinstance(zone, str) or not zone or len(zone) > 128:
        raise ValueError("cron timezone is invalid")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("cron timezone is unavailable") from error
    for field, (lo, hi) in zip(
        expression.split(), [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    ):
        _field(field, lo, hi)
    return {
        "kind": "cron",
        "expression": " ".join(expression.split()),
        "timezone": zone,
    }


def next_cron_ms(value: Mapping[str, Any], after_ms: int) -> int:
    """Find the next UTC minute within 366 days, preserving legacy AND matching."""
    recurrence = normalize_recurrence(value)
    fields = [
        _field(field, lo, hi)
        for field, (lo, hi) in zip(
            recurrence["expression"].split(),
            [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)],
        )
    ]
    zone = ZoneInfo(recurrence["timezone"])
    # Iterate UTC minutes: DST gaps are skipped and repeated wall times retain
    # distinct due instants. The search is finite even for impossible dates.
    candidate = (after_ms // 60_000 + 1) * 60_000
    for _ in range(366 * 24 * 60):
        local = datetime.fromtimestamp(candidate / 1000, timezone.utc).astimezone(zone)
        values = [
            local.minute,
            local.hour,
            local.day,
            local.month,
            (local.weekday() + 1) % 7,
        ]
        if all(number in allowed for number, allowed in zip(values, fields)):
            return candidate
        candidate += 60_000
    raise ValueError("cron has no occurrence within 366 days")
