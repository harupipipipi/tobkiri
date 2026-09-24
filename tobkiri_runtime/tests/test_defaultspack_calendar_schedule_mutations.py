from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR",
        str(tmp_path / "schedules"),
    )
    from domain.agent.scheduler import Scheduler

    Scheduler._instance = None
    instance = Scheduler()
    yield instance
    for schedule_id in list(instance._timers):
        instance._cancel_timer(schedule_id)
    Scheduler._instance = None


def _create(scheduler, mutation_id: str):
    return scheduler.create_schedule(
        "once",
        {
            "message": "Run calendar task",
            "metadata": {
                "source": "calendar",
                "calendar_item_id": "calendar-1",
            },
        },
        {"run_at": "2099-01-01T09:00:00Z"},
        name="Calendar: task",
        mutation_id=mutation_id,
    )


def test_create_retry_returns_the_committed_schedule(scheduler):
    first = _create(scheduler, "calendar-create-1")
    retry = _create(scheduler, "calendar-create-1")

    assert retry["id"] == first["id"]
    assert retry["revision"] == 1
    assert len(scheduler.list_schedules()) == 1


def test_update_requires_revision_and_settles_each_mutation_once(scheduler):
    schedule = _create(scheduler, "calendar-create-1")
    updated = scheduler.update_schedule(
        schedule["id"],
        {
            "name": "Calendar: updated",
            "expected_revision": 1,
            "mutation_id": "calendar-update-1",
        },
    )

    assert updated["revision"] == 2
    assert updated["name"] == "Calendar: updated"
    retry = scheduler.update_schedule(
        schedule["id"],
        {
            "name": "ignored retry payload",
            "expected_revision": 1,
            "mutation_id": "calendar-update-1",
        },
    )
    assert retry["revision"] == 2
    assert retry["name"] == "Calendar: updated"

    with pytest.raises(ValueError, match="revision conflict"):
        scheduler.update_schedule(
            schedule["id"],
            {
                "name": "stale overwrite",
                "expected_revision": 1,
                "mutation_id": "calendar-update-stale",
            },
        )


def test_delete_rejects_a_stale_revision(scheduler):
    schedule = _create(scheduler, "calendar-create-1")
    scheduler.update_schedule(
        schedule["id"],
        {
            "name": "Calendar: updated",
            "expected_revision": 1,
            "mutation_id": "calendar-update-1",
        },
    )

    with pytest.raises(ValueError, match="revision conflict"):
        scheduler.delete_schedule(schedule["id"], expected_revision=1)

    assert scheduler.delete_schedule(schedule["id"], expected_revision=2) is True


def test_concurrent_devices_cannot_both_commit_the_same_revision(scheduler):
    schedule = _create(scheduler, "calendar-create-1")
    barrier = threading.Barrier(3)
    outcomes = []

    def update(name, mutation_id):
        barrier.wait()
        try:
            result = scheduler.update_schedule(
                schedule["id"],
                {"name": name, "expected_revision": 1, "mutation_id": mutation_id},
            )
            outcomes.append(("ok", result["revision"]))
        except ValueError as exc:
            outcomes.append(("conflict", str(exc)))

    threads = [
        threading.Thread(target=update, args=("Device A", "device-a")),
        threading.Thread(target=update, args=("Device B", "device-b")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(result[0] for result in outcomes) == ["conflict", "ok"]
    assert scheduler.get_schedule(schedule["id"])["revision"] == 2


def test_failed_update_write_does_not_publish_an_in_memory_revision(scheduler, monkeypatch):
    schedule = _create(scheduler, "calendar-create-1")
    import domain.agent.scheduler as scheduler_module

    def fail_save(_schedule):
        raise OSError("disk full")

    monkeypatch.setattr(scheduler_module, "save_schedule", fail_save)
    with pytest.raises(OSError, match="disk full"):
        scheduler.update_schedule(
            schedule["id"],
            {
                "name": "must not leak",
                "expected_revision": 1,
                "mutation_id": "calendar-update-failed",
            },
        )

    cached = scheduler.get_schedule(schedule["id"])
    assert cached["name"] == "Calendar: task"
    assert cached["revision"] == 1
    assert "calendar-update-failed" not in cached["settled_mutation_ids"]


def test_failed_delete_write_keeps_the_schedule_visible(scheduler, monkeypatch):
    schedule = _create(scheduler, "calendar-create-1")
    import domain.agent.scheduler as scheduler_module

    def fail_delete(_schedule_id):
        raise OSError("access denied")

    monkeypatch.setattr(scheduler_module, "store_delete", fail_delete)
    with pytest.raises(OSError, match="access denied"):
        scheduler.delete_schedule(schedule["id"], expected_revision=1)

    assert scheduler.get_schedule(schedule["id"])["revision"] == 1


def test_completed_delete_tombstone_blocks_late_execution_resurrection(scheduler):
    schedule = _create(scheduler, "calendar-create-1")
    schedule_id = schedule["id"]
    import domain.agent.scheduler as scheduler_module

    assert scheduler.delete_schedule(schedule_id, expected_revision=1) is True
    assert scheduler._save_schedule_durable(schedule) is False
    assert scheduler_module.load_schedule(schedule_id) is None

    # Even if an already-running execution publishes its stale in-memory
    # object after deletion, public reads and timer arming stay deleted.
    with scheduler._lock:
        scheduler._schedules[schedule_id] = schedule
    scheduler._arm_timer(schedule_id)
    assert scheduler.get_schedule(schedule_id) is None
    assert all(item["id"] != schedule_id for item in scheduler.list_schedules())
    assert schedule_id not in scheduler._timers


def test_update_does_not_consume_the_callers_revision_envelope(scheduler):
    schedule = _create(scheduler, "calendar-create-1")
    updates = {
        "name": "Calendar: updated",
        "expected_revision": 1,
        "mutation_id": "calendar-update-1",
    }

    scheduler.update_schedule(schedule["id"], updates)

    assert updates["expected_revision"] == 1
    assert updates["mutation_id"] == "calendar-update-1"
