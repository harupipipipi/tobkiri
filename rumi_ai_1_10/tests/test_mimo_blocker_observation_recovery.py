from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))
sys.path.insert(0, str(ROOT))


@pytest.mark.parametrize("failed_source", ["scheduler", "child_conversations"])
def test_read_failure_preserves_blocker_until_observed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_source: str
) -> None:
    from ecosystem.rumi_operations_company_pack.domain.agent import mimo_blocker_signals
    from domain.company.runtime_store import CompanyRuntimeStore

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company.db")
    )
    monkeypatch.setattr(CompanyRuntimeStore, "_instance", None)
    observation = {"status": "error", "unavailable": False}

    class Scheduler:
        def get_schedule(self, schedule_id: str) -> dict:
            return {"name": "Heartbeat"}

        def get_history(self, schedule_id: str, **kwargs: object) -> dict:
            if observation["unavailable"] and failed_source == "scheduler":
                raise OSError("temporary history read failure")
            return {
                "entries": [
                    {
                        "execution_id": "execution-1",
                        "status": observation["status"],
                        "error": "model unavailable",
                    }
                ]
            }

    class ChatStore:
        def get_conversation(self, conversation_id: str) -> dict:
            return {"id": conversation_id, "child_conversation_ids": []}

        def list_conversations(self, **kwargs: object) -> tuple[list, int]:
            if observation["unavailable"] and failed_source == "child_conversations":
                raise OSError("temporary child conversation read failure")
            return [], 0

    monkeypatch.setattr(mimo_blocker_signals, "Scheduler", Scheduler)
    monkeypatch.setattr(mimo_blocker_signals, "ChatStore", ChatStore)
    state = {
        "schedule_ids": {"heartbeat": "schedule-1"},
        "conversation_id": "parent-1",
    }
    arguments = {"company_id": "company-1", "profile_id": "profile-1"}
    initial = mimo_blocker_signals.sync_mimo_blocker_signals(state, **arguments)
    runtime = CompanyRuntimeStore()
    try:
        assert len(initial) == 1
        observation["unavailable"] = True
        unavailable = mimo_blocker_signals.sync_mimo_blocker_signals(state, **arguments)
        tasks, total = runtime.list_tasks("company-1")
        assert total == 1
        assert tasks[0]["status"] == "blocked"
        assert unavailable[0]["task_id"] == initial[0]["task_id"]
        assert unavailable[0]["observation_status"] == "unavailable"
        assert runtime.list_messages("company-1")[1] == 1

        observation.update(status="completed", unavailable=False)
        assert mimo_blocker_signals.sync_mimo_blocker_signals(state, **arguments) == []
        assert runtime.list_tasks("company-1")[0][0]["status"] == "completed"
    finally:
        runtime.conn.close()
