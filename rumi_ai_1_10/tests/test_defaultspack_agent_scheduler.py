from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.agent.scheduler import trigger as trigger_block  # noqa: E402
from domain.agent.schedule_store import load_schedule  # noqa: E402
from domain.agent.scheduler import Scheduler  # noqa: E402


def _reset_scheduler_singleton() -> None:
    instance = getattr(Scheduler, "_instance", None)
    if instance is not None:
        instance.shutdown()
    Scheduler._instance = None


def test_manual_trigger_clears_running_execution_when_chat_cannot_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    _reset_scheduler_singleton()
    scheduler = Scheduler()

    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "heartbeat",
            "model": "stub/default",
            "conversation_id": "missing-conversation",
        },
        {"value": 30, "unit": "minutes"},
        name="missing conversation trigger",
    )

    result = trigger_block.run({"schedule_id": schedule["id"]}, {})

    assert result["status"] == "error"
    assert result["error"]["code"] == "SCHEDULE_TRIGGER_FAILED"
    assert result["_http_status"] == 409
    assert "Conversation not found" in result["error"]["message"]
    history_entry = result["data"]["history_entry"]
    assert history_entry["execution_id"].startswith("sexec_")
    assert history_entry["status"] == "error"
    assert history_entry["error"] == "Conversation not found"
    assert history_entry["error_code"] == "NOT_FOUND"

    stored = scheduler.get_schedule(schedule["id"])
    persisted = load_schedule(schedule["id"])
    assert "running_execution" not in stored
    assert "running_execution" not in persisted
    assert stored["last_execution_status"] == "error"
    assert stored["last_execution_error"] == "Conversation not found"

    history = scheduler.get_history(schedule["id"])
    assert history["total"] == 1
    assert history["entries"][0]["execution_id"] == history_entry["execution_id"]
    assert history["entries"][0]["status"] == "error"

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_manual_trigger_clears_running_execution_for_approval_required_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_scheduler_singleton()
    scheduler = Scheduler()
    seen_payloads = []

    def fake_chat_send(payload, context):
        seen_payloads.append(payload)
        running = load_schedule(payload["message"]["metadata"]["schedule_id"]).get("running_execution")
        assert running["execution_id"] == payload["message"]["metadata"]["schedule_execution_id"]
        return {
            "status": "ok",
            "data": {
                "content": [{"type": "text", "text": "approval needed"}],
                "finish_reason": "approval_required",
                "metadata": {"pending_approval": {"request_id": "approval-1"}},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_chat_send)
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "needs approval",
            "model": "stub/default",
            "conversation_id": "conv-approval",
        },
        {"value": 30, "unit": "minutes"},
        name="approval trigger",
    )

    result = trigger_block.run({"schedule_id": schedule["id"]}, {})

    assert result["status"] == "ok"
    assert result["data"]["status"] == "approval_required"
    assert result["data"]["result"] == "approval needed"
    assert len(seen_payloads) == 1
    assert "running_execution" not in scheduler.get_schedule(schedule["id"])
    assert "running_execution" not in load_schedule(schedule["id"])
    assert scheduler.get_history(schedule["id"])["entries"][0]["status"] == "approval_required"

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_overlapping_trigger_keeps_older_execution_visible_until_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _reset_scheduler_singleton()
    scheduler = Scheduler()
    first_started = threading.Event()
    release_first = threading.Event()
    first_id = []

    def fake_chat_send(payload: dict, context: dict) -> dict:
        execution_id = payload["message"]["metadata"]["schedule_execution_id"]
        if not first_id:
            first_id.append(execution_id)
            first_started.set()
            assert release_first.wait(timeout=5)
        return {"status": "ok", "data": {"content": "done"}}

    monkeypatch.setattr("blocks.chat.send.run", fake_chat_send)
    schedule = scheduler.create_schedule(
        "interval",
        {"message": "heartbeat", "model": "stub/default", "conversation_id": "conv"},
        {"value": 30, "unit": "minutes"},
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(scheduler.trigger_now, schedule["id"])
            try:
                assert first_started.wait(timeout=5)
                second = scheduler.trigger_now(schedule["id"])
                assert second["status"] == "completed"
                persisted = load_schedule(schedule["id"])
                assert persisted["running_execution"]["execution_id"] == first_id[0]
                assert persisted["execution_count"] == 1
            finally:
                release_first.set()
            assert first.result(timeout=5)["status"] == "completed"
        persisted = load_schedule(schedule["id"])
        assert "running_execution" not in persisted
        assert persisted["execution_count"] == 2
        assert scheduler.get_history(schedule["id"])["total"] == 2
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
