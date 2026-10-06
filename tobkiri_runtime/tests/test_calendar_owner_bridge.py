"""Finite Calendar compatibility over temp canonical owners and per-call clients."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from typing import Any, Mapping

import pytest

TEST_ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    TEST_ROOT / "ecosystem/rumi_schedule_store_pack/runtime"
)


def load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    # Use the canonical packages selected by this runtime's PYTHONPATH. No
    # output-directory topology or AST-projected helper is a test dependency.
    from tobkiri_protocol import saved_conversation
    from ecosystem.rumi_turn_runtime_pack.runtime import scheduled_saved

    assert callable(saved_conversation.validate_saved_conversation_input)
    assert callable(scheduled_saved.normalize_scheduled_saved_task)
    recurrence = load(
        MODULES / "recurrence.py",
        "ecosystem.rumi_schedule_store_pack.runtime.recurrence",
        monkeypatch,
    )
    store = load(MODULES / "store.py", "calendar_store_test", monkeypatch)
    calendar = load(MODULES / "calendar.py", "calendar_facade_test", monkeypatch)
    return calendar, store, recurrence


class Invocation:
    presentation_owner_principal_id = "shell"
    presentation_owner_session_id = "session"

    def __init__(self, calendar, store):
        self.calendar, self.store, self.calls = calendar, store, []
        self.guards = 0
        self.client_count = 0

    def assert_current(self):
        self.guards += 1

    def contract_client(self, **kwargs):
        assert kwargs["include_credentials"] is False
        assert kwargs["consumer_pack_id"] == "rumi_schedule_store_pack"
        self.client_count += 1
        return self

    def invoke(self, contract, operation, payload):
        self.calls.append((contract, operation, payload))
        assert payload["profile_id"] == "defaults"
        if contract == self.calendar.RESOURCE:
            assert operation == self.calendar.RESOURCE_OPERATION
            return self.store.snapshot()
        if contract == self.calendar.CLOCK:
            assert operation == self.calendar.CLOCK_OPERATION
            return {
                "armed": True,
                "available": True,
                "expires_at_ms": 86400000 + 1000000,
            }
        if contract == self.calendar.CONTROL:
            assert operation == self.calendar.CONTROL_OPERATION
            return {"triggered": self.store.get(payload["schedule_id"]), "count": 1}
        assert (
            contract == self.calendar.ACTION
            and operation == self.calendar.ACTION_OPERATION
        )
        return self.store.apply(
            payload["operation"],
            self.store_module._arguments(payload["operation"], payload),
        )


def captured(calendar, *, read=False, clock=lambda: 1_000_000):
    function = calendar.READ_FUNCTION if read else calendar.WRITE_FUNCTION
    contract = calendar.READ_CONTRACT if read else calendar.WRITE_CONTRACT
    operation = calendar.READ_OPERATION if read else calendar.WRITE_OPERATION
    binding = NS(
        function=NS(function_id=function, implementation_digest="sha256:" + "1" * 64),
        operation=NS(
            contract_id=contract, operation_id=operation, contract_version="1.0.0"
        ),
        principal_ref=NS(value="provider"),
        artifact=NS(digest="sha256:" + "2" * 64),
    )
    context = NS(
        profile_id="defaults",
        provider_bindings=[binding],
        domain_ids={(contract, operation, "provider"): "domain"},
    )
    return calendar.CalendarHostFactoryV4(function, clock=clock).capture(
        context
    ).contributions[0].invoke, operation


@pytest.fixture
def bridge(modules, tmp_path):
    calendar, store, recurrence = modules
    owner = store.ScheduleStore("defaults", root=tmp_path, clock=lambda: 1_000_000)
    invocation = Invocation(calendar, owner)
    invocation.store_module = store
    call, operation = captured(calendar)
    read, read_op = captured(calendar, read=True)

    def execute(action_name, **fields):
        return call(
            operation,
            {"profile_id": "defaults", "operation": action_name, **fields},
            invocation,
        )

    def get(action_name, **fields):
        return read(
            read_op,
            {"profile_id": "defaults", "operation": action_name, **fields},
            invocation,
        )

    return calendar, owner, invocation, execute, get


def create(execute, kind="once", config=None, task=None):
    return execute(
        "create",
        name="Calendar: Review",
        description="Inert display only",
        schedule_type=kind,
        schedule_config=config or {"run_at": "2026-10-07T09:00:00+09:00"},
        task=task
        or {
            "message": "Review",
            "conversation_id": None,
            "model": "profile-selected",
            "metadata": {"source": "calendar"},
        },
    )["data"]


def test_crud_null_destination_model_and_profile_owned_reload(
    bridge, modules, tmp_path
):
    calendar, owner, invocation, execute, get = bridge
    result = create(execute)
    identifier = result["id"]
    raw = owner.get(identifier)
    assert raw["payload"] == {
        "message": "Review",
        "profile_id": "defaults",
        "conversation_id": None,
        "model": "profile-selected",
    }
    assert "metadata" not in raw["payload"] and raw["presentation"]["task"][
        "metadata"
    ] == {"source": "calendar"}
    assert raw["next_run_at_ms"] == calendar._iso_ms("2026-10-07T00:00:00Z")
    assert (
        modules[1].ScheduleStore("defaults", root=tmp_path).get(identifier)["payload"]
        == raw["payload"]
    )
    assert (
        get("get", schedule_id=identifier)["data"]["task"]["model"]
        == "profile-selected"
    )
    updated = execute(
        "update", schedule_id=identifier, name="Renamed", task={"message": "New prompt"}
    )["data"]
    assert updated["id"] == identifier and updated["task"]["message"] == "New prompt"
    assert execute("pause", schedule_id=identifier)["data"]["status"] == "paused"
    assert execute("resume", schedule_id=identifier)["data"]["status"] == "active"
    assert execute("trigger", schedule_id=identifier)["data"]["count"] == 1
    assert get("list")["data"]["total"] == 1
    assert invocation.client_count == 7  # each invocation obtains a fresh client
    assert execute("delete", schedule_id=identifier)["data"]["deleted"] is True


def test_interval_and_cron_owner_completion_recurrence_and_history(bridge, modules):
    _, owner, _, execute, get = bridge
    interval = create(execute, "interval", {"value": 2, "unit": "minutes"})
    assert owner.get(interval["id"])["interval_ms"] == 120000
    cron = create(
        execute, "cron", {"expression": "* * * * *", "timezone": "Asia/Tokyo"}
    )
    assert owner.get(cron["id"])["next_run_at_ms"] == 1_020_000
    owner.clock = lambda: 1_020_000
    claim = owner.apply(
        "claim",
        {
            "schedule_id": cron["id"],
            "expected_revision": owner.snapshot()["revision"],
            "lease_id": "lease",
            "lease_expires_at_ms": 1_100_000,
        },
    )
    completed = owner.apply(
        "complete",
        {
            "schedule_id": cron["id"],
            "expected_revision": claim["revision"],
            "lease_id": "lease",
            "error": "",
        },
    )["schedule"]
    assert (
        completed["status"] == "scheduled" and completed["next_run_at_ms"] == 1_080_000
    )
    assert (
        get("history", schedule_id=cron["id"])["data"]["entries"][0]["status"]
        == "completed"
    )


@pytest.mark.parametrize(
    "kind,config",
    [
        ("cron", {"expression": "60 * * * *"}),
        ("cron", {"expression": "* * * * *", "timezone": "Unavailable/Zone"}),
        ("interval", {"value": True}),
        ("once", {"run_at": "invalid"}),
    ],
)
def test_invalid_timing_has_no_owner_write(bridge, kind, config):
    _, owner, _, execute, _ = bridge
    with pytest.raises((ValueError, TypeError)):
        create(execute, kind, config)
    assert owner.snapshot()["revision"] == 0


def test_crossprofile_approvalflags_and_missing_owner_reject_without_write(bridge):
    calendar, owner, invocation, execute, _ = bridge
    with pytest.raises(PermissionError):
        create(
            execute,
            task={"message": "M", "conversation_id": "existing", "profile_id": "other"},
        )
    with pytest.raises(ValueError):
        create(
            execute,
            task={"message": "M", "conversation_id": "existing", "approved": True},
        )
    invocation.presentation_owner_principal_id = ""
    with pytest.raises(PermissionError):
        create(execute)
    assert owner.snapshot()["revision"] == 0


def test_lean_references_preserved_and_crossprofile_or_metadata_authority_rejected(
    bridge,
):
    _, owner, _, execute, _ = bridge
    refs = [{"kind": "chat", "profile_id": "defaults", "id": "other-chat"}]
    created = create(
        execute,
        task={
            "message": "Only instructions",
            "conversation_id": "existing-chat",
            "chat_references": refs,
        },
    )
    assert owner.get(created["id"])["payload"]["chat_references"] == refs
    before = owner.snapshot()["revision"]
    with pytest.raises(ValueError):
        create(
            execute,
            task={
                "message": "M",
                "conversation_id": None,
                "chat_references": [
                    {"kind": "chat", "profile_id": "other", "id": "cross-profile"}
                ],
            },
        )
    with pytest.raises(ValueError):
        create(
            execute,
            task={
                "message": "M",
                "conversation_id": "existing-chat",
                "tools": ["terminal"],
                "approved": True,
            },
        )
    assert owner.snapshot()["revision"] == before


def test_timezone_cron_and_update_to_once_clears_recurrence(bridge):
    calendar, owner, _, execute, _ = bridge
    value = create(
        execute, "cron", {"expression": "0 9 * * *", "timezone": "Asia/Tokyo"}
    )
    raw = owner.get(value["id"])
    assert raw["next_run_at_ms"] == calendar._iso_ms("1970-01-02T00:00:00Z")
    execute(
        "update",
        schedule_id=value["id"],
        schedule_type="once",
        schedule_config={"run_at": "2026-10-07T00:00:00Z"},
    )
    assert owner.get(value["id"])["recurrence"] is None
    assert owner.get(value["id"])["interval_ms"] == 0


def test_pause_stale_revision_and_running_edit_fail_closed(bridge, modules):
    _, owner, _, execute, _ = bridge
    created = create(execute)
    with pytest.raises(modules[1].ScheduleConflict):
        owner.apply("pause", {"schedule_id": created["id"], "expected_revision": 0})
    revision = owner.snapshot()["revision"]
    owner.apply(
        "update",
        {
            "schedule_id": created["id"],
            "expected_revision": revision,
            "updates": {"next_run_at_ms": 0},
        },
    )
    owner.apply(
        "claim",
        {
            "schedule_id": created["id"],
            "expected_revision": revision + 1,
            "lease_id": "claimed",
            "lease_expires_at_ms": 2_000_000,
        },
    )
    with pytest.raises(modules[1].ScheduleConflict):
        execute("update", schedule_id=created["id"], name="Should not apply")
    assert owner.get(created["id"])["name"] == "Calendar: Review"


def test_scheduler_capture_clients_are_local_and_only_inert_state_is_cached():
    """Concurrent control can observe state without replacing another client."""
    import threading

    path = MODULES.parents[1] / "rumi_scheduler_runtime_pack/runtime/scheduler.py"
    tree = ast.parse(path.read_text())
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_invoke_v4_owner"
    )
    states, seen = {}, []
    registry_lock = threading.Lock()

    class Runtime:
        def __init__(self, client, profile, canonical, process_state):
            self.client, self.state = client, process_state

        def control(self, operation, payload):
            assert not registry_lock.locked()
            seen.append((self.client, self.state))
            if operation == "fail":
                raise ValueError("intentional")
            return {"status": "ok"}

    namespace = {
        "Any": Any,
        "Mapping": Mapping,
        "SERVICE_PACK_ID": "scheduler",
        "threading": threading,
        "hashlib": __import__("hashlib"),
        "json": __import__("json"),
        "_LOCK": registry_lock,
        "_V4_PROCESS_STATES": states,
        "SchedulerRuntime": Runtime,
        "CLOCK_ACTION": "clock",
    }
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "scheduler-owner", "exec"),
        namespace,
    )
    context = NS(
        profile_id="p",
        plan_digest="plan",
        security_epoch=1,
        activation={"activation_id": "first", "fencing_token": 1},
    )
    first, second = object(), object()
    invoke = namespace[node.name]
    invoke(
        "scheduler.control",
        context,
        {"operation": "tick"},
        NS(contract_client=lambda **kwargs: first),
    )
    with pytest.raises(ValueError, match="intentional"):
        invoke(
            "scheduler.control",
            context,
            {"operation": "fail"},
            NS(contract_client=lambda **kwargs: second),
        )
    assert [item[0] for item in seen] == [first, second]
    assert seen[0][1] is seen[1][1]
    assert set(next(iter(states.values()))) == {
        "lock",
        "stopping",
        "active",
        "last_tick_at_ms",
        "last_error",
    }


def test_calendar_missing_wake_driver_rejects_before_schedule_write(modules, tmp_path):
    calendar, store_module, _ = modules
    store = store_module.ScheduleStore("defaults", root=tmp_path)
    inv = Invocation(calendar, store)
    inv.store_module = store_module
    original = inv.invoke

    def invoke(contract, operation, payload):
        if contract == calendar.CLOCK:
            return {"armed": False, "available": False}
        return original(contract, operation, payload)

    inv.invoke = invoke
    host, operation_id = captured(calendar)
    with pytest.raises(PermissionError, match="wake driver"):
        host(
            operation_id,
            {
                "profile_id": "defaults",
                "operation": "create",
                "name": "n",
                "schedule_type": "once",
                "schedule_config": {"run_at": "2030-01-01T00:00:00Z"},
                "task": {"message": "hello", "conversation_id": None},
            },
            inv,
        )
    assert not store.snapshot()["schedules"]


def test_scheduler_renews_only_live_near_deadline_without_rearming():
    import threading

    path = MODULES.parents[1] / "rumi_scheduler_runtime_pack/runtime/scheduler.py"
    tree = ast.parse(path.read_text())
    owner = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "SchedulerRuntime"
    )
    node = next(
        n
        for n in owner.body
        if isinstance(n, ast.FunctionDef) and n.name == "_renew_live_clock"
    )
    namespace = {
        "Any": Any,
        "Mapping": Mapping,
        "SCHEDULE_RESOURCE": "schedule",
        "CLOCK_ACTION": "clock",
    }
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "renew-live", "exec"),
        namespace,
    )
    calls = []
    records = [{"enabled": True, "status": "scheduled"}]
    deadline = 150000

    def invoke(contract, operation, payload):
        calls.append((contract, operation))
        if contract == "schedule":
            return {"schedules": records}
        return {"available": True, "armed": True, "expires_at_ms": deadline}

    runtime = NS(
        lock=threading.RLock(),
        stopping=False,
        profile_id="p",
        clock=lambda: 100000,
        _invoke=invoke,
    )
    namespace[node.name](runtime)
    assert calls == [("schedule", "list"), ("clock", "status"), ("clock", "renew")]
    calls.clear()
    deadline = 1000000
    namespace[node.name](runtime)
    assert calls == [("schedule", "list"), ("clock", "status")]
    calls.clear()
    records.clear()
    namespace[node.name](runtime)
    assert calls == [("schedule", "list")]


@pytest.mark.parametrize("destination", [None, "existing-conversation"])
@pytest.mark.parametrize("preference", ["ask", "agent", "full"])
def test_calendar_approval_preference_is_finite_data(modules, destination, preference):
    calendar = modules[0]
    task, presentation = calendar._normalize_task({
        "message": "scheduled", "conversation_id": destination,
        "action_approval_mode": preference}, "defaults")
    assert task["action_approval_mode"] == preference
    assert "action_approval_mode" not in presentation
    with pytest.raises(ValueError, match="approval preference"):
        calendar._normalize_task({"message": "scheduled", "conversation_id": destination,
                                  "action_approval_mode": "approved"}, "defaults")


@pytest.mark.parametrize("destination", [None, "existing-conversation"])
def test_calendar_workspace_preference_is_opaque_data(modules, destination):
    calendar = modules[0]
    task, presentation = calendar._normalize_task({
        "message": "scheduled", "conversation_id": destination,
        "workspace_id": "workspace:registered-123"}, "defaults")
    assert task["workspace_id"] == "workspace:registered-123"
    assert "workspace_id" not in presentation
    for invalid in (None, "", "/tmp/project", "../project", "workspace\n", "x" * 257):
        with pytest.raises(ValueError, match="workspace preference"):
            calendar._normalize_task({"message": "scheduled", "conversation_id": destination,
                                      "workspace_id": invalid}, "defaults")


@pytest.mark.parametrize("destination", [None, "existing-conversation"])
def test_calendar_message_utf8_adapter_boundary(modules, destination):
    calendar = modules[0]
    # 20480 Japanese characters =61440 UTF-8 bytes (normal Saved cap).
    message = "日" * 20480
    task, _ = calendar._normalize_task({"message": message,
                                      "conversation_id": destination}, "defaults")
    assert task["message"] == message
    for too_long in (message + "a", "日" * 20481, "a" * 61441):
        with pytest.raises(ValueError, match="Saved UTF-8 bound"):
            calendar._normalize_task({"message": too_long,
                                      "conversation_id": destination}, "defaults")
