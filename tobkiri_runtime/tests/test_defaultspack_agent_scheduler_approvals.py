from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _reset_scheduler_singleton():
    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler._instance
    if scheduler is not None:
        for schedule_id in list(getattr(scheduler, "_timers", {}).keys()):
            scheduler._cancel_timer(schedule_id)
    Scheduler._instance = None


def _setup_approval_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(tmp_path / "safety" / "approval.sqlite3"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(tmp_path / "safety" / "approval.secret"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    return approval


class _HmacKey:
    def get_active_key(self):
        return "defaultspack-scheduler-authority-test-key-" + ("x" * 32)


def _setup_schedule_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))


def test_scheduler_chat_payload_preserves_task_message_for_provider_fallback():
    from domain.agent import scheduler as scheduler_module

    payload = scheduler_module._scheduler_chat_payload(
        conversation_id="conv-qa",
        content="Run the scheduled QA task.",
        task_cfg={
            "message": "Run the scheduled QA task.",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "agent_id": "browser_qa",
        },
        schedule_id="sched-qa",
        exec_id="sexec-qa",
        trigger="scheduled",
        params={"model": "xiaomi-token-plan-sgp/mimo-v2-omni"},
        tools=[],
    )

    assert payload["message"]["role"] == "user"
    assert payload["message"]["content"] == "Run the scheduled QA task."
    assert payload["message"]["metadata"]["scheduled_task_message"] == "Run the scheduled QA task."
    assert payload["message"]["metadata"]["source"] == "scheduler"


def _approval_required_response(
    approval,
    *,
    conversation_id: str,
    tool_name: str = "browser_use",
    pending_tool_name: str | None = None,
    operation: str = "browser.open_url",
    risk_level: str = "high",
    arguments: dict | None = None,
) -> dict:
    if arguments is None:
        arguments = {
            "url": "http://127.0.0.1:8766/chat",
            "profile_id": "default",
            "persistent": True,
            "target_app": "",
        }
    request = approval.create_approval_request(
        operation,
        risk_level,
        arguments,
        details={
            "tool_name": tool_name,
            "action": operation,
            "function_id": operation,
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": arguments,
        },
    )
    pending_tool_name = pending_tool_name or tool_name
    pending = {
        "tool_name": pending_tool_name,
        "tool_call_id": f"call_{pending_tool_name}",
        "action": operation,
        "operation": operation,
        "payload": arguments,
        "approval_required": True,
        "approval_request_id": request["request_id"],
        "request_id": request["request_id"],
        "expires_at": request["expires_at"],
    }
    return {
        "status": "ok",
        "data": {
            "id": "assistant-approval",
            "role": "assistant",
            "content": [{"type": "text", "text": "approval needed"}],
            "finish_reason": "approval_required",
            "metadata": {"pending_approval": pending},
        },
    }


def test_schedule_auto_approval_limit_accepts_unlimited_policy():
    from domain.agent.scheduler import _schedule_auto_approval_limit

    assert _schedule_auto_approval_limit({"tool_policy": {}}) == 3
    assert _schedule_auto_approval_limit({"tool_policy": {"schedule_auto_approve_max_followups": 0}}) == 0
    assert _schedule_auto_approval_limit({"tool_policy": {"schedule_auto_approve_max_followups": "unlimited"}}) is None
    assert _schedule_auto_approval_limit({"tool_policy": {"schedule_auto_approve_max_followups": None}}) is None
    assert _schedule_auto_approval_limit({"tool_policy": {"schedule_auto_approve_max_followups": 999}}) == 64


def test_scheduler_ensure_loaded_rearms_missing_active_timer(tmp_path, monkeypatch):
    _setup_schedule_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()

    schedule = scheduler.create_schedule(
        "interval",
        {"message": "keep testing", "conversation_id": "conv-mimo"},
        {"value": 30, "unit": "minutes"},
    )

    assert len(FakeTimer.created) == 1
    assert FakeTimer.created[0].started is True

    with scheduler._lock:
        missing = scheduler._timers.pop(schedule["id"])
    missing.cancel()

    scheduler.ensure_loaded()

    assert len(FakeTimer.created) == 2
    assert FakeTimer.created[1].started is True
    with scheduler._lock:
        assert scheduler._timers[schedule["id"]] is FakeTimer.created[1]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_ensure_loaded_rearms_dead_active_timer(tmp_path, monkeypatch):
    _setup_schedule_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()

    schedule = scheduler.create_schedule(
        "interval",
        {"message": "keep testing", "conversation_id": "conv-mimo"},
        {"value": 30, "unit": "minutes"},
    )
    dead_timer = FakeTimer.created[0]
    dead_timer.cancel()

    scheduler.ensure_loaded()

    assert len(FakeTimer.created) == 2
    assert FakeTimer.created[1].started is True
    with scheduler._lock:
        assert scheduler._timers[schedule["id"]] is FakeTimer.created[1]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_ensure_loaded_does_not_duplicate_live_active_timer(tmp_path, monkeypatch):
    _setup_schedule_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()

    schedule = scheduler.create_schedule(
        "interval",
        {"message": "keep testing", "conversation_id": "conv-mimo"},
        {"value": 30, "unit": "minutes"},
    )

    scheduler.ensure_loaded()
    scheduler.ensure_loaded()

    assert len(FakeTimer.created) == 1
    with scheduler._lock:
        assert scheduler._timers[schedule["id"]] is FakeTimer.created[0]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_reloads_and_rearms_when_schedule_dir_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import save_schedule

    def schedule_payload(schedule_id, message):
        return {
            "id": schedule_id,
            "name": message,
            "description": "",
            "type": "interval",
            "task": {"message": message, "conversation_id": "conv-mimo"},
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)

    first_dir = tmp_path / "first" / "schedules"
    second_dir = tmp_path / "second" / "schedules"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(first_dir))
    save_schedule(schedule_payload("sched-first", "first directory schedule"))

    scheduler = scheduler_module.Scheduler()
    scheduler.ensure_loaded()

    assert len(FakeTimer.created) == 1
    first_timer = FakeTimer.created[0]
    assert first_timer.args == ["sched-first"]
    assert first_timer.started is True

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(second_dir))
    save_schedule(schedule_payload("sched-second", "second directory schedule"))

    scheduler.ensure_loaded()

    assert first_timer.cancelled is True
    assert len(FakeTimer.created) == 2
    assert FakeTimer.created[1].args == ["sched-second"]
    assert FakeTimer.created[1].started is True
    with scheduler._lock:
        assert set(scheduler._schedules) == {"sched-second"}
        assert set(scheduler._timers) == {"sched-second"}

    scheduler.delete_schedule("sched-second")
    _reset_scheduler_singleton()


def test_scheduler_recovers_persisted_stale_manual_running_execution_and_can_trigger(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "manual retry done"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    schedule_id = "sched-stale-manual"
    stale_execution_id = "sexec-stale-manual"
    next_execution_at = "2099-01-01T00:00:00Z"
    save_schedule(
        {
            "id": schedule_id,
            "name": "Stale manual QA",
            "description": "",
            "type": "interval",
            "task": {"message": "keep testing", "conversation_id": "conv-mimo", "timeout": 600},
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": next_execution_at,
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": stale_execution_id,
                "schedule_id": schedule_id,
                "started_at": "2000-01-01T00:00:00Z",
                "trigger": "manual",
            },
            "running_started_at": "2000-01-01T00:00:00Z",
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        recovered = scheduler.get_schedule(schedule_id)

        assert "running_execution" not in recovered
        assert "running_started_at" not in recovered
        assert recovered["execution_count"] == 1
        assert recovered["last_executed_at"]
        assert recovered["next_execution_at"] == next_execution_at
        saved = load_schedule(schedule_id)
        assert "running_execution" not in saved
        assert saved["execution_count"] == 1

        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["execution_id"] == stale_execution_id
        assert entries[0]["trigger"] == "manual"
        assert entries[0]["status"] == "error"
        assert entries[0]["timeout_seconds"] == 600
        assert entries[0]["recovered_stale_running_execution"] is True
        assert "timed out after 600 seconds" in entries[0]["error"]

        retry = scheduler.trigger_now(schedule_id)

        assert retry["status"] == "completed"
        assert retry["result"] == "manual retry done"
        assert len(calls) == 1
        saved_after_retry = load_schedule(schedule_id)
        assert "running_execution" not in saved_after_retry
        assert saved_after_retry["execution_count"] == 2
        entries, total = load_history(schedule_id)
        assert total == 2
        assert entries[0]["status"] == "completed"
        assert entries[1]["execution_id"] == stale_execution_id
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()


def test_stale_scheduled_chat_recovery_appends_durable_error(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, save_schedule

    schedule_id = "sched-stale-chat"
    stale_execution_id = "sexec-stale-chat"
    scheduled_user = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run heartbeat.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule_id,
                "schedule_execution_id": stale_execution_id,
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    assert scheduled_user is not None

    save_schedule(
        {
            "id": schedule_id,
            "name": "Stale chat heartbeat",
            "description": "",
            "type": "interval",
            "task": {
                "message": "Run heartbeat.",
                "model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "conversation_id": conversation_id,
                "timeout": 600,
                "profile_id": "defaultspack.mimo_coding_company",
                "agent_id": "scheduler",
                "metadata": {"company_id": "mimo-coding-company"},
            },
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": stale_execution_id,
                "schedule_id": schedule_id,
                "started_at": "2000-01-01T00:00:00Z",
                "trigger": "scheduled",
                "timeout_seconds": 600,
            },
            "running_started_at": "2000-01-01T00:00:00Z",
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        assert scheduler.get_schedule(schedule_id)["execution_count"] == 1
        entries, total = load_history(schedule_id)
        assert total == 1
        history = entries[0]
        assert history["execution_id"] == stale_execution_id
        assert history["status"] == "error"
        assert history["conversation_id"] == conversation_id
        assert history["assistant_error_message_id"]

        stored = store.get_conversation(conversation_id)
        assistant = stored["messages"][-1]
        assert assistant["role"] == "assistant"
        assert assistant["parent_id"] == scheduled_user["id"]
        assert assistant["id"] == history["assistant_error_message_id"]
        assert assistant["finish_reason"] == "error"
        assert assistant["metadata"]["durable_scheduler_error"] is True
        assert assistant["metadata"]["schedule_execution_id"] == stale_execution_id
        assert "scheduled task timed out after 600 seconds" in assistant["raw_text"]
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_stale_scheduled_chat_recovery_ignores_empty_streaming_assistant(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, save_schedule

    schedule_id = "sched-stale-chat-streaming"
    stale_execution_id = "sexec-stale-chat-streaming"
    scheduled_user = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run scheduled browser QA.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule_id,
                "schedule_execution_id": stale_execution_id,
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    stuck_assistant = store.add_message(
        conversation_id,
        {
            "role": "assistant",
            "parent_id": scheduled_user["id"],
            "content": [],
            "raw_text": "",
            "finish_reason": "streaming",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule_id,
                "schedule_execution_id": stale_execution_id,
                "thinking": {"state": "running"},
            },
        },
    )

    save_schedule(
        {
            "id": schedule_id,
            "name": "Stale streaming chat QA",
            "description": "",
            "type": "interval",
            "task": {
                "message": "Run scheduled browser QA.",
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "conversation_id": conversation_id,
                "timeout": 1800,
                "profile_id": "defaultspack.mimo_coding_company",
                "agent_id": "browser_qa",
                "metadata": {"company_id": "mimo-coding-company"},
            },
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": stale_execution_id,
                "schedule_id": schedule_id,
                "started_at": "2000-01-01T00:00:00Z",
                "trigger": "scheduled",
                "timeout_seconds": 1800,
            },
            "running_started_at": "2000-01-01T00:00:00Z",
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        assert scheduler.get_schedule(schedule_id)["execution_count"] == 1
        entries, total = load_history(schedule_id)
        assert total == 1
        history = entries[0]
        assert history["status"] == "error"
        assert history["assistant_error_message_id"]
        assert history["assistant_error_message_id"] != stuck_assistant["id"]

        stored = store.get_conversation(conversation_id)
        assistant_children = [
            message
            for message in stored["messages"]
            if message["role"] == "assistant" and message["parent_id"] == scheduled_user["id"]
        ]
        assert [message["id"] for message in assistant_children] == [
            stuck_assistant["id"],
            history["assistant_error_message_id"],
        ]
        recovered = assistant_children[-1]
        assert recovered["finish_reason"] == "error"
        assert recovered["metadata"]["durable_scheduler_error"] is True
        assert recovered["metadata"]["schedule_execution_id"] == stale_execution_id
        assert "scheduled task timed out after 1800 seconds" in recovered["raw_text"]
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_scheduled_approval_followup_uses_fresh_approval_parent_when_current_node_is_stale(tmp_path, monkeypatch):
    _setup_schedule_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    conversation_id = conversation["id"]

    root = store.add_message(conversation_id, {"role": "user", "content": "root"})
    error_branch = store.add_message(
        conversation_id,
        {
            "role": "assistant",
            "parent_id": root["id"],
            "content": "old error",
            "finish_reason": "error",
        },
    )
    old_followup = store.add_message(
        conversation_id,
        {
            "role": "user",
            "parent_id": root["id"],
            "content": "old approval followup",
            "metadata": {"source": "scheduler_approval_followup", "schedule_id": "old-schedule"},
        },
    )
    stale_cancelled = store.add_message(
        conversation_id,
        {
            "role": "assistant",
            "parent_id": old_followup["id"],
            "content": "cancelled",
            "finish_reason": "cancelled",
            "metadata": {"cancelled": True},
        },
    )
    store.update_conversation(conversation_id, {"current_node_id": stale_cancelled["id"]})

    from domain.agent import scheduler as scheduler_module

    send_calls: list[dict] = []

    def fake_approve_schedule_pending_approval(task_cfg, pending, *, conversation_id):
        return {
            "summary": {"tool_name": "browser_use", "operation": "browser.open_url"},
            "followup": {"approved": True},
        }

    def fake_send_chat(payload, context):
        send_calls.append(payload)
        message = payload["message"]
        user_message = {
            "role": "user",
            "content": message["content"],
            "metadata": message.get("metadata"),
        }
        if "parent_id" in message:
            user_message["parent_id"] = message["parent_id"]
        user = store.add_message(payload["conversation_id"], user_message)
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        is_followup = metadata.get("source") == "scheduler_approval_followup"
        assistant = store.add_message(
            payload["conversation_id"],
            {
                "role": "assistant",
                "parent_id": user["id"],
                "content": "continued" if is_followup else "approval needed",
                "finish_reason": "stop" if is_followup else "approval_required",
                "metadata": (
                    {}
                    if is_followup
                    else {
                        "pending_approval": {
                            "tool_name": "browser_use",
                            "operation": "browser.open_url",
                            "approval_required": True,
                        }
                    }
                ),
            },
        )
        if not is_followup:
            store.update_conversation(payload["conversation_id"], {"current_node_id": stale_cancelled["id"]})
        return {"status": "ok", "data": assistant}

    monkeypatch.setattr(scheduler_module, "approve_schedule_pending_approval", fake_approve_schedule_pending_approval)
    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run scheduled task.",
            "model": "stub/default",
            "conversation_id": conversation_id,
            "tool_policy": {"schedule_auto_approve_max_followups": 1},
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    try:
        history = scheduler.trigger_now(schedule["id"])
        stored = store.get_conversation(conversation_id)
        scheduled_user = next(
            message
            for message in stored["messages"]
            if (message.get("metadata") or {}).get("source") == "scheduler"
            and (message.get("metadata") or {}).get("schedule_execution_id") == history["execution_id"]
        )
        approval_assistant = next(
            message
            for message in stored["messages"]
            if message.get("role") == "assistant"
            and message.get("parent_id") == scheduled_user["id"]
            and message.get("finish_reason") == "approval_required"
        )
        followup_user = next(
            message
            for message in stored["messages"]
            if (message.get("metadata") or {}).get("source") == "scheduler_approval_followup"
            and (message.get("metadata") or {}).get("schedule_execution_id") == history["execution_id"]
        )
        followup_assistant = next(
            message
            for message in stored["messages"]
            if message.get("role") == "assistant" and message.get("parent_id") == followup_user["id"]
        )

        assert history["status"] == "completed"
        assert scheduled_user["parent_id"] == stale_cancelled["id"]
        assert approval_assistant["parent_id"] == scheduled_user["id"]
        assert followup_user["parent_id"] == approval_assistant["id"]
        assert followup_assistant["parent_id"] == followup_user["id"]
        assert send_calls[1]["message"]["parent_id"] == approval_assistant["id"]
        assert stored["current_node_id"] == followup_assistant["id"]
        assert error_branch["id"] != followup_user["parent_id"]
        assert stale_cancelled["id"] != followup_user["parent_id"]
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_scheduler_recovers_active_stale_running_execution_once_when_original_unwinds(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []
    calls_lock = threading.Lock()
    first_call_started = threading.Event()
    first_call_release = threading.Event()

    def fake_complete(payload, context):
        del context
        with calls_lock:
            calls.append(payload)
            index = len(calls)
        if index == 1:
            first_call_started.set()
            first_call_release.wait(timeout=5)
            return {"status": "ok", "data": {"content": "late first run"}}
        return {"status": "ok", "data": {"content": "manual retry done"}}

    monkeypatch.setattr("blocks.ai.complete.run", fake_complete)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = None
    original_timestamp = scheduler_module.timestamp
    try:
        schedule = scheduler.create_schedule(
            "interval",
            {"message": "keep testing", "model": "stub/default", "timeout": 600},
            {"value": 30, "unit": "minutes"},
        )

        ids = iter(["active-stale", "manual-retry"])
        monkeypatch.setattr(scheduler_module, "gen_id", lambda: next(ids))
        first_started_at = "2000-01-01T00:00:00Z"
        timestamp_values = iter([first_started_at])

        def fake_timestamp():
            try:
                return next(timestamp_values)
            except StopIteration:
                return original_timestamp()

        monkeypatch.setattr(scheduler_module, "timestamp", fake_timestamp)

        first_result: dict[str, dict | None] = {"history": None}

        def run_first():
            first_result["history"] = scheduler._execute_task(schedule["id"], manual=False)

        first_thread = threading.Thread(target=run_first)
        first_thread.start()
        assert first_call_started.wait(timeout=2)

        active_execution_id = "sexec_active-stale"
        with scheduler._lock:
            assert active_execution_id in scheduler._active_execution_ids

        assert scheduler._recover_stale_running_execution(schedule["id"]) is True

        recovered = load_schedule(schedule["id"])
        assert "running_execution" not in recovered
        assert "running_started_at" not in recovered
        assert recovered["execution_count"] == 1

        entries, total = load_history(schedule["id"])
        assert total == 1
        assert entries[0]["execution_id"] == active_execution_id
        assert entries[0]["status"] == "error"
        assert entries[0]["timeout_seconds"] == 600
        assert entries[0]["recovered_stale_running_execution"] is True

        retry = scheduler.trigger_now(schedule["id"])

        assert retry["status"] == "completed"
        assert retry["result"] == "manual retry done"

        first_call_release.set()
        first_thread.join(timeout=2)
        assert not first_thread.is_alive()

        saved = load_schedule(schedule["id"])
        assert "running_execution" not in saved
        assert saved["execution_count"] == 2
        entries, total = load_history(schedule["id"])
        assert total == 2
        assert entries[0]["execution_id"] == "sexec_manual-retry"
        assert entries[0]["status"] == "completed"
        assert entries[1]["execution_id"] == active_execution_id
        assert entries[1]["recovered_stale_running_execution"] is True
        with calls_lock:
            assert len(calls) == 2
        with scheduler._lock:
            assert active_execution_id not in scheduler._active_execution_ids
            assert active_execution_id not in scheduler._stale_recovered_execution_ids
    finally:
        first_call_release.set()
        if "first_thread" in locals():
            first_thread.join(timeout=2)
        if schedule is not None:
            scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_trigger_now_skips_when_schedule_has_active_running_execution(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "should not run"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    schedule_id = "sched-active-manual"
    active_execution_id = "sexec-active-manual"
    started_at = scheduler_module.timestamp()
    next_execution_at = "2099-01-01T00:00:00Z"
    save_schedule(
        {
            "id": schedule_id,
            "name": "Active manual QA",
            "description": "",
            "type": "interval",
            "task": {"message": "keep testing", "conversation_id": "conv-mimo", "timeout": 600},
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": next_execution_at,
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": active_execution_id,
                "schedule_id": schedule_id,
                "started_at": started_at,
                "trigger": "manual",
                "timeout_seconds": 600,
            },
            "running_started_at": started_at,
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        history = scheduler.trigger_now(schedule_id)

        assert history["status"] == "skipped"
        assert history["skipped_reason"] == "already_running"
        assert history["trigger"] == "manual"
        assert active_execution_id in history["error"]
        assert history["running_execution"]["execution_id"] == active_execution_id
        assert calls == []

        saved = load_schedule(schedule_id)
        assert saved["running_execution"]["execution_id"] == active_execution_id
        assert saved["running_execution"]["started_at"] == started_at
        assert saved["running_execution"]["trigger"] == "manual"
        assert saved["running_started_at"] == started_at
        assert saved["execution_count"] == 0
        assert saved["next_execution_at"] == next_execution_at
        entries, total = load_history(schedule_id)
        assert entries == []
        assert total == 0
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()


def test_task_update_obsoletes_active_running_execution_and_allows_retry(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "new prompt completed"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "old QA prompt with bare /chat",
            "model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
            "conversation_id": "conv-mimo",
            "timeout": 1800,
            "tool_policy": {"schedule_initial_tool_choice": "required"},
        },
        {"value": 30, "unit": "minutes"},
    )
    schedule_id = schedule["id"]
    active_execution_id = "sexec-active-old-input"
    started_at = scheduler_module.timestamp()
    persisted = load_schedule(schedule_id)
    persisted["running_execution"] = {
        "execution_id": active_execution_id,
        "schedule_id": schedule_id,
        "started_at": started_at,
        "trigger": "scheduled",
        "timeout_seconds": 1800,
    }
    persisted["running_started_at"] = started_at
    save_schedule(persisted)
    with scheduler._lock:
        scheduler._schedules[schedule_id] = persisted

    try:
        scheduler.update_schedule(
            schedule_id,
            {
                "task": {
                    "message": "new QA prompt with http://127.0.0.1:18766/chat?chat=conv-mimo",
                    "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                    "tool_policy": {
                        "schedule_initial_tool_choice": "required",
                        "schedule_auto_approve_tool_requests": True,
                    },
                }
            },
        )

        updated = load_schedule(schedule_id)
        assert updated["running_execution"]["execution_id"] == active_execution_id
        assert updated["running_execution"]["obsolete_reason"] == "execution_input_changed"
        assert updated["running_execution"]["input_fingerprint"]

        retry = scheduler.trigger_now(schedule_id)

        assert retry["status"] == "completed"
        assert retry["result"] == "new prompt completed"
        assert len(calls) == 1
        assert calls[0]["message"]["content"] == "new QA prompt with http://127.0.0.1:18766/chat?chat=conv-mimo"
        assert calls[0]["params"]["model"] == "xiaomi-token-plan-sgp/mimo-v2-omni"

        saved = load_schedule(schedule_id)
        assert "running_execution" not in saved
        assert "running_started_at" not in saved
        assert saved["execution_count"] == 2

        entries, total = load_history(schedule_id)
        assert total == 2
        assert entries[0]["status"] == "completed"
        assert entries[1]["execution_id"] == active_execution_id
        assert entries[1]["status"] == "obsolete"
        assert entries[1]["obsolete_reason"] == "execution_input_changed"
        assert entries[1]["recovered_obsolete_running_execution"] is True
        assert entries[1]["error"] is None
        assert "recovered_stale_running_execution" not in entries[1]
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()


def test_started_at_running_execution_message_mismatch_does_not_touch_chat_store(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.chat.store import ChatStore

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]
    schedule_id = "sched-legacy-obsolete-chat"
    active_execution_id = "sexec-legacy-bare-chat"
    scheduled_user = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run QA against http://127.0.0.1:18766/chat",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule_id,
                "schedule_execution_id": active_execution_id,
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    chat_loads: list[str] = []
    original_get_conversation = ChatStore.get_conversation

    def fail_if_chat_loaded(self, requested_conversation_id):
        del self
        chat_loads.append(requested_conversation_id)
        raise AssertionError("active started_at running execution should not load ChatStore")

    monkeypatch.setattr(ChatStore, "get_conversation", fail_if_chat_loaded)
    started_at = scheduler_module.timestamp()
    save_schedule(
        {
            "id": schedule_id,
            "name": "Legacy bare chat QA",
            "description": "",
            "type": "interval",
            "task": {
                "message": "Run QA against http://127.0.0.1:18766/chat?chat=" + conversation_id,
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "conversation_id": conversation_id,
                "timeout": 1800,
                "profile_id": "defaultspack.mimo_coding_company",
                "agent_id": "browser_qa",
                "metadata": {"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
            },
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-06-30T00:00:00Z",
            "updated_at": started_at,
            "running_execution": {
                "execution_id": active_execution_id,
                "schedule_id": schedule_id,
                "started_at": started_at,
                "trigger": "scheduled",
                "timeout_seconds": 1800,
            },
            "running_started_at": started_at,
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        active = scheduler.get_schedule(schedule_id)

        assert active["running_execution"]["execution_id"] == active_execution_id
        assert active["execution_count"] == 0
        entries, total = load_history(schedule_id)
        assert total == 0

        saved = load_schedule(schedule_id)
        assert saved["running_execution"]["execution_id"] == active_execution_id
        assert chat_loads == []
        stored = original_get_conversation(store, conversation_id)
        assert len(stored["messages"]) == 1
        assert stored["messages"][0]["id"] == scheduled_user["id"]
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_legacy_running_execution_without_started_at_message_mismatch_obsoletes(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.chat.store import ChatStore

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]
    schedule_id = "sched-legacy-no-start-chat"
    legacy_execution_id = "sexec-legacy-no-start-chat"
    scheduled_user = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run QA against http://127.0.0.1:18766/chat",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule_id,
                "schedule_execution_id": legacy_execution_id,
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    save_schedule(
        {
            "id": schedule_id,
            "name": "Legacy no-start chat QA",
            "description": "",
            "type": "interval",
            "task": {
                "message": "Run QA against http://127.0.0.1:18766/chat?chat=" + conversation_id,
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "conversation_id": conversation_id,
                "timeout": 1800,
                "profile_id": "defaultspack.mimo_coding_company",
                "agent_id": "browser_qa",
                "metadata": {"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
            },
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "not-a-date",
            "updated_at": "not-a-date",
            "running_execution": {
                "execution_id": legacy_execution_id,
                "schedule_id": schedule_id,
                "trigger": "scheduled",
                "timeout_seconds": 1800,
            },
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        recovered = scheduler.get_schedule(schedule_id)

        assert "running_execution" not in recovered
        assert recovered["execution_count"] == 1
        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["execution_id"] == legacy_execution_id
        assert entries[0]["status"] == "obsolete"
        assert entries[0]["obsolete_reason"] == "execution_input_message_changed"
        assert entries[0]["scheduled_user_message_id"] == scheduled_user["id"]
        assert entries[0]["recovered_obsolete_running_execution"] is True
        assert "recovered_stale_running_execution" not in entries[0]

        saved = load_schedule(schedule_id)
        assert "running_execution" not in saved
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_legacy_running_execution_message_mismatch_recovers_after_timeout(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.chat.store import ChatStore

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]
    schedule_id = "sched-legacy-stale-chat"
    stale_execution_id = "sexec-legacy-stale-chat"
    scheduled_user = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run QA against http://127.0.0.1:18766/chat",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule_id,
                "schedule_execution_id": stale_execution_id,
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    save_schedule(
        {
            "id": schedule_id,
            "name": "Legacy stale bare chat QA",
            "description": "",
            "type": "interval",
            "task": {
                "message": "Run QA against http://127.0.0.1:18766/chat?chat=" + conversation_id,
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "conversation_id": conversation_id,
                "timeout": 1800,
                "profile_id": "defaultspack.mimo_coding_company",
                "agent_id": "browser_qa",
                "metadata": {"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
            },
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-06-30T00:00:00Z",
            "updated_at": "2026-06-30T00:00:00Z",
            "running_execution": {
                "execution_id": stale_execution_id,
                "schedule_id": schedule_id,
                "started_at": "2000-01-01T00:00:00Z",
                "trigger": "scheduled",
                "timeout_seconds": 1800,
            },
            "running_started_at": "2000-01-01T00:00:00Z",
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        recovered = scheduler.get_schedule(schedule_id)

        assert "running_execution" not in recovered
        assert recovered["execution_count"] == 1
        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["execution_id"] == stale_execution_id
        assert entries[0]["status"] == "error"
        assert entries[0]["timeout_seconds"] == 1800
        assert entries[0]["recovered_stale_running_execution"] is True
        assert "recovered_obsolete_running_execution" not in entries[0]

        saved = load_schedule(schedule_id)
        assert "running_execution" not in saved
        stored = store.get_conversation(conversation_id)
        assert len(stored["messages"]) == 2
        assert stored["messages"][0]["id"] == scheduled_user["id"]
        assert stored["messages"][1]["finish_reason"] == "error"
        assert stored["messages"][1]["metadata"]["schedule_execution_id"] == stale_execution_id
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_approval_followup_message_does_not_obsolete_running_execution(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.chat.store import ChatStore

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]
    schedule_id = "sched-approval-followup-active"
    active_execution_id = "sexec-approval-followup-active"
    current_message = "Run QA against http://127.0.0.1:18766/chat?chat=" + conversation_id
    store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Continue this approved scheduled task.\n\nScheduled task: " + current_message,
            "metadata": {
                "source": "scheduler_approval_followup",
                "schedule_id": schedule_id,
                "schedule_execution_id": active_execution_id,
                "trigger": "approval_followup",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    started_at = scheduler_module.timestamp()
    save_schedule(
        {
            "id": schedule_id,
            "name": "Approval followup QA",
            "description": "",
            "type": "interval",
            "task": {
                "message": current_message,
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "conversation_id": conversation_id,
                "timeout": 1800,
                "profile_id": "defaultspack.mimo_coding_company",
                "agent_id": "browser_qa",
                "metadata": {"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
            },
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2099-01-01T00:00:00Z",
            "created_at": "2026-06-30T00:00:00Z",
            "updated_at": started_at,
            "running_execution": {
                "execution_id": active_execution_id,
                "schedule_id": schedule_id,
                "started_at": started_at,
                "trigger": "manual",
                "timeout_seconds": 1800,
            },
            "running_started_at": started_at,
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        active = scheduler.get_schedule(schedule_id)

        assert active["running_execution"]["execution_id"] == active_execution_id
        assert active["execution_count"] == 0
        entries, total = load_history(schedule_id)
        assert total == 0
        saved = load_schedule(schedule_id)
        assert saved["running_execution"]["execution_id"] == active_execution_id
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_manual_trigger_fails_fast_when_conversation_is_busy_without_running_marker(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "should not start"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {"message": "manual check", "conversation_id": "conv-busy", "timeout": 30},
        {"run_at": "2099-01-01T00:00:00Z"},
    )
    conversation_lock = scheduler._conversation_execution_lock("conv-busy")
    assert conversation_lock is not None
    assert conversation_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        history = scheduler.trigger_now(schedule["id"])
        elapsed = time.monotonic() - started
    finally:
        conversation_lock.release()

    try:
        assert elapsed < 1
        assert calls == []
        assert history["status"] == "error"
        assert history["error_code"] == "CONVERSATION_RUNNING"
        assert history["skipped_reason"] == "conversation_running"
        assert "conv-busy" in history["error"]

        saved = load_schedule(schedule["id"])
        assert "running_execution" not in saved
        assert "running_started_at" not in saved
        assert saved["execution_count"] == 1
        assert saved["last_executed_at"] == history["completed_at"]

        entries, total = load_history(schedule["id"])
        assert total == 1
        assert entries[0]["execution_id"] == history["execution_id"]
        assert entries[0]["status"] == "error"
        assert entries[0]["error_code"] == "CONVERSATION_RUNNING"
        assert entries[0]["skipped_reason"] == "conversation_running"
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_manual_mimo_trigger_clears_running_when_chat_fails_before_model(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []
    schedule_id_ref: dict[str, str] = {}
    observed_running: dict[str, object] = {}

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        persisted = load_schedule(schedule_id_ref["schedule_id"])
        observed_running.update(persisted.get("running_execution") or {})
        raise RuntimeError("provider setup failed before model.invoke")

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)
    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)

    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run scheduled MiMo desktop QA",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "conversation_id": "conv-mimo-startup-failure",
            "timeout": 30,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "desktop_qa",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )
    schedule_id_ref["schedule_id"] = schedule["id"]

    try:
        history = scheduler.trigger_now(schedule["id"])

        assert len(calls) == 1
        assert observed_running["schedule_id"] == schedule["id"]
        assert observed_running["trigger"] == "manual"
        assert observed_running["execution_id"] == history["execution_id"]
        assert history["status"] == "error"
        assert history["trigger"] == "manual"
        assert "provider setup failed before model.invoke" in history["error"]
        assert history["conversation_id"] == "conv-mimo-startup-failure"

        saved = load_schedule(schedule["id"])
        assert "running_execution" not in saved
        assert "running_started_at" not in saved
        assert saved["execution_count"] == 1
        assert saved["last_executed_at"] == history["completed_at"]

        entries, total = load_history(schedule["id"])
        assert total == 1
        assert entries[0]["execution_id"] == history["execution_id"]
        assert entries[0]["status"] == "error"
        assert entries[0]["trigger"] == "manual"
        assert "provider setup failed before model.invoke" in entries[0]["error"]
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_scheduled_conversation_lock_contention_skips_quickly_without_count_spam(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "should not start"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "scheduled heartbeat",
            "conversation_id": "conv-shared",
            "timeout": 600,
            "tool_policy": {"schedule_conversation_lock_wait_seconds": 0.01},
        },
        {"value": 30, "unit": "minutes"},
    )
    conversation_lock = scheduler._conversation_execution_lock("conv-shared")
    assert conversation_lock is not None
    assert conversation_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        first = scheduler._execute_task(schedule["id"], manual=False)
        second = scheduler._execute_task(schedule["id"], manual=False)
        elapsed = time.monotonic() - started
    finally:
        conversation_lock.release()

    try:
        assert elapsed < 1
        assert calls == []
        assert first["status"] == "skipped"
        assert first["error_code"] == "CONVERSATION_RUNNING"
        assert first["skipped_reason"] == "conversation_running"
        assert first["conversation_id"] == "conv-shared"
        assert second["status"] == "skipped"
        assert second["skipped_reason"] == "conversation_running"

        saved = load_schedule(schedule["id"])
        assert "running_execution" not in saved
        assert "running_started_at" not in saved
        assert saved["execution_count"] == 0
        assert saved["last_executed_at"] is None
        assert saved["next_execution_at"]

        entries, total = load_history(schedule["id"])
        assert total == 1
        assert entries[0]["status"] == "skipped"
        assert entries[0]["error_code"] == "CONVERSATION_RUNNING"
        assert entries[0]["skipped_reason"] == "conversation_running"
        assert entries[0]["conversation_id"] == "conv-shared"
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_scheduled_execution_releases_orphaned_conversation_lock_when_running_cleared(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "orphan recovered"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "scheduled heartbeat",
            "conversation_id": "conv-orphan",
            "timeout": 600,
            "tool_policy": {"schedule_conversation_lock_wait_seconds": 0.01},
        },
        {"value": 30, "unit": "minutes"},
    )
    old_execution_id = "sexec-orphaned-holder"
    old_cancel_event = threading.Event()
    conversation_lock = scheduler._conversation_execution_lock("conv-orphan")
    assert conversation_lock is not None
    assert conversation_lock.acquire(blocking=False)
    with scheduler._lock:
        scheduler._active_execution_ids.add(old_execution_id)
    scheduler._set_conversation_lock_holder(
        "conv-orphan",
        schedule_id=schedule["id"],
        execution_id=old_execution_id,
        started_at="2026-06-30T00:00:00Z",
        timeout_seconds=2592000,
        trigger="scheduled",
        cancel_event=old_cancel_event,
        orphan_releasable=True,
    )

    try:
        history = scheduler._execute_task(schedule["id"], manual=False)

        assert old_cancel_event.is_set()
        assert history["status"] == "completed"
        assert history["result"] == "orphan recovered"
        assert len(calls) == 1
        assert calls[0]["message"]["content"] == "scheduled heartbeat"

        saved = load_schedule(schedule["id"])
        assert "running_execution" not in saved
        assert "running_started_at" not in saved
        assert saved["execution_count"] == 1
        entries, total = load_history(schedule["id"])
        assert total == 1
        assert entries[0]["status"] == "completed"
        with scheduler._lock:
            assert "conv-orphan" not in scheduler._conversation_lock_holders
    finally:
        with scheduler._lock:
            scheduler._active_execution_ids.discard(old_execution_id)
            scheduler._stale_recovered_execution_ids.discard(old_execution_id)
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_orphan_recovery_keeps_matching_active_conversation_lock(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {"message": "scheduled heartbeat", "conversation_id": "conv-active", "timeout": 600},
        {"value": 30, "unit": "minutes"},
    )
    active_execution_id = "sexec-active-holder"
    started_at = scheduler_module.timestamp()
    persisted = load_schedule(schedule["id"])
    persisted["running_execution"] = {
        "execution_id": active_execution_id,
        "schedule_id": schedule["id"],
        "started_at": started_at,
        "trigger": "scheduled",
        "timeout_seconds": 600,
    }
    persisted["running_started_at"] = started_at
    save_schedule(persisted)
    with scheduler._lock:
        scheduler._schedules[schedule["id"]] = persisted
        scheduler._active_execution_ids.add(active_execution_id)
    conversation_lock = scheduler._conversation_execution_lock("conv-active")
    assert conversation_lock is not None
    assert conversation_lock.acquire(blocking=False)
    scheduler._set_conversation_lock_holder(
        "conv-active",
        schedule_id=schedule["id"],
        execution_id=active_execution_id,
        started_at=started_at,
        timeout_seconds=600,
        trigger="scheduled",
        cancel_event=threading.Event(),
        orphan_releasable=True,
    )

    try:
        assert scheduler._release_orphaned_conversation_lock("conv-active") is False
        assert conversation_lock.acquire(blocking=False) is False
    finally:
        scheduler._release_conversation_execution_lock("conv-active", conversation_lock, active_execution_id)
        with scheduler._lock:
            scheduler._active_execution_ids.discard(active_execution_id)
            scheduler._stale_recovered_execution_ids.discard(active_execution_id)
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_timer_skips_active_running_execution_without_overwriting_or_chat_send(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "should not run"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    schedule_id = "sched-active-timer"
    active_execution_id = "sexec-active-timer"
    started_at = scheduler_module.timestamp()
    overdue_next = "2000-01-01T00:00:00Z"
    save_schedule(
        {
            "id": schedule_id,
            "name": "Active timer QA",
            "description": "",
            "type": "interval",
            "task": {"message": "keep testing", "conversation_id": "conv-mimo", "timeout": 600},
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": overdue_next,
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": active_execution_id,
                "schedule_id": schedule_id,
                "started_at": started_at,
                "trigger": "manual",
                "timeout_seconds": 600,
            },
            "running_started_at": started_at,
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        scheduler.ensure_loaded()
        initial_timer_count = len(FakeTimer.created)

        scheduler._on_timer_fire(schedule_id)

        assert calls == []
        saved = load_schedule(schedule_id)
        assert saved["running_execution"]["execution_id"] == active_execution_id
        assert saved["running_execution"]["started_at"] == started_at
        assert saved["running_execution"]["trigger"] == "manual"
        assert saved["running_started_at"] == started_at
        assert saved["execution_count"] == 0
        assert saved["last_executed_at"] is None
        assert saved["next_execution_at"] != overdue_next
        assert saved["next_execution_at"]
        assert len(FakeTimer.created) == initial_timer_count + 1
        assert FakeTimer.created[-1].args == [schedule_id]
        assert FakeTimer.created[-1].started is True

        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["status"] == "skipped"
        assert entries[0]["skipped_reason"] == "already_running"
        assert entries[0]["trigger"] == "scheduled"
        assert entries[0]["running_execution"]["execution_id"] == active_execution_id
        assert active_execution_id in entries[0]["error"]
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()


def test_timer_refreshes_persisted_running_execution_before_starting_chat(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "should not run"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "scheduled QA heartbeat",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "conversation_id": conversation_id,
            "timeout": 1800,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
        },
        {"value": 30, "unit": "minutes"},
    )
    schedule_id = schedule["id"]
    active_execution_id = "sexec-persisted-manual"
    started_at = scheduler_module.timestamp()
    persisted = load_schedule(schedule_id)
    persisted["running_execution"] = {
        "execution_id": active_execution_id,
        "schedule_id": schedule_id,
        "started_at": started_at,
        "trigger": "manual",
        "timeout_seconds": 1800,
    }
    persisted["running_started_at"] = started_at
    save_schedule(persisted)

    try:
        with scheduler._lock:
            assert "running_execution" not in scheduler._schedules[schedule_id]
        initial_timer_count = len(FakeTimer.created)

        scheduler._on_timer_fire(schedule_id)

        assert calls == []
        stored = store.get_conversation(conversation_id)
        assert stored["messages"] == []

        saved = load_schedule(schedule_id)
        assert saved["running_execution"]["execution_id"] == active_execution_id
        assert saved["running_execution"]["trigger"] == "manual"
        assert saved["running_started_at"] == started_at
        assert saved["execution_count"] == 0
        assert saved["last_executed_at"] is None
        assert saved["next_execution_at"]
        assert len(FakeTimer.created) == initial_timer_count + 1
        assert FakeTimer.created[-1].args == [schedule_id]
        assert FakeTimer.created[-1].started is True

        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["status"] == "skipped"
        assert entries[0]["skipped_reason"] == "already_running"
        assert entries[0]["running_execution"]["execution_id"] == active_execution_id
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_once_timer_skip_for_active_running_execution_stays_active_and_retries(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        del context
        calls.append(payload)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "should not run"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    schedule_id = "sched-once-active-timer"
    active_execution_id = "sexec-once-active-timer"
    started_at = scheduler_module.timestamp()
    overdue_next = "2000-01-01T00:00:00Z"
    save_schedule(
        {
            "id": schedule_id,
            "name": "Active once timer QA",
            "description": "",
            "type": "once",
            "task": {"message": "keep testing", "conversation_id": "conv-mimo", "timeout": 600},
            "config": {"run_at": overdue_next},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": overdue_next,
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": active_execution_id,
                "schedule_id": schedule_id,
                "started_at": started_at,
                "trigger": "manual",
                "timeout_seconds": 600,
            },
            "running_started_at": started_at,
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        scheduler.ensure_loaded()
        initial_timer_count = len(FakeTimer.created)

        scheduler._on_timer_fire(schedule_id)

        assert calls == []
        saved = load_schedule(schedule_id)
        assert saved["status"] == "active"
        assert saved["running_execution"]["execution_id"] == active_execution_id
        assert saved["running_execution"]["started_at"] == started_at
        assert saved["running_execution"]["trigger"] == "manual"
        assert saved["running_started_at"] == started_at
        assert saved["execution_count"] == 0
        assert saved["last_executed_at"] is None
        assert saved["next_execution_at"] != overdue_next
        assert saved["next_execution_at"]
        assert len(FakeTimer.created) == initial_timer_count + 1
        assert FakeTimer.created[-1].args == [schedule_id]
        assert FakeTimer.created[-1].started is True

        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["status"] == "skipped"
        assert entries[0]["skipped_reason"] == "already_running"
        assert entries[0]["trigger"] == "scheduled"
        assert entries[0]["running_execution"]["execution_id"] == active_execution_id
        assert active_execution_id in entries[0]["error"]
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()


def test_timer_suppresses_duplicate_already_running_history_for_same_execution(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    schedule_id = "sched-active-duplicate"
    active_execution_id = "sexec-active-duplicate"
    started_at = scheduler_module.timestamp()
    save_schedule(
        {
            "id": schedule_id,
            "name": "Active duplicate QA",
            "description": "",
            "type": "interval",
            "task": {"message": "keep testing", "conversation_id": "conv-mimo", "timeout": 600},
            "config": {"value": 30, "unit": "minutes"},
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": "2000-01-01T00:00:00Z",
            "created_at": "2026-06-28T15:00:00Z",
            "updated_at": "2026-06-28T15:53:36Z",
            "running_execution": {
                "execution_id": active_execution_id,
                "schedule_id": schedule_id,
                "started_at": started_at,
                "trigger": "scheduled",
                "timeout_seconds": 600,
            },
            "running_started_at": started_at,
        }
    )

    scheduler = scheduler_module.Scheduler()
    try:
        scheduler.ensure_loaded()
        first = scheduler._execute_task(schedule_id, manual=False)
        second = scheduler._execute_task(schedule_id, manual=False)

        assert first["status"] == "skipped"
        assert second["status"] == "skipped"
        assert first["skipped_reason"] == "already_running"
        assert second["skipped_reason"] == "already_running"
        entries, total = load_history(schedule_id)
        assert total == 1
        assert entries[0]["running_execution"]["execution_id"] == active_execution_id
    finally:
        scheduler.delete_schedule(schedule_id)
        _reset_scheduler_singleton()


def test_scheduler_marks_interval_running_until_task_completes(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        created = []

        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False
            FakeTimer.created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    started = threading.Event()
    release = threading.Event()

    def fake_send_chat(payload, context):
        started.set()
        assert release.wait(5)
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "interval done"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)
    from domain.agent import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()

    schedule = scheduler.create_schedule(
        "interval",
        {"message": "keep testing", "conversation_id": "conv-mimo"},
        {"value": 30, "unit": "minutes"},
    )

    worker = threading.Thread(target=scheduler._on_timer_fire, args=(schedule["id"],))
    worker.start()
    assert started.wait(5)

    running = scheduler.get_schedule(schedule["id"])
    assert running["running_execution"]["execution_id"].startswith("sexec_")
    assert running["running_execution"]["schedule_id"] == schedule["id"]
    assert running["running_execution"]["trigger"] == "scheduled"
    assert running["running_started_at"] == running["running_execution"]["started_at"]

    release.set()
    worker.join(5)
    assert not worker.is_alive()

    completed = scheduler.get_schedule(schedule["id"])
    assert "running_execution" not in completed
    assert "running_started_at" not in completed
    assert completed["execution_count"] == 1
    assert completed["last_executed_at"]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduled_execution_persists_completion_and_next_time_together(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    def fake_send_chat(payload, context):
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "interval done"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)
    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {"message": "keep testing", "conversation_id": "conv-mimo"},
        {"value": 30, "unit": "minutes"},
    )
    with scheduler._lock:
        scheduler._schedules[schedule["id"]]["next_execution_at"] = "2000-01-01T00:00:00Z"
        save_schedule(scheduler._schedules[schedule["id"]])

    scheduler._execute_task(schedule["id"], manual=False)

    saved = load_schedule(schedule["id"])
    history, total = load_history(schedule["id"])
    assert total == 1
    assert history[0]["status"] == "completed"
    assert "running_execution" not in saved
    assert "running_started_at" not in saved
    assert saved["execution_count"] == 1
    assert saved["last_executed_at"] == history[0]["completed_at"]
    assert saved["next_execution_at"] != "2000-01-01T00:00:00Z"
    assert saved["next_execution_at"]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_times_out_conversation_run_and_allows_next_interval(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    calls: list[dict] = []
    contexts: list[dict] = []
    calls_lock = threading.Lock()
    first_call_started = threading.Event()
    first_call_release = threading.Event()
    first_call_finished = threading.Event()

    def fake_send_chat(payload, context):
        with calls_lock:
            calls.append(payload)
            contexts.append(context)
            index = len(calls)
        if index == 1:
            first_call_started.set()
            try:
                first_call_release.wait(timeout=5)
            finally:
                first_call_finished.set()
            return {
                "status": "ok",
                "data": {
                    "id": "assistant-late",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "late first run"}],
                    "finish_reason": "stop",
                    "metadata": {},
                },
            }
        return {
            "status": "ok",
            "data": {
                "id": "assistant-next",
                "role": "assistant",
                "content": [{"type": "text", "text": "next interval done"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = None
    try:
        schedule = scheduler.create_schedule(
            "interval",
            {"message": "keep testing", "conversation_id": "conv-mimo", "timeout": 0.2},
            {"value": 30, "unit": "minutes"},
        )
        stale_next = "2000-01-01T00:00:00Z"
        with scheduler._lock:
            scheduler._schedules[schedule["id"]]["next_execution_at"] = stale_next
            save_schedule(scheduler._schedules[schedule["id"]])

        first_history = scheduler._execute_task(schedule["id"], manual=False)

        assert first_call_started.wait(timeout=1)
        assert first_history["status"] == "error"
        assert "timed out after 0.2 seconds" in first_history["error"]
        assert first_history["timeout_seconds"] == 0.2
        with calls_lock:
            assert callable(contexts[0].get("is_cancelled"))
            assert contexts[0]["is_cancelled"]() is True
            assert calls[0]["params"]["request_timeout"] == 2.0
        saved_after_timeout = load_schedule(schedule["id"])
        assert "running_execution" not in saved_after_timeout
        assert "running_started_at" not in saved_after_timeout
        assert saved_after_timeout["execution_count"] == 1
        assert saved_after_timeout["last_executed_at"] == first_history["completed_at"]
        assert saved_after_timeout["next_execution_at"] != stale_next
        assert not first_call_finished.is_set()

        second_history = scheduler._execute_task(schedule["id"], manual=False)

        assert second_history["status"] == "completed"
        assert second_history["result"] == "next interval done"
        with calls_lock:
            assert len(calls) == 2
        saved_after_second = load_schedule(schedule["id"])
        assert "running_execution" not in saved_after_second
        assert saved_after_second["execution_count"] == 2

        entries, total = load_history(schedule["id"])
        assert total == 2
        assert entries[0]["status"] == "completed"
        assert entries[1]["status"] == "error"
        assert entries[1]["timeout_seconds"] == 0.2
    finally:
        first_call_release.set()
        first_call_finished.wait(timeout=1)
        if schedule is not None:
            scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_scheduler_times_out_ai_complete_run_and_clears_running(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    class FakeTimer:
        def __init__(self, delay, callback, args=None):
            self.delay = delay
            self.callback = callback
            self.args = args or []
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    complete_started = threading.Event()
    complete_release = threading.Event()
    complete_finished = threading.Event()
    captured: dict[str, object] = {}

    def fake_complete(payload, context):
        captured["payload"] = payload
        captured["context"] = context
        complete_started.set()
        try:
            complete_release.wait(timeout=5)
        finally:
            complete_finished.set()
        return {"status": "ok", "data": {"content": "late complete"}}

    monkeypatch.setattr("blocks.ai.complete.run", fake_complete)

    from domain.agent import scheduler as scheduler_module
    from domain.agent.schedule_store import load_history, load_schedule

    monkeypatch.setattr(scheduler_module.threading, "Timer", FakeTimer)
    scheduler = scheduler_module.Scheduler()
    schedule = None
    try:
        schedule = scheduler.create_schedule(
            "interval",
            {"message": "summarize", "model": "stub/default", "timeout": 0.2},
            {"value": 30, "unit": "minutes"},
        )

        history = scheduler._execute_task(schedule["id"], manual=False)

        assert complete_started.wait(timeout=1)
        assert history["status"] == "error"
        assert "timed out after 0.2 seconds" in history["error"]
        assert history["timeout_seconds"] == 0.2
        assert captured["payload"]["params"]["request_timeout"] == 2.0
        assert callable(captured["context"].get("is_cancelled"))
        assert captured["context"]["is_cancelled"]() is True
        saved = load_schedule(schedule["id"])
        assert "running_execution" not in saved
        assert "running_started_at" not in saved
        assert saved["execution_count"] == 1
        entries, total = load_history(schedule["id"])
        assert total == 1
        assert entries[0]["status"] == "error"
        assert entries[0]["timeout_seconds"] == 0.2
    finally:
        complete_release.set()
        complete_finished.wait(timeout=1)
        if schedule is not None:
            scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_scheduler_ai_complete_uses_profile_authority_context(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    captured: dict[str, object] = {}

    def fake_complete(payload, context):
        captured["payload"] = payload
        captured["context"] = context
        return {"status": "ok", "data": {"content": "done"}}

    monkeypatch.setattr("blocks.ai.complete.run", fake_complete)

    from domain.agent import scheduler as scheduler_module

    scheduler = scheduler_module.Scheduler()
    schedule = None
    try:
        schedule = scheduler.create_schedule(
            "interval",
            {
                "message": "visual QA",
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "timeout": 30,
                "profile_id": "defaultspack.mimo_coding_company",
                "metadata": {"company_id": "mimo-coding-company"},
            },
            {"value": 30, "unit": "minutes"},
        )

        history = scheduler._execute_task(schedule["id"], manual=False)

        assert history["status"] == "completed"
        assert captured["payload"]["model"] == "xiaomi-token-plan-sgp/mimo-v2-omni"
        assert captured["payload"]["params"]["request_timeout"] == 25.0
        assert captured["context"]["profile_id"] == "defaultspack.mimo_coding_company"
        assert captured["context"]["authority_principal_id"] == "profile:defaultspack.mimo_coding_company"
        assert captured["context"]["principal_id"] == "profile:defaultspack.mimo_coding_company"
        assert captured["context"]["source"] == "scheduler"
    finally:
        if schedule is not None:
            scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()


def test_scheduler_auto_approves_mimo_scheduled_browser_request(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            return _approval_required_response(approval, conversation_id="conv-mimo")
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "opened and inspected"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run browser QA.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["browser_use"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_initial_tool_choice": "required",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["browser_use"],
                "schedule_auto_approve_max_followups": 2,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "opened and inspected"
    assert len(calls) == 2
    assert calls[0]["payload"]["params"]["tool_choice"] == "required"
    assert "tool_choice" not in calls[1]["payload"]["params"]
    followup_content = calls[1]["payload"]["message"]["content"]
    assert "Scheduled task:" in followup_content
    assert "Run browser QA." in followup_content
    assert "Approved tool request:" in followup_content
    assert calls[1]["payload"]["message"]["metadata"]["scheduled_task_message"] == "Run browser QA."
    followup = calls[1]["payload"]["message"]["metadata"]["approval_followup"]
    assert followup["tool_name"] == "browser_use"
    assert followup["request_id"].startswith("apr_")
    assert followup["approval_token"]
    assert followup["arguments"] == {
        "url": "http://127.0.0.1:8766/chat",
        "profile_id": "default",
        "persistent": True,
        "target_app": "",
    }
    assert history["auto_approvals"] == [
        {
            "request_id": followup["request_id"],
            "tool_name": "browser_use",
            "operation": "browser.open_url",
            "status": "approved",
        }
    ]
    assert "approval_token" not in history["auto_approvals"][0]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_auto_approved_browser_computer_followup_includes_inline_arguments(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []
    screenshot_args = {"action": "computer.screenshot", "payload": {"detail": "high"}}

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            return _approval_required_response(
                approval,
                conversation_id="conv-mimo",
                tool_name="browser_computer",
                operation="computer.screenshot",
                risk_level="high",
                arguments=screenshot_args,
            )
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "screenshot captured"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Capture a browser computer screenshot.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["browser_computer"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["browser_computer"],
                "schedule_auto_approve_max_followups": 2,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert len(calls) == 2
    followup = calls[1]["payload"]["message"]["metadata"]["approval_followup"]
    assert followup["tool_name"] == "browser_computer"
    assert followup["operation"] == "computer.screenshot"
    assert followup["arguments"] == screenshot_args
    assert "approval_token" not in followup["arguments"]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_resumes_mimo_request_already_approved_by_manager(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []
    approval_args = {"path": "notes.txt", "content": "approved by manager"}
    external_approval: dict[str, str] = {}

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            response = _approval_required_response(
                approval,
                conversation_id="conv-mimo",
                tool_name="coding_file_write",
                operation="tool.coding_file_write",
                risk_level="medium",
                arguments=approval_args,
            )
            request_id = response["data"]["metadata"]["pending_approval"]["request_id"]
            decision = approval.approve(request_id)
            assert decision["approved"] is True
            external_approval.update({"request_id": request_id, "token": decision["token"]})
            assert approval.get_approval_request(request_id)["status"] == "approved"
            return response
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "approved write completed"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Write the scheduled note.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "self_improvement",
            "tools": ["coding_file_write"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["coding_file_write"],
                "schedule_auto_approve_max_followups": "unlimited",
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "approved write completed"
    assert len(calls) == 2
    followup = calls[1]["payload"]["message"]["metadata"]["approval_followup"]
    assert followup["request_id"] == external_approval["request_id"]
    assert followup["approval_token"]
    assert followup["approval_token"] != external_approval["token"]
    assert history["auto_approvals"] == [
        {
            "request_id": external_approval["request_id"],
            "tool_name": "coding_file_write",
            "operation": "tool.coding_file_write",
            "status": "approved",
        }
    ]

    verification = approval.verify_execution_token(
        followup["approval_token"],
        "tool.coding_file_write",
        approval.hash_arguments(approval_args),
        consume=True,
        pack_id="defaultspack",
        conversation_id="conv-mimo",
    )
    assert verification.valid is True
    assert approval.get_approval_request(external_approval["request_id"])["status"] == "consumed"

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_auto_approves_mimo_scheduled_todo_request(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            return _approval_required_response(
                approval,
                conversation_id="conv-mimo",
                tool_name="todo",
                operation="tool.todo",
                risk_level="medium",
                arguments={"action": "list"},
            )
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "todo list checked and task continued"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run MiMo QA loop.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "qa_loop",
            "tools": ["todo"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["todo"],
                "schedule_auto_approve_max_followups": 2,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "todo list checked and task continued"
    assert len(calls) == 2
    followup = calls[1]["payload"]["message"]["metadata"]["approval_followup"]
    assert followup["tool_name"] == "todo"
    assert followup["operation"] == "tool.todo"
    assert followup["approval_token"]
    assert history["auto_approvals"] == [
        {
            "request_id": followup["request_id"],
            "tool_name": "todo",
            "operation": "tool.todo",
            "status": "approved",
        }
    ]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_replay_refreshes_expired_auto_approval_token_for_todo(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    from domain.agent.scheduler import _scheduler_chat_context
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor
    from domain.tool.scheduled_approval import approve_schedule_pending_approval

    conversation_id = "conv-mimo"
    approval_args = {"action": "list"}
    request = approval.create_approval_request(
        "tool.todo",
        "medium",
        approval_args,
        details={
            "tool_name": "todo",
            "action": "tool.todo",
            "function_id": "tool.todo",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approval_args,
        },
    )
    pending = {
        "tool_name": "todo",
        "tool_call_id": "call-todo-initial",
        "action": "tool.todo",
        "operation": "tool.todo",
        "payload": approval_args,
        "arguments": approval_args,
        "approval_required": True,
        "approval_request_id": request["request_id"],
        "request_id": request["request_id"],
        "expires_at": request["expires_at"],
    }
    task_cfg = {
        "message": "Check the todo list heartbeat.",
        "profile_id": "defaultspack.mimo_coding_company",
        "tool_policy": {
            "profile_id": "defaultspack.mimo_coding_company",
            "schedule_auto_approve_tool_requests": True,
            "schedule_auto_approve_tool_allowlist": ["todo"],
        },
        "metadata": {
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    }
    real_extended_approve = approval.approve_with_extended_expiry
    approve_calls: list[dict] = []
    refresh_calls: list[dict] = []
    expired_followup_token: dict[str, str] = {}

    def approve_with_expired_first_token(request_id, **kwargs):
        decision = dict(real_extended_approve(request_id, **kwargs))
        if not approve_calls and decision.get("approved"):
            approve_calls.append(dict(decision))
            stored = approval.get_approval_request(request_id)
            details = stored.get("details") if isinstance(stored, dict) else {}
            expired = approval.issue_execution_token(
                request_id,
                str(stored.get("args_hash") or ""),
                expires_at=approval._now() - 10,
                operation=str(stored.get("operation") or ""),
                function_id=str(details.get("function_id") or details.get("action") or ""),
                pack_id=str(details.get("pack_id") or ""),
                conversation_id=str(details.get("conversation_id") or ""),
            )
            expired_followup_token["token"] = expired
            decision["token"] = expired
            decision["expires_at"] = approval._now() - 10
        elif decision.get("approved"):
            refresh_calls.append(dict(decision))
        return decision

    monkeypatch.setattr(approval, "approve_with_extended_expiry", approve_with_expired_first_token)
    approved = approve_schedule_pending_approval(task_cfg, pending, conversation_id=conversation_id)
    assert approved is not None
    followup = approved["followup"]
    assert followup["approval_token"] == expired_followup_token["token"]

    invocations: list[dict] = []

    def fake_execute(self, tool_name, arguments, context):
        del self
        args = dict(arguments or {})
        tokens = context.get("tool_approval_tokens") if isinstance(context, dict) else {}
        context_token = ""
        if isinstance(tokens, dict):
            context_token = str(tokens.get("todo") or tokens.get("tool.todo") or "").strip()
        token = str(context_token or args.get("approval_token") or "").strip()
        verification = approval.verify_execution_token(
            token,
            "tool.todo",
            approval.hash_arguments(approval_args),
            consume=True,
        )
        invocations.append(
            {
                "tool_name": tool_name,
                "arguments": args,
                "used_token": token,
                "valid_token": verification.valid,
                "token_code": verification.code,
            }
        )
        return {
            "result": "todo list returned open tasks" if verification.valid else "Approval token expired before `todo list` could execute",
            "is_error": not verification.valid,
            "widget": {"type": "todo", "items": []} if verification.valid else None,
        }

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "schedule_id": "sched-mimo",
        "approval_followup": followup,
    }
    tool_context = _scheduler_chat_context(task_cfg)
    tool_context["tool_approval_tokens"] = {
        "todo": followup["approval_token"],
        "tool.todo": followup["approval_token"],
        followup["request_id"]: followup["approval_token"],
    }
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": []},
        input_data={},
        request_id="req-mimo",
        content=[],
        metadata=metadata,
        user_message={"id": "user-followup", "role": "user", "metadata": metadata},
        model="stub/default",
        params={},
        request_context=dict(tool_context),
        tool_context=tool_context,
        standard_messages=[],
        user_text="Continue this approved scheduled task.",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "todo"}],
        tools_called=[],
        connected_tool_names={"todo"},
        call_handler=None,
        model_routing={},
    )

    engine = ChatRunEngine.__new__(ChatRunEngine)
    engine._run_id = "run-mimo"
    engine._conversation_id = conversation_id
    engine._event_seq = 0
    engine._activity_events = []
    engine._tool_logs = []
    engine._text_parts = []
    engine._thinking_transcript_parts = []
    engine._started_tool_call_ids = set()
    engine._cancel_event = threading.Event()
    engine._external_cancel_checker = None
    engine._stream_mode = False

    replay = engine._replay_approval_followup_if_present(prepared, [], prepared.chat_ir, None)
    while True:
        try:
            next(replay)
        except StopIteration as stop:
            blocked = stop.value
            break

    assert blocked is None
    assert len(approve_calls) == 1
    assert len(refresh_calls) == 1
    assert len(invocations) == 1
    assert invocations[0]["valid_token"] is True
    assert invocations[0]["used_token"]
    assert invocations[0]["used_token"] != expired_followup_token["token"]
    assert prepared.tool_context["tool_approval_tokens"]["todo"] == invocations[0]["used_token"]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"


def test_scheduler_auto_approval_renews_expired_request_before_followup_replay(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine, _suppress_duplicate_approval_replay_tool_uses
    from domain.tool.executor import ToolExecutor
    from domain.tool.scheduled_approval import approve_schedule_pending_approval

    conversation_id = "conv-mimo"
    approval_args: dict = {}
    request = approval.create_approval_request(
        "tool.desktop_list",
        "medium",
        approval_args,
        details={
            "tool_name": "desktop_list",
            "action": "tool.desktop_list",
            "function_id": "tool.desktop_list",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approval_args,
        },
    )
    stored = approval._REQUESTS[request["request_id"]]
    stored.status = "expired"
    stored.expires_at = approval._now() - 10
    stored.decision_at = approval._now()
    approval.get_approval_store().save_request(stored)

    pending = {
        "tool_name": "desktop_list",
        "tool_call_id": "call-desktop-list",
        "action": "tool.desktop_list",
        "operation": "tool.desktop_list",
        "payload": approval_args,
        "arguments": approval_args,
        "approval_required": True,
        "approval_request_id": request["request_id"],
        "request_id": request["request_id"],
        "expires_at": request["expires_at"],
    }
    task_cfg = {
        "message": "List managed desktops.",
        "profile_id": "defaultspack.mimo_coding_company",
        "tool_policy": {
            "profile_id": "defaultspack.mimo_coding_company",
            "schedule_auto_approve_tool_requests": True,
            "schedule_auto_approve_tool_allowlist": ["desktop_list"],
        },
        "metadata": {
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    }

    approved = approve_schedule_pending_approval(task_cfg, pending, conversation_id=conversation_id)
    assert approved is not None
    followup = approved["followup"]
    renewed = approval.get_approval_request(request["request_id"])
    assert renewed["status"] == "approved"
    assert renewed["expires_at"] > approval._now()

    invocations: list[dict] = []

    def fake_execute(self, tool_name, arguments, context):
        del self, context
        args = dict(arguments or {})
        verification = approval.verify_execution_token(
            str(args.get("approval_token") or ""),
            "tool.desktop_list",
            approval.hash_arguments(approval_args),
            consume=True,
        )
        invocations.append({"tool_name": tool_name, "valid": verification.valid})
        return {
            "result": "desktop list returned one seat",
            "is_error": not verification.valid,
            "widget": {"desktops": [{"seat_id": "seat-1", "status": "running"}]},
        }

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    metadata = {
        "source": "scheduler_approval_followup",
        "profile_id": "defaultspack.mimo_coding_company",
        "approval_followup": followup,
    }
    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop_list",
                "description": "desktop_list",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    prepared = PreparedChatRun(
        conversation_id=conversation_id,
        conversation={"id": conversation_id, "messages": []},
        input_data={},
        request_id="req-mimo",
        content=[],
        metadata=metadata,
        user_message={"id": "user-followup", "role": "user", "metadata": metadata},
        model="stub/default",
        params={},
        request_context={"source": "scheduler_approval_followup", "profile_id": "defaultspack.mimo_coding_company"},
        tool_context={"tool_approval_tokens": {"desktop_list": followup["approval_token"]}},
        standard_messages=[],
        user_text="Continue this approved scheduled task.",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["desktop_list"],
        connected_tool_names={"desktop_list"},
        call_handler=None,
        model_routing={},
    )

    engine = ChatRunEngine.__new__(ChatRunEngine)
    engine._run_id = "run-mimo"
    engine._conversation_id = conversation_id
    engine._event_seq = 0
    engine._activity_events = []
    engine._tool_logs = []
    engine._text_parts = []
    engine._thinking_transcript_parts = []
    engine._started_tool_call_ids = set()
    engine._cancel_event = threading.Event()
    engine._external_cancel_checker = None
    engine._stream_mode = False

    replay = engine._replay_approval_followup_if_present(prepared, [], prepared.chat_ir, None)
    while True:
        try:
            next(replay)
        except StopIteration as stop:
            blocked = stop.value
            break

    assert blocked is None
    assert invocations == [{"tool_name": "desktop_list", "valid": True}]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"

    duplicate = {
        "type": "tool_use",
        "id": "call-duplicate",
        "name": "desktop_list",
        "input": {"include_destroyed": True},
    }
    _response, filtered = _suppress_duplicate_approval_replay_tool_uses(
        prepared,
        {"content": [duplicate], "finish_reason": "tool_calls"},
        [duplicate],
    )
    assert filtered == []


def test_scheduled_approval_obsoletes_superseded_followup_token(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    from domain.chat.store import ChatStore
    from domain.tool.scheduled_approval import obsolete_superseded_scheduled_approvals

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        metadata={
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    )
    conversation_id = conversation["id"]
    approval_args = {}
    request = approval.create_approval_request(
        "tool.desktop_list",
        "medium",
        approval_args,
        details={
            "tool_name": "desktop_list",
            "action": "tool.desktop_list",
            "function_id": "tool.desktop_list",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approval_args,
        },
    )
    token = approval.approve(request["request_id"])["token"]
    scheduler_user = store.add_message(
        conversation_id,
        {
            "id": "user-scheduler",
            "role": "user",
            "content": "Run managed desktop QA.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": "sched-mimo",
                "schedule_execution_id": "exec-1",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    approval_assistant = store.add_message(
        conversation_id,
        {
            "id": "assistant-approval",
            "role": "assistant",
            "parent_id": scheduler_user["id"],
            "content": "approval required",
            "finish_reason": "approval_required",
            "metadata": {
                "pending_approval": {
                    "tool_name": "desktop_list",
                    "operation": "tool.desktop_list",
                    "approval_required": True,
                    "request_id": request["request_id"],
                    "approval_request_id": request["request_id"],
                }
            },
        },
    )
    followup_user = store.add_message(
        conversation_id,
        {
            "id": "user-followup",
            "role": "user",
            "parent_id": approval_assistant["id"],
            "content": "Continue approved scheduled task.",
            "metadata": {
                "source": "scheduler_approval_followup",
                "schedule_id": "sched-mimo",
                "schedule_execution_id": "exec-1",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "approval_followup": {
                    "request_id": request["request_id"],
                    "approval_request_id": request["request_id"],
                    "approval_token": token,
                    "tool_name": "desktop_list",
                },
            },
        },
    )
    new_scheduler_user = store.add_message(
        conversation_id,
        {
            "id": "user-new-scheduler-turn",
            "role": "user",
            "parent_id": followup_user["id"],
            "content": "Run managed desktop QA again.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": "sched-mimo",
                "schedule_execution_id": "exec-2",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    store.update_conversation(conversation_id, {"current_node_id": new_scheduler_user["id"]})

    result = obsolete_superseded_scheduled_approvals([conversation_id], set())

    assert result["obsolete_count"] == 1
    stored = approval.get_approval_request(request["request_id"])
    assert stored["status"] == "obsolete"
    assert stored["details"]["obsolete_reason"] == "superseded_scheduled_approval_followup"
    verification = approval.verify_execution_token(
        token,
        "tool.desktop_list",
        approval.hash_arguments(approval_args),
        consume=False,
    )
    assert verification.valid is False
    assert verification.code == "APPROVAL_NOT_APPROVED"
    ChatStore._instance = None


def test_scheduled_approval_keeps_current_approved_request_recoverable(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    from domain.chat.store import ChatStore
    from domain.tool.scheduled_approval import obsolete_superseded_scheduled_approvals

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        metadata={
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    )
    conversation_id = conversation["id"]
    approval_args = {"action": "list"}
    request = approval.create_approval_request(
        "tool.todo",
        "medium",
        approval_args,
        details={
            "tool_name": "todo",
            "action": "tool.todo",
            "function_id": "tool.todo",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": approval_args,
        },
    )
    approval.approve(request["request_id"])
    scheduler_user = store.add_message(
        conversation_id,
        {
            "id": "user-scheduler",
            "role": "user",
            "content": "Run todo heartbeat.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": "sched-mimo",
                "schedule_execution_id": "exec-1",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    approval_assistant = store.add_message(
        conversation_id,
        {
            "id": "assistant-current-approval",
            "role": "assistant",
            "parent_id": scheduler_user["id"],
            "content": "approval required",
            "finish_reason": "approval_required",
            "metadata": {
                "pending_approval": {
                    "tool_name": "todo",
                    "operation": "tool.todo",
                    "approval_required": True,
                    "request_id": request["request_id"],
                    "approval_request_id": request["request_id"],
                }
            },
        },
    )
    store.update_conversation(conversation_id, {"current_node_id": approval_assistant["id"]})

    result = obsolete_superseded_scheduled_approvals(
        [conversation_id],
        {request["request_id"]},
    )

    assert result["obsolete_count"] == 0
    assert approval.get_approval_request(request["request_id"])["status"] == "approved"
    ChatStore._instance = None


def test_mimo_schedule_auto_approves_camelcase_mimo_provider_authority_request(tmp_path, monkeypatch):
    from tests.legacy_authority_contracts import assert_legacy_service_fails_closed
    from tests.v4_batch_support import assert_lease_is_single_use, harness

    assert_legacy_service_fails_closed()
    authority = harness(tmp_path)
    assert_lease_is_single_use(authority)


def test_scheduler_auto_approves_display_name_tool_requests(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            return _approval_required_response(
                approval,
                conversation_id="conv-mimo",
                tool_name="Desktop List",
                pending_tool_name="desktop_list",
                operation="tool.Desktop List",
                risk_level="medium",
                arguments={},
            )
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "desktop list checked"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "List managed desktops.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["desktop_list"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["desktop_list"],
                "schedule_auto_approve_max_followups": 2,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "desktop list checked"
    assert len(calls) == 2
    assert calls[0]["context"]["owner_pack"] == "defaultspack"
    assert calls[1]["context"]["owner_pack"] == "defaultspack"
    assert history["auto_approvals"] == [
        {
            "request_id": calls[1]["payload"]["message"]["metadata"]["approval_followup"]["request_id"],
            "tool_name": "desktop_list",
            "operation": "tool.Desktop List",
            "status": "approved",
        }
    ]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_auto_approves_mimo_request_from_approval_requested_event_data(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            arguments = {}
            request = approval.create_approval_request(
                "tool.desktop_list",
                "medium",
                arguments,
                details={
                    "tool_name": "desktop_list",
                    "action": "tool.desktop_list",
                    "function_id": "tool.desktop_list",
                    "pack_id": "defaultspack",
                    "conversation_id": "conv-mimo",
                    "arguments": arguments,
                },
            )
            pending = {
                "tool_name": "desktop_list",
                "tool_call_id": "call_desktop_list",
                "action": "tool.desktop_list",
                "operation": "tool.desktop_list",
                "payload": arguments,
                "approval_required": True,
                "approval_request_id": request["request_id"],
                "request_id": request["request_id"],
                "expires_at": request["expires_at"],
            }
            return {
                "status": "ok",
                "data": {
                    "id": "assistant-approval",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "approval needed"}],
                    "finish_reason": "approval_required",
                    "metadata": {},
                    "events": [
                        {
                            "type": "approval_requested",
                            "phase": "approval_requested",
                            "data": pending,
                        }
                    ],
                },
            }
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "desktop list completed"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "List managed desktops.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["desktop_list"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["desktop_list"],
                "schedule_auto_approve_max_followups": 2,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "desktop list completed"
    assert len(calls) == 2
    followup = calls[1]["payload"]["message"]["metadata"]["approval_followup"]
    assert followup["tool_name"] == "desktop_list"
    assert followup["operation"] == "tool.desktop_list"
    assert followup["approval_token"]
    assert history["auto_approvals"] == [
        {
            "request_id": followup["request_id"],
            "tool_name": "desktop_list",
            "operation": "tool.desktop_list",
            "status": "approved",
        }
    ]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_consumes_auto_approved_desktop_list_before_model_can_reask(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _setup_schedule_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore
    from domain.tool.executor import ToolExecutor
    import domain.chat.stream_engine as engine_module

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        metadata={
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    )
    conversation_id = conversation["id"]

    class FakeGateway:
        def __init__(self):
            self.complete_requests = []

        def complete(self, request_data):
            self.complete_requests.append(request_data)
            if len(self.complete_requests) == 1:
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-desktop-list-initial",
                            "name": "desktop_list",
                            "input": {},
                        }
                    ],
                    "finish_reason": "tool_calls",
                    "usage": {},
                }
            return {
                "content": [
                    {"type": "text", "text": "desktop list already available"},
                    {
                        "type": "tool_use",
                        "id": "call-desktop-list-duplicate",
                        "name": "desktop_list",
                        "input": {"include_destroyed": True},
                    },
                ],
                "finish_reason": "tool_calls",
                "usage": {},
            }

        def stream(self, request_data):
            del request_data
            return iter([])

        def supports_stream(self, model):
            del model
            return False

        def resolve_provider(self, model):
            class Provider:
                pass

            return Provider(), model

    gateway = FakeGateway()
    monkeypatch.setattr(engine_module, "LLMGateway", lambda client=None: gateway)

    invocations: list[dict] = []
    approval_request_ids: list[str] = []

    def fake_execute(self, tool_name, arguments, context):
        del self, context
        args = dict(arguments or {})
        invocations.append({"tool_name": tool_name, "arguments": args})
        token = str(args.get("approval_token") or "").strip()
        if token:
            verification = approval.verify_execution_token(
                token,
                "tool.desktop_list",
                approval.hash_arguments({}),
                consume=True,
            )
            assert verification.valid is True, getattr(verification, "message", verification)
            return {
                "result": "desktop list returned one seat",
                "is_error": False,
                "widget": {"desktops": [{"seat_id": "seat-1", "status": "running"}]},
            }

        approval_args = {key: value for key, value in args.items() if key != "approval_token"}
        request = approval.create_approval_request(
            "tool.desktop_list",
            "medium",
            approval_args,
            details={
                "tool_name": "desktop_list",
                "action": "tool.desktop_list",
                "function_id": "tool.desktop_list",
                "pack_id": "defaultspack",
                "conversation_id": conversation_id,
                "arguments": approval_args,
            },
        )
        approval_request_ids.append(request["request_id"])
        return {
            "result": "Tool 'desktop_list' requires approval",
            "is_error": False,
            "widget": {
                "type": "approval_request",
                "tool_name": "desktop_list",
                "approval_required": True,
                "requires_approval": True,
                "risk_level": "medium",
                "operation": "tool.desktop_list",
                "action": "tool.desktop_list",
                "arguments": approval_args,
                "payload": approval_args,
                "approval_request_id": request["request_id"],
                "expires_at": request["expires_at"],
                "display_summary": request["display_summary"],
            },
        }

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "List managed desktops.",
            "model": "stub/default",
            "conversation_id": conversation_id,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["desktop_list"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["desktop_list"],
                "schedule_auto_approve_max_followups": 1,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    try:
        history = scheduler.trigger_now(schedule["id"])

        assert history["status"] == "completed"
        assert history["result"] == "desktop list already available"
        assert len(gateway.complete_requests) == 2
        assert len(invocations) == 2
        assert invocations[0] == {"tool_name": "desktop_list", "arguments": {}}
        assert invocations[1]["tool_name"] == "desktop_list"
        assert set(invocations[1]["arguments"]) == {"approval_token"}
        assert invocations[1]["arguments"]["approval_token"]
        assert len(approval_request_ids) == 1
        assert approval.get_approval_request(approval_request_ids[0])["status"] == "consumed"
        assert history["auto_approvals"] == [
            {
                "request_id": approval_request_ids[0],
                "tool_name": "desktop_list",
                "operation": "tool.desktop_list",
                "status": "approved",
            }
        ]
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_scheduler_unlimited_auto_approves_repeated_rumi_api_get_desktop_frame_requests(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    approval_count = 66
    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) <= approval_count:
            seat_id = f"seat-{len(calls)}"
            return _approval_required_response(
                approval,
                conversation_id="conv-mimo",
                tool_name="rumi_api",
                operation="tool.rumi_api",
                risk_level="high",
                arguments={
                    "action": "request",
                    "method": "GET",
                    "path": f"/api/desktops/{seat_id}/frame",
                },
            )
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "desktop frames inspected"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Keep inspecting the desktop frames.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["rumi_api"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["GET /api/desktops/{id}/frame"],
                "schedule_auto_approve_max_followups": "unlimited",
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "desktop frames inspected"
    assert len(calls) == approval_count + 1
    assert len(history["auto_approvals"]) == approval_count
    assert history["auto_approvals"][0]["tool_name"] == "rumi_api"
    assert history["auto_approvals"][0]["operation"] == "GET /api/desktops/{id}/frame"
    assert history["auto_approvals"][-1]["operation"] == "GET /api/desktops/{id}/frame"
    assert "approval_token" not in history["auto_approvals"][0]
    followup = calls[1]["payload"]["message"]["metadata"]["approval_followup"]
    assert followup["tool_name"] == "rumi_api"
    assert followup["operation"] == "tool.rumi_api"
    assert followup["approval_token"]

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_does_not_auto_approve_post_frame_when_get_frame_is_allowlisted(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        return _approval_required_response(
            approval,
            conversation_id="conv-mimo",
            tool_name="rumi_api",
            operation="tool.rumi_api",
            risk_level="high",
            arguments={
                "action": "request",
                "method": "POST",
                "path": "/api/desktops/seat-1/frame",
            },
        )

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Inspect a desktop frame.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["rumi_api"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["GET /api/desktops/{id}/frame"],
                "schedule_auto_approve_max_followups": "unlimited",
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "approval_required"
    assert len(calls) == 1
    assert "auto_approvals" not in history

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_does_not_auto_approve_rumi_api_post_frame_from_desktop_frame_alias(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        return _approval_required_response(
            approval,
            conversation_id="conv-mimo",
            tool_name="rumi_api",
            operation="tool.rumi_api",
            risk_level="high",
            arguments={
                "action": "request",
                "method": "POST",
                "path": "/api/desktops/seat-1/frame",
            },
        )

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Inspect a desktop frame.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["rumi_api"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["desktop_frame"],
                "schedule_auto_approve_max_followups": "unlimited",
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "approval_required"
    assert len(calls) == 1
    assert "auto_approvals" not in history

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduler_auto_approves_route_listing_without_broad_rumi_api_allowlist(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        if len(calls) == 1:
            return _approval_required_response(
                approval,
                conversation_id="conv-mimo",
                tool_name="rumi_api",
                operation="tool.rumi_api",
                risk_level="medium",
                arguments={"action": "list_routes"},
            )
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "routes checked"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "List routes.",
            "model": "stub/default",
            "conversation_id": "conv-mimo",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["rumi_api"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["rumi_api:list_routes"],
                "schedule_auto_approve_max_followups": 2,
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "completed"
    assert history["result"] == "routes checked"
    assert len(calls) == 2
    assert history["auto_approvals"][0]["tool_name"] == "rumi_api"
    assert history["auto_approvals"][0]["operation"] == "tool.rumi_api"

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_mimo_status_recovers_externally_approved_scheduled_approval(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        metadata={
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    )
    conversation_id = conversation["id"]

    from domain.agent.scheduler import Scheduler
    from domain.agent.schedule_store import load_history

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run browser QA.",
            "model": "stub/default",
            "conversation_id": conversation_id,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["browser_use"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_initial_tool_choice": "required",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["browser_use"],
                "schedule_auto_approve_max_followups": "unlimited",
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": conversation_id,
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )
    approval_response = _approval_required_response(
        approval,
        conversation_id=conversation_id,
        tool_name="browser_use",
        operation="tool.browser_use",
    )
    pending = approval_response["data"]["metadata"]["pending_approval"]
    request_id = pending["request_id"]
    scheduler_message = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run browser QA.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule["id"],
                "schedule_execution_id": "sexec-original",
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    approval_message = store.add_message(conversation_id, approval_response["data"])
    assert scheduler_message is not None
    assert approval_message is not None
    store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "A later stored branch message should not be treated as current.",
            "metadata": {"source": "manual"},
        },
    )
    store.update_conversation(conversation_id, {"current_node_id": approval_message["id"]})

    external_decision = approval.approve(request_id)
    assert external_decision["approved"] is True

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "continued after approval"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from ecosystem.rumi_operations_team_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    try:
        runtime = MimoCodingCompanyRuntime()
        schedules = runtime._schedules_for_state(
            {"schedule_ids": {"qa_loop": schedule["id"]}},
            recover_scheduled_approvals=True,
        )

        assert [item["id"] for item in schedules] == [schedule["id"]]
        assert len(calls) == 1
        payload = calls[0]["payload"]
        metadata = payload["message"]["metadata"]
        assert metadata["source"] == "scheduler_approval_followup"
        assert metadata["schedule_id"] == schedule["id"]
        assert metadata["schedule_execution_id"] == "sexec-original"
        assert metadata["approval_followup"]["request_id"] == request_id
        assert metadata["approval_followup"]["approval_token"]
        assert "tool_choice" not in payload["params"]
        assert calls[0]["context"]["owner_pack"] == "defaultspack"

        entries, total = load_history(schedule["id"])
        assert total == 1
        history = entries[0]
        assert history["status"] == "completed"
        assert history["result"] == "continued after approval"
        assert history["recovered_scheduled_approval"] is True
        assert history["recovered_execution_id"] == "sexec-original"
        assert history["auto_approvals"] == [
            {
                "request_id": request_id,
                "tool_name": "browser_use",
                "operation": "tool.browser_use",
                "status": "approved",
            }
        ]
        assert "approval_token" not in history["auto_approvals"][0]
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_scheduler_does_not_duplicate_existing_scheduled_approval_followup(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore
    from domain.agent.scheduler import Scheduler

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        metadata={
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    )
    conversation_id = conversation["id"]

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run browser QA.",
            "model": "stub/default",
            "conversation_id": conversation_id,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["browser_use"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["browser_use"],
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    approval_response = _approval_required_response(
        approval,
        conversation_id=conversation_id,
        tool_name="browser_use",
        operation="tool.browser_use",
    )
    pending = approval_response["data"]["metadata"]["pending_approval"]
    request_id = pending["request_id"]
    scheduler_message = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Run browser QA.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule["id"],
                "schedule_execution_id": "sexec-original",
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    approval_message = store.add_message(
        conversation_id,
        {
            **approval_response["data"],
            "parent_id": scheduler_message["id"],
        },
    )
    decision = approval.approve(request_id)
    assert decision["approved"] is True
    store.add_message(
        conversation_id,
        {
            "role": "user",
            "parent_id": approval_message["id"],
            "content": "Continue this approved scheduled task.",
            "metadata": {
                "source": "scheduler_approval_followup",
                "schedule_id": schedule["id"],
                "schedule_execution_id": "sexec-original",
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "approval_followup": {
                    "tool_name": "browser_use",
                    "request_id": request_id,
                    "approval_token": decision["token"],
                },
            },
        },
    )
    store.update_conversation(conversation_id, {"current_node_id": approval_message["id"]})

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        return {"status": "ok", "data": {"role": "assistant", "content": "duplicated", "finish_reason": "stop"}}

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    try:
        result = scheduler.recover_scheduled_chat_approval(schedule["id"])

        assert result["status"] == "no_current_approval"
        assert result["continued_count"] == 0
        assert calls == []
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_mimo_status_recovers_scheduled_desktop_frame_approval_card(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
        metadata={
            "profile_id": "defaultspack.mimo_coding_company",
            "company_id": "mimo-coding-company",
        },
    )
    conversation_id = conversation["id"]

    from domain.agent.schedule_store import load_history
    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Inspect the managed desktop frame.",
            "model": "stub/default",
            "conversation_id": conversation_id,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["desktop_frame"],
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["desktop_frame"],
                "schedule_auto_approve_max_followups": "unlimited",
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": conversation_id,
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )
    arguments = {"seat_id": "seat-1"}
    request = approval.create_approval_request(
        "tool.desktop_frame",
        "medium",
        arguments,
        details={
            "tool_name": "Desktop Frame",
            "action": "tool.desktop_frame",
            "function_id": "tool.desktop_frame",
            "pack_id": "defaultspack",
            "conversation_id": conversation_id,
            "arguments": arguments,
        },
    )
    scheduler_message = store.add_message(
        conversation_id,
        {
            "role": "user",
            "content": "Inspect the managed desktop frame.",
            "metadata": {
                "source": "scheduler",
                "schedule_id": schedule["id"],
                "schedule_execution_id": "sexec-card",
                "trigger": "scheduled",
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
    )
    approval_message = store.add_message(
        conversation_id,
        {
            "id": "assistant-desktop-card",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_result",
                    "tool_result": {
                        "tool_call_id": "call_desktop_frame",
                        "name": "Desktop Frame",
                        "content": "[truncated depth]",
                        "approval_required": True,
                        "approval_request_id": request["request_id"],
                        "arguments": arguments,
                    },
                }
            ],
            "finish_reason": "approval_required",
            "metadata": {},
            "events": [],
        },
    )
    assert scheduler_message is not None
    assert approval_message is not None
    store.update_conversation(conversation_id, {"current_node_id": approval_message["id"]})

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": "desktop frame inspected"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from ecosystem.rumi_operations_team_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    try:
        runtime = MimoCodingCompanyRuntime()
        schedules = runtime._schedules_for_state(
            {"schedule_ids": {"qa_loop": schedule["id"]}},
            recover_scheduled_approvals=True,
        )

        assert [item["id"] for item in schedules] == [schedule["id"]]
        assert len(calls) == 1
        followup = calls[0]["payload"]["message"]["metadata"]["approval_followup"]
        assert followup["tool_name"] == "desktop_frame"
        assert followup["request_id"] == request["request_id"]
        assert followup["approval_token"]
        assert followup["arguments"] == arguments
        assert calls[0]["context"]["owner_pack"] == "defaultspack"

        entries, total = load_history(schedule["id"])
        assert total == 1
        history = entries[0]
        assert history["status"] == "completed"
        assert history["result"] == "desktop frame inspected"
        assert history["recovered_scheduled_approval"] is True
        assert history["recovered_approval_card"] is True
        assert history["recovered_execution_id"] == "sexec-card"
        assert history["auto_approvals"] == [
            {
                "request_id": request["request_id"],
                "tool_name": "desktop_frame",
                "operation": "tool.desktop_frame",
                "status": "approved",
            }
        ]
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_scheduler_leaves_non_mimo_approval_waiting(tmp_path, monkeypatch):
    approval = _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    calls: list[dict] = []

    def fake_send_chat(payload, context):
        calls.append({"payload": payload, "context": context})
        return _approval_required_response(approval, conversation_id="conv-other")

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run browser QA.",
            "model": "stub/default",
            "conversation_id": "conv-other",
            "profile_id": "defaultspack.local_agent",
            "agent_id": "browser_qa",
            "tools": ["browser_use"],
            "tool_policy": {
                "profile_id": "defaultspack.local_agent",
                "schedule_auto_approve_tool_requests": True,
                "schedule_auto_approve_tool_allowlist": ["browser_use"],
            },
            "metadata": {
                "profile_id": "defaultspack.local_agent",
                "company_id": "other-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    history = scheduler.trigger_now(schedule["id"])

    assert history["status"] == "approval_required"
    assert history["finish_reason"] == "approval_required"
    assert "approval_required" in history["result"]
    assert len(calls) == 1
    assert "owner_pack" not in calls[0]["context"]
    assert "auto_approvals" not in history

    scheduler.delete_schedule(schedule["id"])
    _reset_scheduler_singleton()


def test_scheduled_chat_timeout_after_user_commit_appends_durable_error(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]

    def fake_send_chat(payload, context):
        store.add_message(
            payload["conversation_id"],
            {
                "role": "user",
                "content": payload["message"]["content"],
                "metadata": payload["message"]["metadata"],
            },
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            checker = context.get("is_cancelled") if isinstance(context, dict) else None
            if callable(checker) and checker():
                break
            time.sleep(0.01)
        return {"status": "ok", "data": {"content": "late", "finish_reason": "stop"}}

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run scheduled browser QA.",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "conversation_id": conversation_id,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "timeout": 0.05,
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    try:
        history = scheduler.trigger_now(schedule["id"])
        time.sleep(0.05)
        stored = store.get_conversation(conversation_id)
        messages = stored["messages"]

        assert history["status"] == "error"
        assert history["timeout_seconds"] == 0.05
        assert history["conversation_id"] == conversation_id
        assert history["assistant_error_message_id"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        user, assistant = messages
        assert assistant["parent_id"] == user["id"]
        assert assistant["id"] == history["assistant_error_message_id"]
        assert assistant["finish_reason"] == "error"
        assert assistant["metadata"]["durable_scheduler_error"] is True
        assert assistant["metadata"]["provider_invocation_started"] is False
        assert assistant["metadata"]["schedule_execution_id"] == history["execution_id"]
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_scheduled_chat_failure_after_user_commit_appends_redacted_durable_error(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2-omni",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    conversation_id = conversation["id"]

    def fake_send_chat(payload, context):
        store.add_message(
            payload["conversation_id"],
            {
                "role": "user",
                "content": payload["message"]["content"],
                "metadata": payload["message"]["metadata"],
            },
        )
        raise RuntimeError("provider setup failed with api_key=sk-abcdefghijklmnopqrstuvwxyz123456")

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run scheduled browser QA.",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "conversation_id": conversation_id,
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "timeout": 5,
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
            },
        },
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    try:
        history = scheduler.trigger_now(schedule["id"])
        stored = store.get_conversation(conversation_id)
        assistant = stored["messages"][-1]

        assert history["status"] == "error"
        assert history["assistant_error_message_id"] == assistant["id"]
        assert assistant["role"] == "assistant"
        assert assistant["finish_reason"] == "error"
        assert assistant["metadata"]["error_code"] == "SCHEDULED_CHAT_EXECUTION_FAILED"
        assert "api_key=[redacted]" in assistant["raw_text"]
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in assistant["raw_text"]
        assert assistant["events"][0]["type"] == "task_failed"
    finally:
        scheduler.delete_schedule(schedule["id"])
        _reset_scheduler_singleton()
        ChatStore._instance = None


def test_schedule_history_replaces_lone_surrogates_before_persisting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    from domain.agent.schedule_store import append_history, load_history, save_schedule

    save_schedule(
        {
            "id": "sched-surrogate",
            "type": "once",
            "task": {"message": "bad \udc88 schedule"},
            "config": {"run_at": "2099-01-01T00:00:00Z"},
            "status": "active",
        }
    )
    append_history(
        "sched-surrogate",
        {
            "execution_id": "sexec-surrogate",
            "status": "error",
            "error": "bad \udc88 history",
        },
    )

    entries, total = load_history("sched-surrogate")

    assert total == 1
    assert entries[0]["error"] == "bad ? history"
    assert "bad ? schedule" in (tmp_path / "user_data" / "shared" / "schedules" / "sched-surrogate.json").read_text(
        encoding="utf-8"
    )


def test_schedule_history_recovers_valid_entries_from_corrupt_legacy_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    from domain.agent.schedule_store import append_history, load_history, save_schedule

    save_schedule(
        {
            "id": "sched-corrupt-history",
            "type": "once",
            "task": {"message": "recover history"},
            "config": {"run_at": "2099-01-01T00:00:00Z"},
            "status": "active",
        }
    )
    history_path = tmp_path / "user_data" / "shared" / "schedules" / "sched-corrupt-history_history.json"
    history_path.write_text(
        """[
  {"execution_id": "sexec-valid-before", "status": "completed", "result": "before"},
  {"execution_id": "sexec-corrupt", "status": "authority_approval_required", "result": "authority_approval_required "needs approval""},
  {"execution_id": "sexec-valid-after", "status": "error", "error": "after"}
]""",
        encoding="utf-8",
    )

    entries, total = load_history("sched-corrupt-history")

    assert total == 2
    assert [entry["execution_id"] for entry in entries] == ["sexec-valid-after", "sexec-valid-before"]

    append_history(
        "sched-corrupt-history",
        {"execution_id": "sexec-new", "status": "completed", "result": "new"},
    )
    reparsed = json.loads(history_path.read_text(encoding="utf-8"))

    assert [entry["execution_id"] for entry in reparsed] == [
        "sexec-valid-before",
        "sexec-valid-after",
        "sexec-new",
    ]


def test_schedule_history_coerces_non_json_result_and_error_payloads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    from domain.agent.schedule_store import append_history, load_history

    append_history(
        "sched-json-safe",
        {
            "execution_id": "sexec-json-safe",
            "status": "error",
            "result": {"bad_number": float("nan"), "odd_set": {"alpha", "beta"}},
            "error": ValueError("authority_approval_required \"quoted\" result"),
        },
    )

    entries, total = load_history("sched-json-safe")

    assert total == 1
    assert entries[0]["result"]["bad_number"] == "nan"
    assert sorted(entries[0]["result"]["odd_set"]) == ["alpha", "beta"]
    assert entries[0]["error"] == 'authority_approval_required "quoted" result'


def test_schedule_store_can_use_explicit_schedules_dir_when_cwd_differs(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    runtime_root = repo_root / "tobkiri_runtime"
    schedules_dir = runtime_root / "user_data" / "shared" / "schedules"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(schedules_dir))

    from domain.agent.schedule_store import load_history, save_schedule, append_history

    save_schedule(
        {
            "id": "sched-explicit",
            "type": "once",
            "task": {"message": "use explicit runtime schedule dir"},
            "config": {"run_at": "2099-01-01T00:00:00Z"},
            "status": "active",
        }
    )
    append_history(
        "sched-explicit",
        {
            "execution_id": "sexec-explicit",
            "status": "completed",
            "result": "ok",
        },
    )

    entries, total = load_history("sched-explicit")

    assert total == 1
    assert entries[0]["result"] == "ok"
    assert (schedules_dir / "sched-explicit.json").is_file()
    assert not (repo_root / "user_data" / "shared" / "schedules" / "sched-explicit.json").exists()


def test_scheduler_serializes_chat_runs_for_same_conversation(tmp_path, monkeypatch):
    _setup_approval_store(tmp_path, monkeypatch)
    _reset_scheduler_singleton()

    active = 0
    max_active = 0
    call_count = 0
    lock = threading.Lock()
    first_call_started = threading.Event()
    release_first_call = threading.Event()

    def fake_send_chat(payload, context):
        nonlocal active, max_active, call_count
        with lock:
            active += 1
            max_active = max(max_active, active)
            call_count += 1
            index = call_count
            if index == 1:
                first_call_started.set()
        if index == 1:
            assert release_first_call.wait(timeout=2)
        time.sleep(0.05)
        content = payload["message"]["content"]
        with lock:
            active -= 1
        return {
            "status": "ok",
            "data": {
                "id": "assistant-final",
                "role": "assistant",
                "content": [{"type": "text", "text": content + " done"}],
                "finish_reason": "stop",
                "metadata": {},
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_send_chat)

    from domain.agent.scheduler import Scheduler

    scheduler = Scheduler()
    first = scheduler.create_schedule(
        "once",
        {"message": "first", "conversation_id": "conv-shared"},
        {"run_at": "2099-01-01T00:00:00Z"},
    )
    second = scheduler.create_schedule(
        "once",
        {"message": "second", "conversation_id": "conv-shared"},
        {"run_at": "2099-01-01T00:00:00Z"},
    )

    results = {}

    def run_schedule(key, schedule_id):
        results[key] = scheduler._execute_task(schedule_id, manual=False)

    t1 = threading.Thread(target=run_schedule, args=("first", first["id"]))
    t2 = threading.Thread(target=run_schedule, args=("second", second["id"]))
    t1.start()
    assert first_call_started.wait(timeout=2)
    t2.start()
    time.sleep(0.1)

    assert call_count == 1
    assert max_active == 1

    release_first_call.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert results["first"]["status"] == "completed"
    assert results["second"]["status"] == "completed"
    assert max_active == 1

    scheduler.delete_schedule(first["id"])
    scheduler.delete_schedule(second["id"])
    _reset_scheduler_singleton()
