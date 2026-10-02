"""Editable review Flow using only the selected public Workflow v4 contract."""

from __future__ import annotations

from typing import Any, Mapping

from .store import digest

WORKFLOW = "tobkiri.workflow.v4"
REVIEW = "tobkiri.action.work-plan.review.v1"
REVIEW_OP = "tobkiri_agent_control_pack.work-plan-review"
DEFINITION = "agent-control-review-v1"


class ReviewWorkflow:
    """Bind the initial small Flow to current catalog targets; retain user edits."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Call an exact public operation without another Pack's private state."""
        return dict(self.client.invoke(WORKFLOW, operation, payload))

    def ensure(self) -> dict[str, Any]:
        """Create and publish the first Flow, never overwrite an existing definition."""
        definitions = self.invoke("definition.list", {})["definitions"]
        existing = next((x for x in definitions if x["definition_id"] == DEFINITION), None)
        if existing is not None:
            if existing["state"] != "published":
                raise RuntimeError("editable review Flow is not published")
            return dict(existing)
        palette = self.invoke("operation.palette", {})
        matches = [
            x
            for x in palette["operations"]
            if x["contract_id"] == REVIEW and x["operation_id"] == REVIEW_OP
        ]
        if len(matches) != 1:
            raise RuntimeError("review Flow operation is unavailable or ambiguous")
        target = matches[0]
        document = {
            "workflow_api_version": "io.tobkiri.workflow.v4",
            "name": "Tobkiri independent work-plan review",
            "max_concurrency": 1,
            "steps": [
                {
                    "id": "inspect",
                    "request": {
                        key: target[key]
                        for key in (
                            "contract_id",
                            "contract_revision_digest",
                            "operation_id",
                            "function_principal_id",
                        )
                    }
                    | {
                        "input": {
                            "operation": "inspect",
                            "plan_id": "${inputs.plan_id}",
                            "generation": "${inputs.generation}",
                            "occurrence_id": "${inputs.occurrence_id}",
                        }
                    },
                    "retry": {"max_attempts": 1, "backoff_ms": 0},
                }
            ],
        }
        created = self.invoke(
            "definition.create",
            {
                "definition_id": DEFINITION,
                "document": document,
            },
        )
        return self.invoke(
            "definition.publish",
            {
                "definition_id": DEFINITION,
                "if_match": created["etag"],
            },
        )

    def run(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Run one scheduler occurrence with durable Workflow deduplication."""
        self.ensure()
        run_id = "review-flow-" + digest(values["occurrence_id"])[:24]
        inputs = {**dict(values["payload"]), "occurrence_id": values["occurrence_id"]}
        try:
            self.invoke(
                "run.create",
                {
                    "definition_id": DEFINITION,
                    "run_id": run_id,
                    "occurrence_id": values["occurrence_id"],
                    "inputs": inputs,
                },
            )
        except Exception:
            retained = self.invoke("run.get", {"run_id": run_id})["run"]
            if (
                retained.get("definition_id") != DEFINITION
                or retained.get("occurrence_id") != values["occurrence_id"]
                or retained.get("inputs") != inputs
            ):
                raise ValueError("retained review Flow input does not match")
        current = self.invoke("run.get", {"run_id": run_id})
        # Restart never retries an ambiguous dispatching/running StepAttempt.
        run = current["run"]
        if run["state"] in {"queued", "running"} and not current["attempts"]:
            current = self.invoke("run.advance", {"run_id": run_id})
            run = current["run"]
        state = run["state"]
        status = {
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "timed_out": "failed",
            "waiting_approval": "waiting_approval",
            "paused": "waiting",
            "queued": "accepted",
            "running": "running",
        }.get(state, "reconciliation_required")
        return {"status": status, "workflow_run_id": run_id, "workflow_state": state}

    def status(self, occurrence_id: str, *, cancel: bool = False) -> dict[str, Any]:
        """Read or cancel the canonical Workflow owner state."""
        run_id = "review-flow-" + digest(occurrence_id)[:24]
        if cancel:
            self.invoke("run.cancel", {"run_id": run_id})
        current = self.invoke("run.get", {"run_id": run_id})
        state = current["run"]["state"]
        return {
            "status": {
                "succeeded": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "waiting_approval": "waiting_approval",
                "paused": "waiting",
            }.get(state, "running"),
            "workflow_run_id": run_id,
            "workflow_state": state,
        }
