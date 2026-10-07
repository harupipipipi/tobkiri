"""Selected immutable Workflow execution through the ordinary attempt engine."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json

from .engine import WorkflowEngineV4
from .models import DefinitionState, RunState, WorkflowConflict, WorkflowDenied, digest


def run_selected(engine: WorkflowEngineV4, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Publish a content-addressed source and drive its normally authorized steps.

    This is invoked behind the captured Workflow provider, not an authority
    factory. Waiting approval remains waiting; every step uses the existing
    reserve/commit/Broker port. Caller inputs convey data only.
    """
    from core_runtime.profile_workflow_catalog import selected_workflow_document

    if set(payload) != {"definition_id", "inputs", "occurrence_id"}:
        raise WorkflowDenied("selected Workflow request fields are invalid")
    identity, inputs, occurrence = (
        payload["definition_id"],
        payload["inputs"],
        payload["occurrence_id"],
    )
    if (
        not isinstance(identity, str)
        or not isinstance(inputs, Mapping)
        or not isinstance(occurrence, str)
        or not 1 <= len(occurrence) <= 256
    ):
        raise WorkflowDenied("selected Workflow request is invalid")
    if len(canonical_json(dict(inputs))) > 256 * 1024:
        raise WorkflowDenied("selected Workflow input exceeds its byte limit")
    document = selected_workflow_document(engine, identity)
    compiled = engine.compile_preview(document)
    definition_id = "selected-" + digest({"source": document, "compiled": compiled})[7:]
    try:
        definition = engine.store.create_definition(definition_id, document)
    except WorkflowConflict:
        definition = engine.store.get_definition(definition_id)
        if definition["document"] != document:
            raise WorkflowDenied("selected Workflow stored source differs")
    if definition["state"] == DefinitionState.DRAFT.value:
        definition = engine.store.transition_definition(
            definition_id,
            if_match=definition["etag"],
            expected=DefinitionState.DRAFT,
            target=DefinitionState.PUBLISHED,
            compiled=compiled,
        )
    if definition.get("compiled") != compiled:
        raise WorkflowDenied("selected Workflow stored compilation differs")
    run_id = (
        "selected-run-"
        + digest(
            {
                "definition_id": definition_id,
                "occurrence": occurrence,
            }
        )[7:]
    )
    try:
        run = engine.start_run(
            definition_id=definition_id,
            inputs=inputs,
            occurrence_id=run_id,
            run_id=run_id,
        )
    except WorkflowConflict:
        run = engine.store.get_run(run_id)
        if (
            run["definition_id"] != definition_id
            or run["inputs"] != dict(inputs)
            or run["catalog_digest"] != compiled["catalog_digest"]
            or run["security_epoch"] != compiled["security_epoch"]
        ):
            raise WorkflowDenied("selected Workflow occurrence is stale or altered")
    for _ in range(len(compiled["steps"]) + 1):
        if run["state"] not in {
            RunState.QUEUED.value,
            RunState.RUNNING.value,
            RunState.WAITING_APPROVAL.value,
        }:
            break
        previous = digest(run)
        run = engine.advance_run(run_id)["run"]
        if run["state"] == RunState.WAITING_APPROVAL.value or digest(run) == previous:
            break
    return {"run": run, "attempts": engine.store.list_attempts(run_id)}
