"""Isolated public-contract tests; not native-app or real-model evidence."""

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
import pytest
from ecosystem.tobkiri_agent_control_pack.runtime.inbox import (
    acknowledge,
    prepare_input,
)
from ecosystem.tobkiri_agent_control_pack.runtime.ports import (
    SAVED,
    CONVERSATION,
    GENERATE,
    MODEL,
)
from ecosystem.tobkiri_agent_control_pack.runtime.schedules import (
    SCHEDULE_ACTION,
    SCHEDULE_RESOURCE,
)
from ecosystem.tobkiri_agent_control_pack.runtime.service import Actor, WorkPlanService
from ecosystem.tobkiri_agent_control_pack.runtime.store import Conflict, PlanStore
from ecosystem.rumi_agent_runtime_service_pack.runtime.runtime import _task_context
from tobkiri_protocol.canonical import canonical_digest

USER = Actor(
    "operator",
    frozenset(
        {
            "plan.manage",
            "plan.configure",
            "goal.replace",
            "plan.execute",
            "plan.review",
            "inbox.consume",
        }
    ),
)


class FakePorts:
    """Selected contracts with deterministic external IO and a callable test tool."""

    def __init__(self) -> None:
        self.now = 1000000
        self.schedules: dict[str, Any] = {}
        self.revision = 0
        self.calls: list[Any] = []
        self.conversation = {
            "id": "conversation",
            "conversation_revision": 1,
            "model": "test-model",
            "messages": [{"id": "evidence-1", "role": "tool", "content": "test failed"}],
        }
        self.verdict = "drift"
        self.fail: str | None = None
        self.status = "completed"
        self.tools = 0
        self.callback: Any = None
        self.receipts: dict[str, Any] = {}

    def invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((contract, operation, deepcopy(dict(payload))))
        if contract == self.fail:
            raise RuntimeError("offline adapter")
        if contract == CONVERSATION:
            if payload["operation"] == "saved_receipt":
                return {"receipt": deepcopy(self.receipts.get(payload["turn_id"]))}
            return {"conversation": deepcopy(self.conversation)}
        if contract == SCHEDULE_RESOURCE:
            return {
                "revision": self.revision,
                "schedules": deepcopy(list(self.schedules.values())),
            }
        if contract == SCHEDULE_ACTION:
            assert payload["expected_revision"] == self.revision
            key, action = payload["schedule_id"], payload["operation"]
            if action == "create":
                assert key not in self.schedules
                self.schedules[key] = {
                    "id": key,
                    "status": "scheduled",
                    **{
                        k: deepcopy(payload[k])
                        for k in (
                            "payload",
                            "action_id",
                            "next_run_at_ms",
                            "interval_ms",
                        )
                    },
                }
            elif action == "update":
                self.schedules[key].update(payload["updates"])
            else:
                self.schedules[key]["status"] = {
                    "pause": "paused",
                    "cancel": "cancelled",
                    "resume": "scheduled",
                }[action]
            self.revision += 1
            return {
                "revision": self.revision,
                "schedule": deepcopy(self.schedules[key]),
            }
        if contract == MODEL:
            return {
                "contract_version": "tobkiri.secondary-model-policy.v1",
                "resolved_profile_id": "test-model",
                "thinking_level": "medium",
                "store_revision": 1,
            }
        if contract == GENERATE:
            if self.callback:
                self.callback()
            if payload["request_id"].startswith("compact-"):
                return {"status": "ok", "output": {"summary": "source summary"}}
            return {
                "status": "ok",
                "output": {
                    "verdict": self.verdict,
                    "evidence_refs": ["evidence-1"],
                    "condition_id": "repair-test",
                    "todo_ids": ["one"],
                    "instruction": "Return to the failing test.",
                },
            }
        if contract == SAVED:
            self.tools += 1
            request = payload["request"]
            assert request["task_context"]["version"] == "tobkiri.saved-task-context.v1"
            ids = [
                "message:"
                + canonical_digest(
                    [request["conversation_id"], request["turn_id"], role]
                ).removeprefix("sha256:")
                for role in ("user", "assistant")
            ]
            reference = {
                "assistant_message_id": ids[1],
                "conversation_id": request["conversation_id"],
                "conversation_revision": request["conversation_revision"] + 2,
                "user_message_id": ids[0],
                "outcome_digest": canonical_digest("fixture"),
            }
            receipt = {
                "turn_id": request["turn_id"],
                "conversation_id": request["conversation_id"],
                "initial_revision": request["conversation_revision"],
                "user_revision": request["conversation_revision"] + 1,
                "user_message_id": ids[0],
                "assistant_message_id": ids[1],
                "input_digest": canonical_digest(payload),
                "result_reference": reference,
            }
            self.receipts[request["turn_id"]] = receipt
            self.conversation["messages"].append(
                {
                    "id": ids[1],
                    "parent_id": ids[0],
                    "role": "assistant",
                    "content": "finished",
                    "tool_logs": [
                        {
                            "tool_name": "test-tool",
                            "tool_call_id": "tool-one",
                            "result": '{"status":"ok"}',
                        }
                    ],
                }
            )
            return {
                "status": self.status,
                "turn": {
                    "id": request["turn_id"],
                    "input_digest": receipt["input_digest"],
                    "result_reference": reference,
                },
            }
        raise AssertionError((contract, operation))


@pytest.fixture
def rig(tmp_path: Path) -> tuple[WorkPlanService, FakePorts]:
    ports = FakePorts()
    return WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now), ports


def apply(service: WorkPlanService, operation: str, **fields: Any) -> dict[str, Any]:
    plan = service.store.get("plan") or {}
    return service.action(
        {
            "operation": operation,
            "plan_id": "plan",
            "conversation_id": "conversation",
            "operation_id": f"op-{plan.get('revision', 0)}",
            "expected_revision": plan.get("revision", 0),
            **fields,
        },
        USER,
    )


def configure(service: WorkPlanService) -> None:
    apply(
        service,
        "settings.configure",
        settings={"executor": "conversation:conversation", "reviewer": "reviewer"},
    )


def add(service: WorkPlanService, item_id: str = "one", **extra: Any) -> dict[str, Any]:
    return apply(
        service,
        "todo.add",
        item_id=item_id,
        body=f"Work {item_id}",
        criteria=["tool_success:test-tool"],
        **extra,
    )


def run(service: WorkPlanService, key: str = "run-one") -> dict[str, Any]:
    plan = service.store.get("plan")
    return service.run_next(
        {"plan_id": "plan", "expected_revision": plan["revision"], "operation_id": key},
        USER,
    )


def trigger(service: WorkPlanService, key: str = "occurrence-1") -> dict[str, Any]:
    plan = service.store.get("plan")
    return service.review(
        {
            "occurrence_id": key,
            "plan_id": "plan",
            "generation": plan["generation"],
        },
        USER,
    )


def test_absent_reads_are_side_effect_free(rig: Any) -> None:
    service, ports = rig
    assert service.store.get("plan") is None and service.store.list() == []
    assert not service.store.path.exists() and not ports.schedules


def test_goal_only_and_100_todos_share_one_600_second_timer_without_postponement(
    rig: Any,
) -> None:
    service, ports = rig
    configure(service)
    apply(
        service,
        "goal.set",
        goal_id="goal",
        body="Fix tests",
        constraints=["ask before push"],
    )
    for index in range(100):
        add(service, f"todo-{index}")
        ports.now += 1000
    assert len(ports.schedules) == 1
    schedule = next(iter(ports.schedules.values()))
    assert schedule["next_run_at_ms"] == 1600000 and schedule["interval_ms"] == 600000
    assert service.store.get("plan")["goal"]["constraints"] == ["ask before push"]


def test_off_pause_and_interval_changes_keep_explicit_policy(rig: Any) -> None:
    service, ports = rig
    configure(service)
    add(service)
    apply(service, "settings.configure", settings={"interval_seconds": 120})
    apply(service, "settings.configure", settings={"interval_seconds": 1200})
    assert next(iter(ports.schedules.values()))["next_run_at_ms"] == 1120000
    apply(service, "settings.configure", settings={"enabled": False})
    add(service, "two")
    apply(service, "plan.pause")
    apply(service, "plan.resume")
    assert service.store.get("plan")["settings"]["enabled"] is False
    assert next(iter(ports.schedules.values()))["status"] == "paused"


def test_schedule_failure_is_not_monitored_and_retries_use_same_record(
    rig: Any,
) -> None:
    service, ports = rig
    configure(service)
    ports.fail = SCHEDULE_ACTION
    add(service)
    assert service.store.get("plan")["schedule"]["status"] == "binding_failed"
    ports.fail = None
    service.reconcile_schedule("plan")
    assert service.store.get("plan")["schedule"]["status"] == "scheduled"


def test_dependency_cycle_and_unknown_link_rollback(rig: Any) -> None:
    service, _ = rig
    add(service)
    add(service, "two", dependencies=["one"])
    before = service.store.get("plan")
    with pytest.raises(ValueError, match="cycle"):
        apply(service, "todo.update", item_id="one", updates={"dependencies": ["two"]})
    with pytest.raises(ValueError, match="unknown"):
        add(service, "bad", dependencies=["missing"])
    assert service.store.get("plan") == before


def test_dependency_order_runs_actual_test_callable_without_timer_wait(
    rig: Any,
) -> None:
    service, ports = rig
    configure(service)
    add(service)
    add(service, "two", dependencies=["one"])
    assert run(service)["status"] == "done"
    assert run(service, "run-two")["status"] == "done"
    assert ports.tools == 2 and ports.now == 1000000


@pytest.mark.parametrize(
    "status,expected",
    [
        ("waiting_approval", "blocked"),
        ("waiting_user", "blocked"),
        ("cancelled", "cancelled"),
        ("failed", "blocked"),
    ],
)
def test_wait_cancel_failure_are_not_completed(rig: Any, status: str, expected: str) -> None:
    service, ports = rig
    configure(service)
    add(service)
    ports.status = status
    assert run(service)["status"] == expected


def test_natural_language_self_report_without_supported_receipts_is_unverified(
    rig: Any,
) -> None:
    service, _ = rig
    configure(service)
    apply(service, "todo.add", item_id="one", body="Work", criteria=["natural condition"])
    assert run(service)["status"] == "needs_review"


def test_ambiguous_result_and_restart_never_reissue_uncertain_tool(rig: Any) -> None:
    service, ports = rig
    configure(service)
    add(service)
    ports.fail = SAVED
    run(service)
    restarted = WorkPlanService(PlanStore(service.store.path.parents[4], "profile-a"), ports)
    assert restarted.store.path == service.store.path
    assert run(restarted)["status"] == "waiting" and ports.tools == 0


@pytest.mark.parametrize("verdict", ["on_track", "blocked", "unverifiable"])
def test_only_drift_delivers_executor_guidance(rig: Any, verdict: str) -> None:
    service, ports = rig
    configure(service)
    add(service)
    ports.verdict = verdict
    assert trigger(service)["review_status"] == verdict
    assert service.store.get("plan")["inbox"] == []


def test_drift_replay_and_unresolved_finding_coalesce_and_ack_is_not_resolution(
    rig: Any,
) -> None:
    service, ports = rig
    configure(service)
    add(service)
    trigger(service)
    trigger(service)
    trigger(service, "occurrence-2")
    plan = service.store.get("plan")
    assert (
        len(plan["inbox"]) == 1 and plan["inbox"][0]["agent_binding"] == "conversation:conversation"
    )
    accepted = service.prepare_input(
        {
            "plan_id": "plan",
            "conversation_id": "conversation",
            "expected_revision": plan["revision"],
            "operation_id": "input-one",
            "input_id": "turn-one",
            "agent_binding": "conversation:conversation",
            "boundary": "before_turn",
        },
        USER,
    )
    initial = {
        "request": {
            "turn_id": "turn-one",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "Keep original bytes",
            "task_context": accepted["task_context"],
        }
    }
    ports.receipts["turn-one"] = {
        "turn_id": "turn-one",
        "conversation_id": "conversation",
        "initial_revision": 1,
        "user_revision": 2,
        "input_digest": canonical_digest(initial),
        "user_message_id": "message:"
        + canonical_digest(["conversation", "turn-one", "user"]).removeprefix("sha256:"),
    }
    service.ack(
        {
            "plan_id": "plan",
            "expected_revision": accepted["plan"]["revision"],
            "operation_id": "ack-one",
            "accepted_input": initial,
            "input_id": "turn-one",
            "event_ids": [plan["inbox"][0]["id"]],
        },
        USER,
    )
    assert service.store.get("plan")["review"]["verdict"] == "drift"
    assert len([x for x in ports.calls if x[0] == GENERATE]) == 2


def test_model_failure_remains_review_failed(rig: Any) -> None:
    service, ports = rig
    configure(service)
    add(service)
    ports.fail = GENERATE
    assert trigger(service)["review_status"] == "review_failed"
    assert not service.store.get("plan")["inbox"]


def test_edit_during_review_rejects_late_finding(rig: Any) -> None:
    service, ports = rig
    configure(service)
    add(service)
    ports.callback = lambda: add(service, "two")
    assert trigger(service)["status"] == "cancelled"
    assert not service.store.get("plan")["inbox"]


def test_manual_scheduled_instruction_uses_same_inbox_without_review(rig: Any) -> None:
    service, ports = rig
    configure(service)
    apply(
        service,
        "remind.create",
        reminder_id="manual",
        body="Continue",
        agent_binding="conversation:conversation",
        timezone="Asia/Tokyo",
        delay_seconds=600,
    )
    reminder = service.store.get("plan")["reminders"]["manual"]
    values = {
        "action_id": "agent-control.remind",
        "occurrence_id": "manual-one",
        "payload": {
            "plan_id": "plan",
            "reminder_id": "manual",
            "generation": reminder["generation"],
        },
    }
    assert service.dispatch(values, USER)["status"] == "completed"
    assert not any(x[0] == GENERATE for x in ports.calls)
    apply(service, "remind.cancel", reminder_id="manual")
    assert (
        service.dispatch({**values, "occurrence_id": "manual-two"}, USER)["status"] == "cancelled"
    )


def test_extra_instruction_does_not_replace_goal_or_recompact(rig: Any) -> None:
    service, ports = rig
    apply(service, "goal.set", goal_id="goal", body="Fix tests")
    before = service.store.get("plan")
    apply(
        service,
        "remind.deliver",
        event_id="extra",
        body="Start with X",
        agent_binding="conversation:conversation",
    )
    assert service.store.get("plan")["goal"] == before["goal"]
    assert service.store.get("plan")["context"] == before["context"]
    assert not any(x[0] == GENERATE for x in ports.calls)


def test_whole_goal_commit_is_atomic_with_checkpoint_and_compaction_failure_rollback(
    rig: Any,
) -> None:
    service, ports = rig
    configure(service)
    apply(service, "goal.set", goal_id="goal", body="Old", constraints=["ask before push"])
    preview = apply(service, "goal.prepare_replace", preview_id="preview", body="New")["preview"]
    before = service.store.get("plan")
    ports.fail = GENERATE
    with pytest.raises(RuntimeError):
        apply(
            service,
            "goal.commit_replace",
            preview_id="preview",
            preview_digest=preview["digest"],
        )
    assert service.store.get("plan") == before
    ports.fail = None
    result = apply(
        service,
        "goal.commit_replace",
        preview_id="preview",
        preview_digest=preview["digest"],
    )
    assert result["plan"]["goal"]["body"] == "New"
    assert result["plan"]["goal"]["constraints"] == ["ask before push"]
    assert result["plan"]["context"]["goal_revision"] == result["plan"]["goal"]["revision"]
    assert result["plan"]["history"][-1]["checkpoint"]["goal"]["body"] == "Old"


def test_client_approved_does_not_delegate_settings_or_goal_replace(rig: Any) -> None:
    service, _ = rig
    actor = Actor("agent", frozenset({"plan.manage"}))
    with pytest.raises(PermissionError):
        service.action(
            {
                "operation": "settings.configure",
                "plan_id": "plan",
                "conversation_id": "conversation",
                "operation_id": "off",
                "expected_revision": 0,
                "approved": True,
                "settings": {"enabled": False},
            },
            actor,
        )
    assert service.store.get("plan") is None


def test_idempotency_rebinding_revision_and_conversation_scope(rig: Any) -> None:
    service, _ = rig
    values = {
        "operation": "todo.add",
        "plan_id": "plan",
        "conversation_id": "conversation",
        "operation_id": "same",
        "expected_revision": 0,
        "item_id": "one",
        "body": "One",
    }
    service.action(values, USER)
    assert service.action(values, USER)["deduplicated"]
    with pytest.raises(Conflict, match="rebound"):
        service.action({**values, "body": "different"}, USER)
    with pytest.raises(Conflict, match="stale"):
        service.action({**values, "operation_id": "other"}, USER)
    with pytest.raises(PermissionError):
        apply(
            service,
            "remind.deliver",
            conversation_id="other",
            event_id="extra",
            body="x",
            agent_binding="conversation:conversation",
        )


def test_wrong_recipient_unsafe_boundary_and_stale_ack(rig: Any) -> None:
    service, _ = rig
    add(service)
    plan = service.store.get("plan")
    with pytest.raises(PermissionError):
        service.prepare_input(
            {
                "plan_id": "plan",
                "conversation_id": "conversation",
                "expected_revision": plan["revision"],
                "operation_id": "bad",
                "input_id": "turn",
                "agent_binding": "conversation:conversation",
                "boundary": "before_turn",
            },
            Actor("outsider", frozenset({"inbox.consume"})),
        )
    with pytest.raises(ValueError):
        prepare_input(
            plan,
            {
                "agent_binding": "conversation:conversation",
                "input_id": "turn",
                "boundary": "inside_tool",
            },
            0,
        )
    with pytest.raises(Conflict):
        acknowledge(plan, {"input_id": "turn", "event_ids": ["missing"]})


def test_task_context_rejects_authority_fields() -> None:
    value = {
        "version": "tobkiri.context-projection.v1",
        "instructions": [{"body": "work"}],
    }
    assert _task_context(value) == value
    with pytest.raises(ValueError):
        _task_context({**value, "approved": True})
