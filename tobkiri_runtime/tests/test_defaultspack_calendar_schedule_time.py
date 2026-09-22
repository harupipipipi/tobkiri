from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.agent.calendar_schedule_time import (  # noqa: E402
    CALENDAR_TIME_POLICY_VERSION,
    normalize_once_calendar_config,
    resolve_calendar_wall_time,
)


def _config(**overrides):
    value = {
        "run_at": "2026-02-28T15:00:00Z",
        "local_date": "2026-03-01",
        "local_time": "00:00",
        "time_zone": "Asia/Tokyo",
        "time_mode": "floating",
        "dst_resolution": "exact",
        "interpretation_policy": CALENDAR_TIME_POLICY_VERSION,
        "multi_day_time_scope": "start_only",
    }
    value.update(overrides)
    return value


def test_normalizes_half_hour_zone_and_preserves_intent():
    result = normalize_once_calendar_config(
        _config(
            run_at="2026-06-01T03:30:00Z",
            local_date="2026-06-01",
            local_time="09:00",
            time_zone="Asia/Kolkata",
            time_mode="fixed",
        )
    )

    assert result["run_at"] == "2026-06-01T03:30:00Z"
    assert result["normalized_run_at"] == result["run_at"]
    assert result["utc_offset"] == "+05:30"
    assert result["time_mode"] == "fixed"


def test_spring_forward_gap_fails_closed():
    with pytest.raises(ValueError, match="does not exist"):
        normalize_once_calendar_config(
            _config(
                run_at="2026-03-08T07:30:00Z",
                local_date="2026-03-08",
                local_time="02:30",
                time_zone="America/New_York",
            )
        )


def test_fall_back_duplicate_requires_explicit_resolution():
    with pytest.raises(ValueError, match="ambiguous"):
        normalize_once_calendar_config(
            _config(
                run_at="2026-11-01T05:30:00Z",
                local_date="2026-11-01",
                local_time="01:30",
                time_zone="America/New_York",
            )
        )


def test_fall_back_duplicate_resolves_earlier_and_later():
    earlier = resolve_calendar_wall_time(
        "2026-11-01",
        "01:30",
        "America/New_York",
        dst_resolution="earlier",
    )
    later = resolve_calendar_wall_time(
        "2026-11-01",
        "01:30",
        "America/New_York",
        dst_resolution="later",
    )

    assert earlier["run_at"] == "2026-11-01T05:30:00Z"
    assert later["run_at"] == "2026-11-01T06:30:00Z"


def test_unambiguous_wall_time_rejects_stale_dst_resolution():
    with pytest.raises(ValueError, match="dst_resolution must be exact"):
        normalize_once_calendar_config(_config(dst_resolution="earlier"))


def test_rejects_invalid_iana_zone():
    with pytest.raises(ValueError, match="valid IANA"):
        normalize_once_calendar_config(_config(time_zone="Local/Guess"))


def test_rejects_client_instant_that_disagrees_with_wall_time():
    with pytest.raises(ValueError, match="does not match"):
        normalize_once_calendar_config(_config(run_at="2026-02-28T14:00:00Z"))


def test_utc_leap_day_is_stable():
    result = resolve_calendar_wall_time("2028-02-29", "23:59", "UTC")
    assert result == {
        "run_at": "2028-02-29T23:59:00Z",
        "utc_offset": "+00:00",
        "dst_resolution": "exact",
    }


def test_legacy_run_at_only_config_remains_compatible():
    config = {"run_at": "2099-01-01T00:00:00Z"}
    assert normalize_once_calendar_config(config) == config


def test_scheduler_persists_normalized_calendar_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR",
        str(tmp_path / "schedules"),
    )
    from domain.agent import scheduler as scheduler_module

    scheduler_module.Scheduler._instance = None
    scheduler = scheduler_module.Scheduler()
    monkeypatch.setattr(scheduler, "_arm_timer", lambda _schedule_id: None)
    schedule = scheduler.create_schedule(
        "once",
        {"message": "Run the calendar task"},
        {
            "run_at": "2099-02-28T15:00:00Z",
            "local_date": "2099-03-01",
            "local_time": "00:00",
            "time_zone": "Asia/Tokyo",
            "time_mode": "floating",
            "dst_resolution": "exact",
            "interpretation_policy": CALENDAR_TIME_POLICY_VERSION,
            "multi_day_time_scope": "start_only",
        },
    )

    assert schedule["config"]["normalized_run_at"] == "2099-02-28T15:00:00Z"
    assert schedule["config"]["utc_offset"] == "+09:00"
    assert schedule["next_execution_at"] == "2099-02-28T15:00:00Z"
    scheduler_module.Scheduler._instance = None


def test_scheduler_rejects_mismatched_calendar_instant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR",
        str(tmp_path / "schedules"),
    )
    from domain.agent import scheduler as scheduler_module

    scheduler_module.Scheduler._instance = None
    scheduler = scheduler_module.Scheduler()
    with pytest.raises(ValueError, match="does not match"):
        scheduler.create_schedule(
            "once",
            {"message": "Run the calendar task"},
            {
                "run_at": "2099-02-28T14:00:00Z",
                "local_date": "2099-03-01",
                "local_time": "00:00",
                "time_zone": "Asia/Tokyo",
                "time_mode": "floating",
                "dst_resolution": "exact",
                "interpretation_policy": CALENDAR_TIME_POLICY_VERSION,
            },
        )
    scheduler_module.Scheduler._instance = None


def test_calendar_time_revision_rejects_stale_update():
    current = normalize_once_calendar_config(_config())
    with pytest.raises(ValueError, match="revision is stale"):
        normalize_once_calendar_config(
            _config(expected_time_revision="stale-revision"),
            current_config=current,
        )

    accepted = normalize_once_calendar_config(
        _config(expected_time_revision=current["time_revision"]),
        current_config=current,
    )
    assert accepted["time_revision"] == current["time_revision"]
    assert "expected_time_revision" not in accepted


def test_calendar_time_revision_is_required_for_existing_contract_update():
    current = normalize_once_calendar_config(_config())

    with pytest.raises(ValueError, match="expected_time_revision is required"):
        normalize_once_calendar_config(
            _config(local_time="00:01", run_at="2026-02-28T15:01:00Z"),
            current_config=current,
        )
