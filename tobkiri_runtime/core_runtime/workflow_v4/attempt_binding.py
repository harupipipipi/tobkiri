"""Bind attempt authority queries to sealed Workflow-owned request records."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.workflow_v4.models import WorkflowDenied, digest
from core_runtime.workflow_v4.store import WorkflowStoreV4


def authority_query(run: Mapping[str, Any], attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Project the exact public reservation query without authority material."""
    request = attempt["request"]
    return {
        "authority_api_version": "io.tobkiri.workflow-authority.v4",
        "workflow_id": run["definition_id"],
        "workflow_revision_digest": run["revision_digest"],
        "run_id": run["run_id"],
        "step_id": attempt["step_id"],
        "attempt_number": attempt["attempt_number"],
        "request_id": request["request_id"],
        "request_digest": attempt["request_digest"],
        "effect_digest": digest(request["effect_ceiling"]),
        "call_chain": request["call_chain"],
        "idempotency_key": request["idempotency_key"],
        "function_principal_id": request["function_principal_id"],
        "contract_id": request["contract_id"],
        "contract_revision_digest": request["contract_revision_digest"],
        "operation_id": request["operation_id"],
        "activation_id": run["activation_id"],
        "activation_digest": run["activation_digest"],
        "security_epoch": run["security_epoch"],
    }


def stored_attempt_for_query(
    store: WorkflowStoreV4, query: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve full input only from one authenticated local attempt record.

    The reservation projection deliberately omits provider input. Reconstructing
    it from arbitrary query fields would allow input substitution after compile.
    Store reads authenticate their records; every projected field and the full
    request digest must still agree before a Host port receives the request.
    """
    run_id = query.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise WorkflowDenied("Workflow attempt reservation binding is invalid")
    run = store.get_run(run_id)
    matches = [
        attempt
        for attempt in store.list_attempts(run_id)
        if attempt["step_id"] == query.get("step_id")
        and attempt["attempt_number"] == query.get("attempt_number")
    ]
    if len(matches) != 1:
        raise WorkflowDenied("Workflow attempt reservation is unavailable")
    attempt = matches[0]
    if (
        digest(dict(query)) != digest(authority_query(run, attempt))
        or digest(attempt["request"]) != attempt["request_digest"]
    ):
        raise WorkflowDenied("Workflow attempt reservation request changed")
    return run, attempt
