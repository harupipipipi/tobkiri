"""Real job/schedule owner recovery with isolated public adapter fixtures.

Workflow Authority/model fixtures below are explicit substitutes. These tests do
not claim production Workflow admission, a native timer, or real 600-second IO.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.workflow_v4 import (
    ApprovalState,
    WorkflowEngineV4,
    WorkflowProviderV4,
    WorkflowStoreV4,
)
from core_runtime.workflow_v4.models import InvocationOutcome
from ecosystem.rumi_job_action_broker_pack.runtime.broker import JobActionBroker
from ecosystem.rumi_schedule_store_pack.runtime.store import ScheduleStore, _arguments
from ecosystem.rumi_scheduler_runtime_pack.runtime.scheduler import SchedulerRuntime
from ecosystem.tobkiri_agent_control_pack.runtime.host import WorkPlanHostFactory
from ecosystem.tobkiri_agent_control_pack.runtime.ports import SAVED, SAVED_OP
from ecosystem.tobkiri_agent_control_pack.runtime.service import WorkPlanService
from ecosystem.tobkiri_agent_control_pack.runtime.store import PlanStore
from ecosystem.tobkiri_agent_control_pack.runtime.workflow import (
    REVIEW,
    REVIEW_OP,
    WORKFLOW,
    ReviewWorkflow,
)
from tests.test_agent_control_pack import FakePorts, USER, add, apply, configure
from tests.test_workflow_v4 import Authority, Catalog, Validator

PROFILE = "profile-a"
JOB = "tobkiri.action.job.v1"
ADAPTER = "tobkiri.action.job.adapter.v2"


class Adapter:
    """Finite public test adapter recording exact dispatched/restored envelopes."""

    def __init__(self) -> None:
        self.status = "running"
        self.calls: list[dict[str, Any]] = []
        self.metadata = {
            "provider_id": "fixture.adapter",
            "operation_id": "fixture.adapter-op",
            "artifact_digest": "sha256:" + "1" * 64,
        }
        self.fail = False

    def providers(self, contract: str) -> tuple[dict[str, str], ...]:
        assert contract == ADAPTER
        return (deepcopy(self.metadata),)

    def invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        assert contract == ADAPTER and operation == self.metadata["operation_id"]
        if payload["operation"] == "describe":
            return {"action_ids": ["fixture.action"]}
        self.calls.append(deepcopy(dict(payload)))
        if self.fail:
            raise RuntimeError("unknown effect outcome")
        if payload["operation"] == "cancel":
            self.status = "cancelled"
        return {"status": self.status}


def request(**changes: Any) -> dict[str, Any]:
    """Create an ordinary dispatch payload; its lease is identity, not authority."""
    return {
        "profile_id": PROFILE,
        "action_id": "fixture.action",
        "payload": {"plan_id": "plan"},
        "idempotency_key": "schedule:lease",
        "schedule_id": "schedule",
        "lease_id": "lease",
        **changes,
    }


@pytest.mark.parametrize(
    "pending", ["accepted", "running", "waiting", "waiting_approval", "reconciliation_required"]
)
def test_restart_status_cancel_restores_exact_immutable_envelope(
    tmp_path: Path, pending: str
) -> None:
    adapter = Adapter()
    adapter.status = pending
    broker = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    original = request()
    assert broker.invoke("dispatch", original)["status"] == pending
    original["payload"]["plan_id"] = "changed-after-dispatch"
    restarted = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    assert restarted.invoke("status", {"idempotency_key": "schedule:lease"})["status"] == pending
    assert (
        restarted.invoke("cancel", {"idempotency_key": "schedule:lease"})["status"] == "cancelled"
    )
    for call in adapter.calls:
        assert {key: value for key, value in call.items() if key != "operation"} == request()
    assert [call["operation"] for call in adapter.calls] == ["dispatch", "status", "cancel"]


@pytest.mark.parametrize("field", ["profile_id", "schedule_id", "lease_id", "payload"])
def test_retained_hash_and_profile_modification_rejects_without_adapter(
    tmp_path: Path, field: str
) -> None:
    adapter = Adapter()
    broker = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    broker.invoke("dispatch", request())
    ledger = json.loads(broker.path.read_text())
    envelope = ledger["entries"]["schedule:lease"]["dispatch_envelope"]
    envelope[field] = {"plan_id": "other"} if field == "payload" else "other"
    broker.path.write_text(json.dumps(ledger))
    count = len(adapter.calls)
    with pytest.raises(PermissionError):
        broker.invoke("status", {"idempotency_key": "schedule:lease"})
    assert len(adapter.calls) == count


def test_copied_ledger_and_client_approval_never_grant_cross_profile_dispatch(
    tmp_path: Path,
) -> None:
    adapter = Adapter()
    first = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    first.invoke("dispatch", request())
    other = JobActionBroker(adapter, "profile-b", root=tmp_path, canonical=True)
    other.path.parent.mkdir(parents=True)
    other.path.write_bytes(first.path.read_bytes())
    with pytest.raises(PermissionError):
        other.invoke("status", {"idempotency_key": "schedule:lease"})
    with pytest.raises(PermissionError):
        first.invoke("dispatch", request(profile_id="profile-b"))
    with pytest.raises(PermissionError):
        first.invoke("dispatch", request(payload={"approved": True}))


@pytest.mark.parametrize("operation", ["dispatch", "status", "cancel"])
@pytest.mark.parametrize("missing", ["dispatch_envelope", "provider_binding"])
def test_old_incomplete_binding_requires_reconciliation_without_redispatch(
    tmp_path: Path, operation: str, missing: str
) -> None:
    adapter = Adapter()
    broker = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    broker.invoke("dispatch", request())
    ledger = json.loads(broker.path.read_text())
    ledger["entries"]["schedule:lease"].pop(missing)
    broker.path.write_text(json.dumps(ledger))
    count = len(adapter.calls)
    assert broker.invoke(operation, request())["status"] == "reconciliation_required"
    assert len(adapter.calls) == count


def test_replay_never_rebinds_scheduler_lease_or_selected_artifact(tmp_path: Path) -> None:
    adapter = Adapter()
    broker = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    broker.invoke("dispatch", request())
    with pytest.raises(PermissionError):
        broker.invoke("dispatch", request(lease_id="other"))
    adapter.metadata["artifact_digest"] = "sha256:" + "2" * 64
    count = len(adapter.calls)
    assert broker.invoke("status", request())["status"] == "reconciliation_required"
    assert len(adapter.calls) == count


def test_exception_does_not_authorize_a_new_dispatch_after_restart(tmp_path: Path) -> None:
    adapter = Adapter()
    adapter.fail = True
    broker = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    assert broker.invoke("dispatch", request())["status"] == "reconciliation_required"
    adapter.fail = False
    restarted = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    assert restarted.invoke("dispatch", request())["deduplicated"] is True
    assert [x["operation"] for x in adapter.calls].count("dispatch") == 1
    assert restarted.invoke("status", request())["status"] == "running"


def test_confirmed_cancel_fences_late_dispatch_completion(tmp_path: Path) -> None:
    entered, release = Event(), Event()

    class BlockingAdapter(Adapter):
        def invoke(
            self, contract: str, operation: str, payload: Mapping[str, Any]
        ) -> dict[str, Any]:
            if payload["operation"] != "dispatch":
                return super().invoke(contract, operation, payload)
            self.calls.append(deepcopy(dict(payload)))
            entered.set()
            assert release.wait(5)
            return {"status": "completed"}

    adapter = BlockingAdapter()
    broker = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
    outcomes: list[dict[str, Any]] = []
    worker = Thread(target=lambda: outcomes.append(broker.invoke("dispatch", request())))
    worker.start()
    try:
        assert entered.wait(5)
        restarted = JobActionBroker(adapter, PROFILE, root=tmp_path, canonical=True)
        assert restarted.invoke("cancel", request())["status"] == "cancelled"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes[0]["status"] == "cancelled"
    assert outcomes[0]["result"]["status"] == "cancelled"
    assert restarted.invoke("status", request())["status"] == "cancelled"


class PublicOwners(FakePorts):
    """Actual durable owners and captured AC adapter with explicit model/Authority fixtures."""

    def __init__(self, root: Path, clock: list[int], *, waiting: bool = False) -> None:
        super().__init__()
        self.now = clock[0]
        self.clock = clock
        self.store = ScheduleStore(PROFILE, root=root, clock=lambda: clock[0])
        self.broker = JobActionBroker(self, PROFILE, root=root, canonical=True)
        self.service = WorkPlanService(PlanStore(root, PROFILE), self, clock=lambda: clock[0])
        catalog = Catalog()
        catalog.value["operations"][0].update(
            contract_id=REVIEW, operation_id=REVIEW_OP, function_principal_id="fixture.review"
        )
        self.authority = Authority(
            ApprovalState.WAITING_APPROVAL if waiting else ApprovalState.RESERVED
        )
        self.cancelled: list[str] = []
        owner = self

        class Invoker:
            def invoke(self, request: Mapping[str, Any], *, authority: Any) -> InvocationOutcome:
                assert authority.dispatch_token.startswith("one-shot-")
                return InvocationOutcome(output=owner.service.review(request["input"], USER))

            def cancel(self, request_id: str) -> None:
                owner.cancelled.append(request_id)

        self.workflow_store = WorkflowStoreV4(root / "workflow.sqlite3", clock=lambda: 100.0)
        self.workflow = WorkflowProviderV4(
            WorkflowEngineV4(
                store=self.workflow_store,
                catalog=catalog,
                authority=self.authority,
                invoker=Invoker(),
                validator=Validator(),
                clock=lambda: 100.0,
            )
        )
        factory = WorkPlanHostFactory("job")
        binding = SimpleNamespace(
            function=SimpleNamespace(function_id=factory.function_id),
            operation=SimpleNamespace(
                contract_id=ADAPTER, operation_id=factory.operation_id, contract_version="2.0.0"
            ),
            principal_ref=SimpleNamespace(value="fixture.job"),
            artifact=SimpleNamespace(digest="fixture.artifact"),
        )
        binding.function.implementation_digest = "fixture.implementation"
        context = SimpleNamespace(
            profile_id=PROFILE,
            user_data_root=root,
            provider_bindings=(binding,),
            domain_ids={(ADAPTER, factory.operation_id, "fixture.job"): "fixture.domain"},
        )
        self.captured = factory.capture(context).contributions[0]
        self.metadata = {
            "provider_id": factory.function_id,
            "operation_id": factory.operation_id,
            "principal_id": "fixture.job",
            "artifact_digest": "fixture.artifact",
        }

    def providers(self, contract: str) -> tuple[dict[str, str], ...]:
        assert contract == ADAPTER
        return (self.metadata,)

    def invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        if contract == "tobkiri.resource.schedule.v1":
            if values["operation"] == "list":
                return self.store.snapshot()
            if values["operation"] == "get":
                return {"schedule": self.store.get(values["schedule_id"])}
            return self.store.due(values["now_ms"], values["limit"], values.get("schedule_id", ""))
        if contract == "tobkiri.action.schedule.v1":
            return self.store.apply(values["operation"], _arguments(values["operation"], values))
        if contract == JOB:
            return self.broker.invoke(values.pop("operation"), values)
        if contract == WORKFLOW:
            return self.workflow.invoke(operation, values)
        if contract == ADAPTER:
            invocation = SimpleNamespace(
                assert_current=lambda: None,
                presentation_owner_principal_id="operator",
                envelope=SimpleNamespace(context=SimpleNamespace(request_id="fixture.request")),
                contract_client=lambda **_: self,
            )
            return dict(self.captured.invoke(operation, values, invocation))
        return super().invoke(contract, operation, payload)


def test_injected_600_second_chain_runs_one_review_and_recurs(tmp_path: Path) -> None:
    clock = [1000000]
    ports = PublicOwners(tmp_path, clock)
    configure(ports.service)
    add(ports.service)
    scheduler = SchedulerRuntime(ports, PROFILE, clock=lambda: clock[0], canonical=True)
    clock[0] += 599999
    assert scheduler.control("tick", {})["count"] == 0
    clock[0] += 1
    assert scheduler.control("tick", {})["count"] == 1
    plan = ports.service.store.get("plan")
    assert plan["review"]["verdict"] == "drift" and len(plan["inbox"]) == 1
    schedule = ports.store.get(plan["schedule"]["id"])
    assert schedule["next_run_at_ms"] == clock[0] + 600000
    assert schedule["status"] == "scheduled"


def test_waiting_review_survives_restart_and_durable_scheduler_stop(tmp_path: Path) -> None:
    clock = [1000000]
    first = PublicOwners(tmp_path, clock, waiting=True)
    configure(first.service)
    add(first.service)
    clock[0] += 600000
    runtime = SchedulerRuntime(first, PROFILE, clock=lambda: clock[0], canonical=True)
    assert runtime.control("tick", {})["dispatched"][0]["result"]["status"] == "waiting_approval"
    plan = first.service.store.get("plan")
    first.workflow_store.close()
    clock[0] += 300001
    restarted = PublicOwners(tmp_path, clock, waiting=True)
    runtime = SchedulerRuntime(restarted, PROFILE, clock=lambda: clock[0], canonical=True)
    assert runtime.control("tick", {})["count"] == 0
    assert restarted.authority.commit_count == 0
    assert runtime.control("stop", {})["cancellations"][0]["status"] == "cancelled"
    assert restarted.store.get(plan["schedule"]["id"])["status"] == "cancelled"
    assert restarted.service.store.get("plan")["review"] is None


@pytest.mark.parametrize("action", ["plan.pause", "plan.cancel"])
def test_plan_fences_and_cancels_exact_waiting_owner_run(tmp_path: Path, action: str) -> None:
    clock = [1000000]
    ports = PublicOwners(tmp_path, clock, waiting=True)
    configure(ports.service)
    add(ports.service)
    clock[0] += 600000
    SchedulerRuntime(ports, PROFILE, clock=lambda: clock[0], canonical=True).control("tick", {})
    result = apply(ports.service, action)
    assert result["review_cancellation"]["status"] == "completed"
    plan = ports.service.store.get("plan")
    for occurrence, binding in plan["review_runs"].items():
        assert binding["cancel_requested"] and binding["status"] == "cancelled"
        assert (
            ports.workflow.invoke("run.get", {"run_id": ReviewWorkflow.run_id(occurrence)})["run"][
                "state"
            ]
            == "cancelled"
        )
    assert plan["review"] is None


def test_pause_during_review_never_publishes_late_finding(tmp_path: Path) -> None:
    clock = [1000000]
    ports = PublicOwners(tmp_path, clock)
    configure(ports.service)
    add(ports.service)
    ports.callback = lambda: apply(ports.service, "plan.pause")
    clock[0] += 600000
    SchedulerRuntime(ports, PROFILE, clock=lambda: clock[0], canonical=True).control("tick", {})
    plan = ports.service.store.get("plan")
    assert plan["status"] == "paused" and plan["review"] is None and plan["inbox"] == []


def test_explicit_resume_reenables_a_cancelled_review_schedule(tmp_path: Path) -> None:
    clock = [1000000]
    ports = PublicOwners(tmp_path, clock, waiting=True)
    configure(ports.service)
    add(ports.service)
    clock[0] += 600000
    SchedulerRuntime(ports, PROFILE, clock=lambda: clock[0], canonical=True).control("tick", {})
    apply(ports.service, "plan.pause")
    restarted = SchedulerRuntime(ports, PROFILE, clock=lambda: clock[0], canonical=True)
    restarted.control("stop", {})
    plan = ports.service.store.get("plan")
    assert ports.store.get(plan["schedule"]["id"])["status"] == "cancelled"
    apply(ports.service, "plan.resume")
    latest = ports.store.get(plan["schedule"]["id"])
    assert latest["status"] == "scheduled" and latest["enabled"]
    assert latest["payload"]["generation"] == ports.service.store.get("plan")["generation"]


def scheduled_reminder(ports: PublicOwners) -> tuple[str, dict[str, Any]]:
    """Enqueue one actually dispatched public reminder, without waking an executor."""
    apply(
        ports.service,
        "remind.create",
        reminder_id="notice",
        body="Keep the requirement",
        agent_binding="conversation:conversation",
        timezone="UTC",
        delay_seconds=1,
    )
    ports.clock[0] += 1000
    SchedulerRuntime(ports, PROFILE, clock=lambda: ports.clock[0], canonical=True).control(
        "tick", {}
    )
    plan = ports.service.store.get("plan")
    event = plan["inbox"][0]
    schedule = ports.store.get(plan["reminders"]["notice"]["schedule_id"])
    # The public job ledger owns the dispatch key even after enqueue completed.
    entry = next(
        value
        for value in ports.broker._read()["entries"].values()
        if value["action_id"] == "agent-control.remind"
    )
    assert schedule["status"] == "completed" and event["status"] == "pending"
    return entry["idempotency_key"], event


def test_reminder_cancel_fences_pending_enqueue_without_false_apply(tmp_path: Path) -> None:
    clock = [1000000]
    ports = PublicOwners(tmp_path, clock)
    configure(ports.service)
    key, _ = scheduled_reminder(ports)
    result = apply(ports.service, "remind.cancel", reminder_id="notice")
    assert result["delivery_cancellation"]["status"] == "completed"
    assert ports.broker.invoke("cancel", {"idempotency_key": key})["status"] == "cancelled"
    plan = ports.service.store.get("plan")
    assert plan["inbox"][0]["status"] == "cancelled"
    received = ports.service.prepare_input(
        {
            "plan_id": "plan",
            "conversation_id": "conversation",
            "expected_revision": plan["revision"],
            "operation_id": "prepare-cancelled",
            "input_id": "after-cancel",
            "agent_binding": "conversation:conversation",
            "boundary": "before_turn",
        },
        USER,
    )
    assert received["instructions"] == []


def test_received_reminder_requires_canonical_acceptance_and_never_claims_cancel(
    tmp_path: Path,
) -> None:
    clock = [1000000]
    ports = PublicOwners(tmp_path, clock)
    configure(ports.service)
    key, _ = scheduled_reminder(ports)
    plan = ports.service.store.get("plan")
    received = ports.service.prepare_input(
        {
            "plan_id": "plan",
            "conversation_id": "conversation",
            "expected_revision": plan["revision"],
            "operation_id": "prepare-input",
            "input_id": "input",
            "agent_binding": "conversation:conversation",
            "boundary": "before_turn",
        },
        USER,
    )
    source = {
        "request": {
            "turn_id": "input",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "Original content",
            "tool_selection": {"mode": "auto"},
            "task_context": received["task_context"],
        }
    }
    pending = ports.broker.invoke("cancel", {"idempotency_key": key})
    assert pending["status"] == "cancellation_pending"
    plan = ports.service.store.get("plan")
    with pytest.raises(ValueError):
        ports.service.ack(
            {
                "plan_id": "plan",
                "expected_revision": plan["revision"],
                "operation_id": "ack-unaccepted",
                "input_id": "input",
                "event_ids": [plan["inbox"][0]["id"]],
                "accepted_input": source,
            },
            USER,
        )
    assert ports.service.store.get("plan")["inbox"][0]["status"] == "received"
    # The canonical saved owner fixture accepts this exact immutable input.
    ports.invoke(SAVED, SAVED_OP, source)
    plan = ports.service.store.get("plan")
    accepted = ports.service.ack(
        {
            "plan_id": "plan",
            "expected_revision": plan["revision"],
            "operation_id": "ack-accepted",
            "input_id": "input",
            "event_ids": [plan["inbox"][0]["id"]],
            "accepted_input": source,
        },
        USER,
    )
    assert accepted["status"] == "applied"
    assert ports.broker.invoke("cancel", {"idempotency_key": key})["status"] == "completed"
    assert ports.service.store.get("plan")["inbox"][0]["status"] == "applied"


def test_unknown_workflow_run_is_reconciliation_not_a_new_run(tmp_path: Path) -> None:
    ports = PublicOwners(tmp_path, [1000000])
    before = ports.workflow.invoke("definition.list", {})["definitions"]
    assert (
        ReviewWorkflow(ports).status("lost-owner", cancel=True)["status"]
        == "reconciliation_required"
    )
    assert ports.workflow.invoke("definition.list", {})["definitions"] == before
