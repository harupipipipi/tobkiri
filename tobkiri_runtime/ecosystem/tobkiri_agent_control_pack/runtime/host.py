"""Exact captured Host functions; Profile, actor, root and grants are not payloads."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.agent_inbox_v1 import CONTEXT_CONTRACT, INBOX_CONTRACT
from tobkiri_protocol.work_plan_v1 import (
    ACTION_CONTRACT,
    REPLACE_CONTRACT,
    RESOURCE_CONTRACT,
)
from .ports import SAVED, CONVERSATION, GENERATE, MODEL
from .schedules import SCHEDULE_ACTION, SCHEDULE_RESOURCE
from .service import Actor, WorkPlanService
from .store import PlanStore, digest, identifier, new_plan
from .workflow import WORKFLOW, REVIEW, ReviewWorkflow

PACK_ID = "tobkiri_agent_control_pack"
CONTRACTS = {
    "resource": (RESOURCE_CONTRACT, "work-plan-resource"),
    "action": (ACTION_CONTRACT, "work-plan-action"),
    "settings": ("tobkiri.action.work-plan.settings.v1", "work-plan-settings"),
    "replace": (REPLACE_CONTRACT, "context-replace"),
    "context": (CONTEXT_CONTRACT, "context-project"),
    "inbox": (INBOX_CONTRACT, "inbox-action"),
    "execute": ("tobkiri.action.work-plan.execute.v1", "work-plan-execute"),
    "job": ("tobkiri.action.job.adapter.v1", "work-plan-job"),
    "review": (REVIEW, "work-plan-review"),
}
DEPENDENCIES = frozenset(
    {CONVERSATION, SCHEDULE_ACTION, SCHEDULE_RESOURCE, MODEL, GENERATE, SAVED, WORKFLOW}
)
_ACTIONS = {
    "goal.set",
    "goal.refine",
    "goal.prepare_replace",
    "goal.cancel_replace",
    "todo.add",
    "todo.update",
    "todo.cancel",
    "remind.deliver",
    "remind.create",
    "remind.cancel",
}
_SETTINGS = {"settings.configure", "plan.pause", "plan.cancel", "plan.resume"}


class WorkPlanHostFactory:
    """Capture only one declared operation and a Profile-owned local store."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.contract_id, suffix = CONTRACTS[kind]
        self.operation_id = f"{PACK_ID}.{suffix}"
        self.function_id = f"{PACK_ID}.work-plan.{kind}"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Reject foreign functions, contracts, domain and missing Profile roots."""
        if (
            not context.profile_id
            or context.user_data_root is None
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("work-plan capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != self.function_id
            or operation.contract_id != self.contract_id
            or operation.operation_id != self.operation_id
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("work-plan capture identity is invalid")
        domain = context.domain_ids.get(
            (self.contract_id, self.operation_id, binding.principal_ref.value)
        )
        if not domain:
            raise PermissionError("work-plan domain is unavailable")
        store = PlanStore(context.user_data_root, context.profile_id)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                operation_id != self.operation_id
                or payload.get("profile_id", context.profile_id) != context.profile_id
            ):
                raise PermissionError("work-plan invocation scope is invalid")
            values = {k: v for k, v in payload.items() if k not in {"profile_id", "_session_id"}}
            actor_id = invocation.presentation_owner_principal_id
            # Grants derive from the exact Broker-admitted operation; selecting a
            # model, supplying approved:true or writing actor text grants nothing.
            grants = {
                "action": {"plan.manage"},
                "settings": {"plan.manage", "plan.configure"},
                "replace": {"plan.manage", "goal.replace"},
                "context": {"inbox.consume"},
                "inbox": {"inbox.consume"},
                "execute": {"plan.execute"},
                "job": {"plan.review"},
                "review": {"plan.review"},
                "resource": set(),
            }[self.kind]
            actor = Actor(actor_id, frozenset(grants))
            if self.kind == "resource":
                if values == {"operation": "list"}:
                    return {"plans": store.list()}
                if set(values) == {"operation", "plan_id"} and values["operation"] == "get":
                    return {"plan": _display(store.get(identifier(values["plan_id"])))}
                if (
                    set(values) == {"operation", "conversation_id"}
                    and values["operation"] == "get_for_conversation"
                ):
                    plan = next(
                        (
                            p
                            for p in store.list()
                            if p["conversation_id"] == values["conversation_id"]
                        ),
                        None,
                    )
                    if plan is None:
                        plan = new_plan(
                            f"plan-{digest(values['conversation_id'])[:24]}",
                            context.profile_id,
                        )
                        plan["conversation_id"] = values["conversation_id"]
                    return {"plan": _display(plan)}
                raise ValueError("work-plan read fields are invalid")
            client = invocation.contract_client(
                allowed_contract_ids=DEPENDENCIES,
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            service = WorkPlanService(store, client, guard=invocation.assert_current)
            values.setdefault(
                "operation_id",
                f"request-{digest(invocation.envelope.context.request_id)[:24]}",
            )
            if "conversation_id" in values:
                values.setdefault("plan_id", f"plan-{digest(values['conversation_id'])[:24]}")
            if values.get("operation") == "goal.set":
                values.setdefault("goal_id", f"goal-{digest(values['plan_id'])[:24]}")
            if values.get("operation") == "goal.prepare_replace":
                values.setdefault("preview_id", f"preview-{digest(values['operation_id'])[:24]}")
            if values.get("operation") == "todo.add":
                values.setdefault("item_id", f"todo-{digest(values['operation_id'])[:24]}")
            if values.get("operation") == "remind.deliver":
                values.setdefault("event_id", f"event-{digest(values['operation_id'])[:24]}")
            if values.get("operation") == "settings.configure" and "settings" not in values:
                values["settings"] = {
                    key: values.pop(key)
                    for key in ("executor", "reviewer", "enabled", "interval_seconds")
                    if key in values
                }
                if isinstance(values["settings"].get("interval_seconds"), str):
                    raw = values["settings"]["interval_seconds"]
                    if not raw.isdecimal():
                        raise ValueError("review interval must be seconds")
                    values["settings"]["interval_seconds"] = int(raw)
            if self.kind in {"action", "settings", "replace"}:
                allowed = (
                    _ACTIONS
                    if self.kind == "action"
                    else _SETTINGS
                    if self.kind == "settings"
                    else {"goal.commit_replace"}
                )
                if values.get("operation") not in allowed:
                    raise PermissionError("work-plan action does not match its authority")
                return service.action(values, actor)
            if self.kind == "context":
                if values.get("operation") == "prepare_input_for_conversation":
                    plan = next(
                        (
                            p
                            for p in store.list()
                            if p["conversation_id"] == values["conversation_id"]
                        ),
                        None,
                    )
                    if plan is None:
                        return {"status": "unconfigured"}
                    values.update(
                        plan_id=plan["id"],
                        expected_revision=plan["revision"],
                        agent_binding=plan["settings"]["executor"],
                    )
                    values["operation"] = "prepare_input"
                if values.pop("operation", None) != "prepare_input":
                    raise ValueError("context operation is invalid")
                return service.prepare_input(values, actor)
            if self.kind == "inbox":
                if values.pop("operation", None) != "ack":
                    raise ValueError("inbox operation is invalid")
                return service.ack(values, actor)
            if self.kind == "execute":
                if values.pop("operation", None) != "run_next":
                    raise ValueError("execution operation is invalid")
                return service.run_next(values, actor)
            if self.kind == "review":
                if values.pop("operation", None) != "inspect":
                    raise ValueError("review operation is invalid")
                return service.review(values, actor)
            if values.get("operation") == "describe":
                return {"action_ids": ["agent-control.review", "agent-control.remind"]}
            if values.get("operation") in {"status", "cancel"}:
                job = values.get("payload", {})
                plan = store.get(identifier(job["plan_id"]))
                if not plan:
                    return {"status": "cancelled"}
                key = f"occurrence-{__import__('hashlib').sha256(str(values.get('idempotency_key')).encode()).hexdigest()[:24]}"
                if values.get("action_id") == "agent-control.review":
                    return ReviewWorkflow(client).status(
                        key, cancel=values["operation"] == "cancel"
                    )
                if values["operation"] == "cancel":
                    return {
                        "status": "cancelled"
                        if plan["status"] != "active"
                        else "cancellation_requested"
                    }
                finding = plan["review_occurrences"].get(key)
                return {
                    "status": "completed" if finding else "reconciliation_required",
                    "finding": finding,
                }
            if values.pop("operation", None) != "dispatch":
                raise ValueError("job operation is invalid")
            values["occurrence_id"] = (
                f"occurrence-{__import__('hashlib').sha256(str(values['idempotency_key']).encode()).hexdigest()[:24]}"
            )
            return service.dispatch(values, actor)

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=self.contract_id,
                    contract_version="1.0.0",
                    operation_id=self.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    f"{PACK_ID}.work-plan.{kind}": WorkPlanHostFactory(kind) for kind in CONTRACTS
}


def _display(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        **dict(plan),
        "todo_summary": "\n".join(
            f"{item['id']}: {item['body']} ({item['status']})" for item in plan["todos"]
        ),
        "review_status": (plan.get("review") or {}).get("verdict", "unverified"),
    }
