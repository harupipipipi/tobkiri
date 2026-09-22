from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CALENDAR_TIME_POLICY_VERSION = "tobkiri.calendar-time.v1"
CALENDAR_TIME_CONFIG_KEYS = {
    "time_zone",
    "local_date",
    "local_time",
    "time_mode",
    "dst_resolution",
    "interpretation_policy",
}


def normalize_once_calendar_config(
    config: dict[str, Any],
    *,
    current_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalize a calendar-owned one-shot schedule config.

    Legacy one-shot schedules that only contain ``run_at`` remain compatible.
    Once any calendar time-contract field is supplied, the complete contract is
    required and the claimed UTC instant must match the IANA-zone resolution.
    """

    normalized = dict(config)
    expected_revision = str(normalized.pop("expected_time_revision", "") or "").strip()
    current_revision = str((current_config or {}).get("time_revision") or "").strip()
    if current_revision and not expected_revision:
        raise ValueError(
            "schedule_config.expected_time_revision is required when changing "
            "an existing calendar schedule"
        )
    if expected_revision and expected_revision != current_revision:
        raise ValueError("calendar time revision is stale; reload before changing the schedule")
    if not any(key in normalized for key in CALENDAR_TIME_CONFIG_KEYS):
        return normalized

    policy = _required_string(normalized, "interpretation_policy")
    if policy != CALENDAR_TIME_POLICY_VERSION:
        raise ValueError(
            "schedule_config.interpretation_policy must be " + CALENDAR_TIME_POLICY_VERSION
        )

    zone_name = _required_string(normalized, "time_zone")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("schedule_config.time_zone must be a valid IANA time zone") from exc

    local_date = _required_string(normalized, "local_date")
    local_time = _required_string(normalized, "local_time")
    try:
        wall_time = datetime.strptime(f"{local_date}T{local_time}", "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError(
            "schedule_config.local_date/local_time must use YYYY-MM-DD and HH:MM"
        ) from exc

    time_mode = _required_string(normalized, "time_mode")
    if time_mode not in {"floating", "fixed"}:
        raise ValueError("schedule_config.time_mode must be floating or fixed")

    candidates = _wall_time_candidates(wall_time, zone)
    if not candidates:
        raise ValueError(
            "calendar wall time does not exist in the selected time zone; "
            "choose a valid local time explicitly"
        )

    requested_resolution = _required_string(normalized, "dst_resolution")
    if len(candidates) == 1:
        if requested_resolution != "exact":
            raise ValueError("schedule_config.dst_resolution must be exact")
        selected = candidates[0]
        resolved_resolution = "exact"
    else:
        if requested_resolution not in {"earlier", "later"}:
            raise ValueError(
                "calendar wall time is ambiguous; choose dst_resolution earlier or later"
            )
        selected = candidates[0] if requested_resolution == "earlier" else candidates[-1]
        resolved_resolution = requested_resolution

    run_at = _parse_aware_datetime(_required_string(normalized, "run_at"))
    selected_utc = selected.astimezone(timezone.utc).replace(microsecond=0)
    if run_at.astimezone(timezone.utc).replace(microsecond=0) != selected_utc:
        raise ValueError(
            "schedule_config.run_at does not match the submitted local time and time zone"
        )

    time_scope = str(normalized.get("multi_day_time_scope") or "start_only").strip()
    if time_scope not in {"start_only", "each_day"}:
        raise ValueError("schedule_config.multi_day_time_scope must be start_only or each_day")

    normalized.update(
        {
            "run_at": _utc_iso(selected_utc),
            "normalized_run_at": _utc_iso(selected_utc),
            "time_zone": zone_name,
            "local_date": local_date,
            "local_time": local_time,
            "time_mode": time_mode,
            "dst_resolution": resolved_resolution,
            "interpretation_policy": CALENDAR_TIME_POLICY_VERSION,
            "utc_offset": _offset_text(selected.utcoffset()),
            "multi_day_time_scope": time_scope,
        }
    )
    normalized["time_revision"] = _time_revision(normalized)
    return normalized


def resolve_calendar_wall_time(
    local_date: str,
    local_time: str,
    time_zone: str,
    *,
    dst_resolution: str = "exact",
) -> dict[str, str]:
    """Resolve a calendar wall time for tests and non-HTTP callers."""

    probe = normalize_once_calendar_config(
        _config_with_computed_run_at(
            local_date,
            local_time,
            time_zone,
            dst_resolution=dst_resolution,
        )
    )
    return {
        "run_at": probe["run_at"],
        "utc_offset": probe["utc_offset"],
        "dst_resolution": probe["dst_resolution"],
    }


def _config_with_computed_run_at(
    local_date: str,
    local_time: str,
    time_zone: str,
    *,
    dst_resolution: str,
) -> dict[str, str]:
    try:
        zone = ZoneInfo(time_zone)
        wall_time = datetime.strptime(f"{local_date}T{local_time}", "%Y-%m-%dT%H:%M")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid calendar wall time") from exc
    candidates = _wall_time_candidates(wall_time, zone)
    if not candidates:
        selected_run_at = "1970-01-01T00:00:00Z"
    elif len(candidates) == 1 or dst_resolution == "earlier":
        selected_run_at = _utc_iso(candidates[0].astimezone(timezone.utc))
    else:
        selected_run_at = _utc_iso(candidates[-1].astimezone(timezone.utc))
    return {
        "run_at": selected_run_at,
        "local_date": local_date,
        "local_time": local_time,
        "time_zone": time_zone,
        "time_mode": "floating",
        "dst_resolution": dst_resolution,
        "interpretation_policy": CALENDAR_TIME_POLICY_VERSION,
        "multi_day_time_scope": "start_only",
    }


def _wall_time_candidates(wall_time: datetime, zone: ZoneInfo) -> list[datetime]:
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        local = wall_time.replace(tzinfo=zone, fold=fold)
        instant = local.astimezone(timezone.utc)
        round_trip = instant.astimezone(zone)
        if round_trip.replace(tzinfo=None) != wall_time:
            continue
        candidates[instant] = local
    return [candidates[key] for key in sorted(candidates)]


def _required_string(value: dict[str, Any], key: str) -> str:
    result = str(value.get(key) or "").strip()
    if not result:
        raise ValueError(f"schedule_config.{key} is required")
    return result


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("schedule_config.run_at must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("schedule_config.run_at must include a UTC offset")
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offset_text(value: Any) -> str:
    seconds = int(value.total_seconds()) if value is not None else 0
    sign = "+" if seconds >= 0 else "-"
    absolute = abs(seconds)
    hours, remainder = divmod(absolute, 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _time_revision(value: dict[str, Any]) -> str:
    payload = {
        key: value.get(key)
        for key in (
            "run_at",
            "local_date",
            "local_time",
            "time_zone",
            "time_mode",
            "dst_resolution",
            "interpretation_policy",
            "multi_day_time_scope",
        )
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
