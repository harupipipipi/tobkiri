"""Bind the Calendar UI routes to captured canonical schedule owners."""

from __future__ import annotations

import re
from typing import Mapping


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_READ = "rumi_schedule_store_pack.calendar.resource"
_WRITE = "rumi_schedule_store_pack.calendar.manage"
_READ_CONTRACT = "tobkiri.resource.calendar.schedule.v1"
_WRITE_CONTRACT = "tobkiri.action.calendar.schedule.v1"
_CREATE_KEYS = frozenset({"name", "description", "schedule_type",
                          "schedule_config", "task"})


def calendar_identity(operation: str) -> tuple[str, str, str, str, str]:
    """Return one exact public contribution and selected provider binding."""
    read = operation in {"list", "get", "history"}
    provider = _READ if read else _WRITE
    return ("defaults.calendar.schedules." + operation,
            _READ_CONTRACT if read else _WRITE_CONTRACT,
            "rumi_schedule_store_pack.calendar-read" if read else
            "rumi_schedule_store_pack.calendar-manage", provider, provider)


CALENDAR_TARGETS = {calendar_identity(operation): operation for operation in
                    ("list", "get", "history", "create", "update", "pause",
                     "resume", "delete", "trigger")}


def normalize_calendar_payload(
    identity: tuple[str, str, str, str, str],
    payload: Mapping[str, object],
    *,
    profile_id: str,
) -> dict[str, object]:
    """Inject the actual Profile and finite operation without caller authority."""
    if _ID.fullmatch(profile_id) is None or identity not in CALENDAR_TARGETS:
        raise ValueError("Calendar requires a captured Profile and route")
    operation = CALENDAR_TARGETS[identity]
    keys = (_CREATE_KEYS if operation in {"create", "update"} else
            {"status"} if operation == "list" else
            {"limit", "offset"} if operation == "history" else set())
    if operation not in {"create", "list"}:
        keys = set(keys) | {"schedule_id"}
    if set(payload) - keys:
        raise ValueError("Calendar request fields are invalid")
    result = dict(payload)
    if operation not in {"create", "list"}:
        identifier = result.get("schedule_id")
        if not isinstance(identifier, str) or _ID.fullmatch(identifier) is None:
            raise ValueError("Calendar schedule identity is invalid")
    if operation == "history":
        for field, lower, upper in (("limit", 1, 200), ("offset", 0, 1000000)):
            if field not in result:
                continue
            value = result[field]
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,7}", value):
                value = int(value)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Calendar history pagination is invalid")
            result[field] = value
    return {**result, "profile_id": profile_id, "operation": operation}
