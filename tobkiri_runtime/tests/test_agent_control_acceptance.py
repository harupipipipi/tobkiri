"""Real Workflow state plus isolated public saved-input and acceptance boundaries."""

from copy import deepcopy
import hashlib
from pathlib import Path
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
from core_runtime import host_provider_hooks_v4
from ecosystem.rumi_turn_runtime_pack.runtime.input_context import execute_with_input_context
from ecosystem.tobkiri_agent_control_pack.runtime.service import WorkPlanService
from ecosystem.tobkiri_agent_control_pack.runtime.store import PlanStore
from ecosystem.tobkiri_agent_control_pack.runtime.workflow import REVIEW, REVIEW_OP, WORKFLOW
from tests.test_agent_control_pack import FakePorts, USER, add, apply, configure
from tests.test_agent_control_pack import trigger
from tests.test_workflow_v4 import Authority, Catalog, Validator
from tobkiri_protocol.agent_inbox_v1 import CONTEXT_CONTRACT, INBOX_CONTRACT
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.saved_task_context import validate_saved_task_context


class WorkflowPorts(FakePorts):
    """Public contract adapter with actual durable Workflow engine and explicit AI fake."""

    provider: WorkflowProviderV4

    def invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if contract == WORKFLOW:
            return self.provider.invoke(operation, payload)
        return super().invoke(contract, operation, payload)


def workflow_rig(
    path: Path, *, waiting: bool = False
) -> tuple[WorkPlanService, WorkflowPorts, Authority]:
    """Build deterministic captured catalog, reserve/commit authority and selected ports."""
    ports = WorkflowPorts()
    service = WorkPlanService(PlanStore(path, "profile-a"), ports, clock=lambda: ports.now)
    catalog = Catalog()
    catalog.value["operations"][0].update(
        contract_id=REVIEW,
        operation_id=REVIEW_OP,
        function_principal_id="work-plan-review-provider",
    )
    authority = Authority(ApprovalState.WAITING_APPROVAL if waiting else ApprovalState.RESERVED)

    class Invoker:
        def invoke(self, request: Mapping[str, Any], *, authority: Any) -> InvocationOutcome:
            assert authority.dispatch_token.startswith("one-shot-")
            value = service.review(request["input"], USER)
            return InvocationOutcome(output=value)

        def cancel(self, request_id: str) -> None:
            pass

    ports.provider = WorkflowProviderV4(
        WorkflowEngineV4(
            store=WorkflowStoreV4(path / "workflow.sqlite3", clock=lambda: 100.0),
            catalog=catalog,
            authority=authority,
            invoker=Invoker(),
            validator=Validator(),
            clock=lambda: 100.0,
        )
    )
    return service, ports, authority


def dispatch(service: WorkPlanService) -> dict[str, Any]:
    plan = service.store.get("plan")
    return service.dispatch(
        {
            "action_id": "agent-control.review",
            "occurrence_id": "flow-occurrence",
            "payload": {"plan_id": "plan", "generation": plan["generation"]},
        },
        USER,
    )


def test_editable_real_workflow_dispatch_is_durable_and_occurrence_idempotent(
    tmp_path: Path,
) -> None:
    service, ports, authority = workflow_rig(tmp_path)
    configure(service)
    add(service)
    first = dispatch(service)
    assert first["status"] == "completed" and first["review_status"] == "drift"
    assert dispatch(service)["status"] == "completed"
    assert authority.commit_count == 1
    assert len(ports.provider.invoke("definition.list", {})["definitions"]) == 1
    assert len(service.store.get("plan")["inbox"]) == 1


def test_real_workflow_approval_wait_never_calls_model_or_reports_completed(tmp_path: Path) -> None:
    service, ports, authority = workflow_rig(tmp_path, waiting=True)
    configure(service)
    add(service)
    assert dispatch(service)["status"] == "waiting_approval"
    assert dispatch(service)["status"] == "waiting_approval"
    assert authority.commit_count == 0 and service.store.get("plan")["review"] is None
    assert not any("ai.generate" in call[0] for call in ports.calls)


def test_review_model_failure_remains_failed_when_workflow_operation_returned(
    tmp_path: Path,
) -> None:
    service, ports, _ = workflow_rig(tmp_path)
    configure(service)
    add(service)
    ports.fail = "tobkiri.service.ai.generate.v1"
    result = dispatch(service)
    assert result["status"] == "failed" and result["review_status"] == "review_failed"
    assert service.store.get("plan")["inbox"] == []


class ContextClient:
    """Selected public context/owner contracts; no private Pack state lookup."""

    profile_id = "profile-a"

    def __init__(self, service: WorkPlanService, ports: FakePorts) -> None:
        self.service, self.ports = service, ports
        self.session = self
        self.acks = 0

    def provider_metadata(self, contract: str) -> tuple[dict[str, str], ...]:
        return ({"operation_id": "context" if contract == CONTEXT_CONTRACT else "inbox"},)

    def invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = {k: v for k, v in payload.items() if k not in {"profile_id", "operation"}}
        if contract == CONTEXT_CONTRACT:
            plan = self.service.store.get("plan")
            values.update(
                plan_id="plan",
                expected_revision=plan["revision"],
                agent_binding="conversation:conversation",
            )
            return self.service.prepare_input(values, USER)
        if contract == INBOX_CONTRACT:
            self.acks += 1
            return self.service.ack(values, USER)
        return self.ports.invoke(contract, operation, payload)


def context_rig(path: Path) -> tuple[WorkPlanService, FakePorts, ContextClient]:
    ports = FakePorts()
    service = WorkPlanService(PlanStore(path, "profile-a"), ports, clock=lambda: ports.now)
    configure(service)
    apply(
        service,
        "remind.deliver",
        body="Review the failure",
        event_id="remind-one",
        agent_binding="conversation:conversation",
    )
    return service, ports, ContextClient(service, ports)


def test_canonical_input_hook_preserves_bytes_and_only_acks_actual_owner_acceptance(
    tmp_path: Path,
) -> None:
    service, ports, client = context_rig(tmp_path)
    source = {
        "request": {
            "turn_id": "turn-one",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "  原文\nunchanged  ",
        }
    }
    original = deepcopy(source)
    from ecosystem.tobkiri_agent_control_pack.runtime.ports import SAVED

    outcome = execute_with_input_context(
        source,
        client=client,
        guard=lambda: None,
        execute=lambda value: ports.invoke(SAVED, "saved", value),
    )
    assert source == original
    saved_call = next(call for call in ports.calls if call[0] == SAVED)
    assert saved_call[2]["request"]["content"] == original["request"]["content"]
    assert outcome["input_context_receipt"]["delivery_status"] == "applied"
    assert client.acks == 1 and service.store.get("plan")["inbox"][0]["status"] == "applied"


def test_no_acceptance_and_ack_failure_keep_delivery_pending_without_reexecuting(
    tmp_path: Path,
) -> None:
    service, _, client = context_rig(tmp_path)
    source = {
        "request": {
            "turn_id": "turn-one",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "original",
        }
    }
    outcome = execute_with_input_context(
        source, client=client, guard=lambda: None, execute=lambda _: {"status": "waiting_approval"}
    )
    assert outcome["status"] == "waiting_approval"
    assert outcome["input_context_receipt"]["delivery_status"] == "pending"
    assert client.acks == 0 and service.store.get("plan")["inbox"][0]["status"] == "received"


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipient_id", "other-agent"),
        ("input_id", "other-turn"),
        ("generation", True),
        ("approved", True),
    ],
)
def test_saved_context_rejects_scope_or_control_authority(
    tmp_path: Path, field: str, value: Any
) -> None:
    service, _, client = context_rig(tmp_path)
    prepared = client.invoke(
        CONTEXT_CONTRACT,
        "context",
        {
            "operation": "prepare_input_for_conversation",
            "conversation_id": "conversation",
            "input_id": "turn-one",
            "operation_id": "input-one",
            "boundary": "before_turn",
        },
    )
    context = prepared["task_context"]
    context[field] = value
    with pytest.raises(ValueError):
        validate_saved_task_context(context, conversation_id="conversation", turn_id="turn-one")
    with pytest.raises(ValueError):
        validate_saved_conversation_input(
            {
                "request": {
                    "turn_id": "turn-one",
                    "conversation_id": "conversation",
                    "conversation_revision": 1,
                    "content": "raw",
                    "task_context": context,
                }
            }
        )


def test_actual_saved_host_bridge_keeps_original_user_content_separate_from_context(
    tmp_path: Path,
) -> None:
    from ecosystem.defaultspack.runtime import saved_conversation as guest
    from tests.test_saved_host_exchange import _Exchange

    exchange = _Exchange(tmp_path)
    request = exchange.outer.payload["request"]
    original = deepcopy(request["content"])
    request["task_context"] = {
        "version": "tobkiri.saved-task-context.v1",
        "source_id": "plan",
        "conversation_id": request["conversation_id"],
        "recipient_id": "conversation:" + request["conversation_id"],
        "input_id": request["turn_id"],
        "source_revision": 1,
        "generation": 1,
        "items": [
            {
                "id": "instruction-one",
                "kind": "instruction",
                "body": "Preserve the Goal constraints.",
            }
        ],
    }
    exchange.intent = guest.start(request)
    for _ in range(4):
        exchange.step()
    exchange.host.finish(exchange.intent)
    source = exchange.store.get(request["conversation_id"])
    assert source["messages"][0]["content"] == original
    generated = next(
        payload
        for target, payload in exchange.calls
        if target[0] == "tobkiri.service.ai.generate.v1"
    )
    assert generated["messages"][-1]["role"] == "user"
    assert "lower-authority" in generated["messages"][-1]["content"]
    assert original == generated["messages"][-2]["content"]


def test_ack_retry_uses_immutable_input_identity_after_cas_revision_advanced(
    tmp_path: Path,
) -> None:
    service, ports, client = context_rig(tmp_path)
    source = {
        "request": {
            "turn_id": "turn-one",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "original",
        }
    }
    from ecosystem.tobkiri_agent_control_pack.runtime.ports import SAVED

    first = execute_with_input_context(
        source,
        client=client,
        guard=lambda: None,
        execute=lambda value: ports.invoke(SAVED, "saved", value),
    )
    assert first["input_context_receipt"]["delivery_status"] == "applied"
    # A retry reaches a retained canonical run: the external model/tool is not called again.
    second = execute_with_input_context(
        source, client=client, guard=lambda: None, execute=lambda _: {"status": "completed"}
    )
    assert second["input_context_receipt"]["delivery_status"] == "applied"
    assert ports.tools == 1 and service.store.get("plan")["inbox"][0]["status"] == "applied"


def test_reminder_job_status_proves_enqueue_and_cancel_fences_only_unapplied_event(
    tmp_path: Path,
) -> None:
    service, _, _ = context_rig(tmp_path)
    apply(
        service,
        "remind.create",
        reminder_id="manual",
        body="Check source",
        agent_binding="conversation:conversation",
        timezone="Asia/Tokyo",
        delay_seconds=600,
    )
    plan = service.store.get("plan")
    values = {
        "action_id": "agent-control.remind",
        "occurrence_id": "scheduled-one",
        "payload": {
            "plan_id": "plan",
            "reminder_id": "manual",
            "generation": plan["reminders"]["manual"]["generation"],
        },
    }
    assert service.reminder_job_status(values, USER)["status"] == "reconciliation_required"
    assert service.dispatch(values, USER)["status"] == "completed"
    assert service.reminder_job_status(values, USER) == {
        "status": "completed",
        "delivery_status": "pending",
    }
    assert service.reminder_job_status(values, USER, cancel=True)["status"] == "cancelled"
    assert service.reminder_job_status(values, USER)["status"] == "cancelled"


def test_snapshot_policy_captures_receipt_on_configuration_and_reuses_it(tmp_path: Path) -> None:
    service, ports, _ = context_rig(tmp_path)
    apply(service, "settings.configure", settings={"model_policy": {"mode": "snapshot"}})
    receipt = service.store.get("plan")["settings"]["snapshot_receipt"]
    assert receipt == {
        "contract_version": "tobkiri.secondary-model-policy.v1",
        "resolved_profile_id": "test-model",
        "thinking_level": "medium",
        "store_revision": 1,
    }
    apply(service, "settings.configure", settings={"interval_seconds": 300})
    assert service.store.get("plan")["settings"]["snapshot_receipt"] == receipt
    service = WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now)
    service.ports.resolve_model(service.store.get("plan")["settings"], ports.conversation)
    assert ports.calls[-1][2]["snapshot_receipt"] == receipt


def test_unavailable_snapshot_configuration_preserves_authoritative_settings(
    tmp_path: Path,
) -> None:
    service, ports, _ = context_rig(tmp_path)
    before = service.store.get("plan")
    ports.fail = "tobkiri.resource.ai.model.profile.v1"
    with pytest.raises(RuntimeError):
        apply(service, "settings.configure", settings={"model_policy": {"mode": "snapshot"}})
    assert service.store.get("plan") == before


def test_first_configuration_captures_snapshot_with_default_thinking_policy(tmp_path: Path) -> None:
    ports = FakePorts()
    service = WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now)
    apply(service, "settings.configure", settings={"model_policy": {"mode": "snapshot"}})
    assert service.store.get("plan")["settings"]["snapshot_receipt"]["thinking_level"] == "medium"


def test_client_cannot_inject_a_snapshot_receipt(tmp_path: Path) -> None:
    service, _, _ = context_rig(tmp_path)
    before = service.store.get("plan")
    with pytest.raises(PermissionError, match="snapshot receipt"):
        apply(
            service,
            "settings.configure",
            settings={"snapshot_receipt": {"resolved_profile_id": "forged"}},
        )
    assert service.store.get("plan") == before


@pytest.mark.parametrize("status", [None, "paused", "cancelled", "unavailable", "unconfigured"])
def test_saved_retry_recovers_immutable_projection_after_restart_without_preparing_again(
    tmp_path: Path, status: str | None
) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractClient
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import SAVED_CONTRACTS, execute_saved_turn
    from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
    from tests.test_saved_turn_coordinator import _Session

    class ProjectedSession(_Session):
        context_calls = 0
        context_status: str | None = "received"

        def provider_metadata(self, contract_id: str) -> tuple:
            if contract_id == CONTEXT_CONTRACT and self.context_status is not None:
                return ({"operation_id": "selected-context"},)
            return ()

        def invoke(self, contract_id: str, operation: str, payload: dict, **kwargs: Any) -> dict:
            if contract_id == CONTEXT_CONTRACT:
                self.context_calls += 1
                return {"status": self.context_status, "task_context": context, "instructions": []}
            return super().invoke(contract_id, operation, payload, **kwargs)

    session = ProjectedSession(tmp_path)
    source = deepcopy(session.initial)
    request = source["request"]
    context = {
        "version": "tobkiri.saved-task-context.v1",
        "source_id": "selected-plan",
        "conversation_id": request["conversation_id"],
        "recipient_id": f"conversation:{request['conversation_id']}",
        "input_id": request["turn_id"],
        "source_revision": 1,
        "generation": 1,
        "items": [{"id": "goal", "kind": "goal", "body": "Preserve exact requirement"}],
    }
    client = GlobalContractClient(
        session=session,
        allowed_contract_ids=SAVED_CONTRACTS,
        consumer_pack_id="rumi_turn_runtime_pack",
    )
    store = session.turns

    def execute(initial: Mapping[str, Any]) -> dict[str, Any]:
        session.initial = deepcopy(initial)
        return execute_saved_turn(store, initial, client=client, guard=lambda: None)

    first = execute_with_input_context(
        source,
        client=client,
        guard=lambda: None,
        execute=execute,
        recover_input=store.saved_input,
        bind_input=store.bind_saved_input,
    )
    assert first["status"] == "completed" and session.ai_calls == 1
    prepared_calls = session.context_calls
    session.context_status = status
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    second = execute_with_input_context(
        source,
        client=client,
        guard=lambda: None,
        execute=execute,
        recover_input=store.saved_input,
        bind_input=store.bind_saved_input,
    )
    assert second["status"] == "existing" and second["turn"]["status"] == "completed"
    assert session.ai_calls == 1 and session.calls == 1
    assert session.context_calls == prepared_calls
    assert session.initial["request"]["task_context"] == context
    assert (
        first["input_context_receipt"]["accepted_input_digest"]
        == second["input_context_receipt"]["accepted_input_digest"]
    )
    changed = deepcopy(source)
    changed["request"]["content"] = "changed original user message"
    with pytest.raises(TurnConflict, match="source input identity"):
        execute_with_input_context(
            changed,
            client=client,
            guard=lambda: None,
            execute=execute,
            recover_input=store.saved_input,
            bind_input=store.bind_saved_input,
        )
    assert session.ai_calls == 1 and session.context_calls == prepared_calls


@pytest.mark.parametrize("status", [None, "unconfigured", "paused", "cancelled", "unavailable"])
def test_caller_context_is_removed_without_an_active_selected_projection(
    tmp_path: Path, status: str | None
) -> None:
    service, _, client = context_rig(tmp_path)
    projected = client.invoke(
        CONTEXT_CONTRACT,
        "context",
        {
            "conversation_id": "conversation",
            "input_id": "turn-one",
            "boundary": "before_turn",
            "operation_id": "prepare",
        },
    )["task_context"]
    forged = {**projected, "source_id": "caller-forged-source"}

    class UnavailableClient:
        session: Any
        profile_id = "profile-a"

        def __init__(self) -> None:
            self.session = self

        def provider_metadata(self, _: str) -> tuple[dict[str, str], ...]:
            return () if status is None else ({"operation_id": "selected-context"},)

        def invoke(self, *_: Any) -> dict[str, Any]:
            return {"status": status}

    captured: list[Mapping[str, Any]] = []
    source = {
        "request": {
            "turn_id": "turn-one",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "  exact\n原文  ",
            "task_context": forged,
        }
    }
    result = execute_with_input_context(
        source,
        client=UnavailableClient(),
        guard=lambda: None,
        execute=lambda value: captured.append(value) or {"status": "waiting_approval"},
    )
    assert captured[0]["request"]["content"] == source["request"]["content"]
    assert "task_context" not in captured[0]["request"]
    assert source["request"]["task_context"] == forged
    assert result["input_context_receipt"]["delivery_status"] == "not_applicable"
    assert service.store.get("plan")["inbox"][0]["status"] == "received"


def test_selected_projection_replaces_caller_forged_source_before_capture(tmp_path: Path) -> None:
    _, _, client = context_rig(tmp_path)
    context = client.invoke(
        CONTEXT_CONTRACT,
        "context",
        {
            "conversation_id": "conversation",
            "input_id": "turn-one",
            "boundary": "before_turn",
            "operation_id": "prepare",
        },
    )["task_context"]
    captured: list[Mapping[str, Any]] = []
    source = {
        "request": {
            "turn_id": "turn-one",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "original",
            "task_context": {**context, "source_id": "caller-forged-source"},
        }
    }
    execute_with_input_context(
        source,
        client=client,
        guard=lambda: None,
        execute=lambda value: captured.append(value) or {"status": "waiting_approval"},
    )
    accepted = captured[0]["request"]["task_context"]
    assert accepted["source_id"] == context["source_id"]
    assert accepted["items"] == context["items"]
    assert accepted["source_revision"] >= context["source_revision"]
    assert source["request"]["task_context"]["source_id"] == "caller-forged-source"


@pytest.mark.parametrize(
    "pack,function",
    [
        ("tobkiri_agent_control_pack", f"tobkiri_agent_control_pack.work-plan.{kind}")
        for kind in (
            "resource",
            "action",
            "settings",
            "replace",
            "context",
            "inbox",
            "execute",
            "job",
            "review",
        )
    ]
    + [("rumi_turn_runtime_pack", "rumi_turn_runtime_pack.turn-runtime.saved")],
)
def test_real_host_loader_imports_factories_without_parent_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pack: str, function: str
) -> None:
    """Stub materialization verification while exercising the actual digest module loader."""
    pack_root = Path(__file__).resolve().parents[1] / "ecosystem" / pack
    implementation_path = "runtime/host.py"
    source = (pack_root / implementation_path).read_bytes()
    source_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    captured = SimpleNamespace(
        files=(SimpleNamespace(path=implementation_path, content=source),),
        implementation_path=implementation_path,
        materialization_digest=source_digest,
    )
    monkeypatch.setattr(
        host_provider_hooks_v4, "capture_materialized_artifact", lambda *_: captured
    )
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function, implementation_digest=source_digest)
    )
    factory = host_provider_hooks_v4.load_host_provider_factory(pack_root, binding)
    assert factory is not None and factory.function_id == function


def test_goal_only_new_scope_defaults_to_primary_and_independent_600_second_review(
    tmp_path: Path,
) -> None:
    ports = FakePorts()
    service = WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now)
    apply(service, "goal.set", goal_id="goal", body="Preserve requirements")
    plan = service.store.get("plan")
    assert plan["settings"]["executor"] == "conversation:conversation"
    assert plan["settings"]["reviewer"] == "review:conversation"
    assert plan["todos"] == []
    assert len(ports.schedules) == 1
    assert next(iter(ports.schedules.values()))["next_run_at_ms"] == ports.now + 600000


def test_goal_only_defaults_preserve_existing_off_and_explicit_role_configuration(
    tmp_path: Path,
) -> None:
    ports = FakePorts()
    service = WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now)
    apply(service, "settings.configure", settings={"enabled": False, "reviewer": "custom-review"})
    apply(service, "goal.set", goal_id="goal", body="Preserve requirements")
    plan = service.store.get("plan")
    assert plan["settings"]["enabled"] is False and plan["settings"]["reviewer"] == "custom-review"
    assert plan["schedule"]["status"] in {"paused", "unconfigured"}
    assert all(schedule["status"] != "scheduled" for schedule in ports.schedules.values())


def test_goal_only_unavailable_scheduler_is_saved_but_never_reported_monitored(
    tmp_path: Path,
) -> None:
    ports = FakePorts()
    ports.fail = "tobkiri.action.schedule.v1"
    service = WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now)
    apply(service, "goal.set", goal_id="goal", body="Preserve requirements")
    plan = service.store.get("plan")
    assert plan["goal"]["body"] == "Preserve requirements"
    assert plan["schedule"]["status"] == "binding_failed" and ports.schedules == {}


class CorrectionPorts(FakePorts):
    """Selected evidence/model outputs for correction lifecycle boundary cases."""

    refs = ["evidence-1"]
    resolved: list[str] = []
    instruction = "Repair the observed failure."

    def invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = super().invoke(contract, operation, payload)
        if contract == "tobkiri.service.ai.generate.v1" and "verdict" in value.get("output", {}):
            value["output"].update(
                evidence_refs=self.refs,
                resolved_finding_ids=self.resolved,
                instruction=self.instruction,
            )
        return value


def correction_rig(path: Path) -> tuple[WorkPlanService, CorrectionPorts]:
    """Create one tracked task with one actual public-source drift reference."""
    ports = CorrectionPorts()
    service = WorkPlanService(PlanStore(path, "profile-a"), ports, clock=lambda: ports.now)
    configure(service)
    add(service)
    assert trigger(service)["review_status"] == "drift"
    return service, ports


def append_evidence(ports: CorrectionPorts, revision: int, reference: str = "evidence-2") -> None:
    """Advance the fake public owner revision with a distinct actual message reference."""
    ports.conversation["conversation_revision"] = revision
    ports.conversation["messages"].append(
        {"id": reference, "role": "tool", "content": "test repaired"}
    )
    ports.refs = [reference]


def prepared_instructions(service: WorkPlanService, input_id: str) -> list[dict[str, Any]]:
    """Project the next exact input to inspect future guidance rather than history mutation."""
    plan = service.store.get("plan")
    return service.prepare_input(
        {
            "plan_id": "plan",
            "conversation_id": "conversation",
            "expected_revision": plan["revision"],
            "operation_id": f"prepare-{input_id}",
            "input_id": input_id,
            "agent_binding": "conversation:conversation",
            "boundary": "before_turn",
        },
        USER,
    )["instructions"]


def test_same_condition_coalesces_changed_evidence_and_instruction(tmp_path: Path) -> None:
    service, ports = correction_rig(tmp_path)
    first = deepcopy(service.store.get("plan")["inbox"][0])
    append_evidence(ports, 2)
    ports.instruction = "A revised wording for the same failure."
    assert trigger(service, "later-drift")["review_status"] == "drift"
    plan = service.store.get("plan")
    assert len(plan["inbox"]) == 1 and plan["inbox"][0] == first
    assert plan["review"]["id"] == first["finding_id"]
    assert plan["review"]["evidence_refs"] == ["evidence-2"]


def test_recheck_must_advance_past_latest_drift_after_storage_key_sorting(tmp_path: Path) -> None:
    service, ports = correction_rig(tmp_path)
    append_evidence(ports, 2)
    trigger(service, "a-newer-drift")
    plan = service.store.get("plan")
    service.store.mutate(
        "plan",
        plan["revision"],
        "canonical-history-key-order",
        {},
        USER.principal_id,
        lambda current: (
            current.update(review_occurrences=dict(sorted(current["review_occurrences"].items())))
            or {}
        ),
    )
    ports.verdict = "on_track"
    ports.resolved = [service.store.get("plan")["inbox"][0]["finding_id"]]
    ports.refs = ["evidence-1"]
    trigger(service, "recheck-without-newer-revision")
    assert not service.store.get("plan")["inbox"][0].get("resolved_at_ms")


def test_explicit_new_evidence_recheck_retires_future_guidance_and_retains_history(
    tmp_path: Path,
) -> None:
    service, ports = correction_rig(tmp_path)
    original = deepcopy(service.store.get("plan")["inbox"][0])
    ports.verdict = "on_track"
    ports.resolved = [original["finding_id"]]
    append_evidence(ports, 2)
    assert trigger(service, "recheck")["review_status"] == "on_track"
    plan = service.store.get("plan")
    assert plan["review"]["resolved_finding_ids"] == [original["finding_id"]]
    assert plan["review_occurrences"]["occurrence-1"]["verdict"] == "drift"
    assert (
        plan["inbox"][0]["body"] == original["body"]
        and plan["inbox"][0]["at_ms"] == original["at_ms"]
    )
    assert plan["inbox"][0]["resolution_review_occurrence"] == "recheck"
    assert prepared_instructions(service, "after-repair") == []
    restarted = WorkPlanService(PlanStore(tmp_path, "profile-a"), ports, clock=lambda: ports.now)
    assert prepared_instructions(restarted, "after-restart") == []


@pytest.mark.parametrize(
    "case", ["same_revision", "old_reference", "no_explicit_resolution", "unknown_finding"]
)
def test_unproved_recheck_does_not_retire_guidance(tmp_path: Path, case: str) -> None:
    service, ports = correction_rig(tmp_path)
    finding = service.store.get("plan")["inbox"][0]["finding_id"]
    ports.verdict = "on_track"
    ports.resolved = [finding]
    append_evidence(ports, 1 if case == "same_revision" else 2)
    if case == "old_reference":
        ports.refs = ["evidence-1"]
    elif case == "no_explicit_resolution":
        ports.resolved = []
    elif case == "unknown_finding":
        ports.resolved = ["forged-finding"]
    trigger(service, "unproved-recheck")
    plan = service.store.get("plan")
    assert not plan["inbox"][0].get("resolved_at_ms")
    assert len(prepared_instructions(service, "still-needs-repair")) == 1


def test_regression_after_resolution_creates_one_new_delivery_and_keeps_old_history(
    tmp_path: Path,
) -> None:
    service, ports = correction_rig(tmp_path)
    first = deepcopy(service.store.get("plan")["inbox"][0])
    ports.verdict = "on_track"
    ports.resolved = [first["finding_id"]]
    append_evidence(ports, 2)
    trigger(service, "repair-confirmed")
    ports.verdict = "drift"
    ports.resolved = []
    append_evidence(ports, 3, "evidence-3")
    trigger(service, "regression")
    trigger(service, "same-regression")
    plan = service.store.get("plan")
    assert len(plan["inbox"]) == 2
    old, new = plan["inbox"]
    assert old["id"] != new["id"] and old["finding_id"] == new["finding_id"]
    assert old["resolved_at_ms"] and not new.get("resolved_at_ms")
    assert [item["id"] for item in prepared_instructions(service, "new-regression-input")] == [
        new["id"]
    ]


def test_oversized_review_context_is_failed_before_any_model_generation(tmp_path: Path) -> None:
    service, ports = correction_rig(tmp_path)
    before = sum(call[0] == "tobkiri.service.ai.generate.v1" for call in ports.calls)
    ports.conversation["messages"][0]["content"] = "x" * (129 * 1024)
    assert trigger(service, "too-large")["review_status"] == "review_failed"
    assert sum(call[0] == "tobkiri.service.ai.generate.v1" for call in ports.calls) == before
