"""Real Workflow state plus isolated public saved-input and acceptance boundaries."""

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from core_runtime.workflow_v4 import (
    ApprovalState,
    WorkflowEngineV4,
    WorkflowProviderV4,
    WorkflowStoreV4,
)
from core_runtime.workflow_v4.models import InvocationOutcome
from ecosystem.rumi_turn_runtime_pack.runtime.input_context import execute_with_input_context
from ecosystem.tobkiri_agent_control_pack.runtime.service import WorkPlanService
from ecosystem.tobkiri_agent_control_pack.runtime.store import PlanStore
from ecosystem.tobkiri_agent_control_pack.runtime.workflow import REVIEW, REVIEW_OP, WORKFLOW
from tests.test_agent_control_pack import FakePorts, USER, add, apply, configure
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
