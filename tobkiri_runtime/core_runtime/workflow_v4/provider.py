"""Finite Workflow Pack v4 provider surface."""

from __future__ import annotations

from typing import Any, Mapping

from .engine import WorkflowEngineV4
from .models import DefinitionState, WorkflowValidationError, require_mapping

WORKFLOW_CONTRACT_ID = "tobkiri.workflow.v4"
WORKFLOW_FUNCTION_PRINCIPAL = "tobkiri.workflow.provider"
WORKFLOW_OPERATIONS = (
    "definition.archive",
    "definition.create",
    "definition.delete",
    "definition.get",
    "definition.list",
    "definition.publish",
    "definition.update",
    "definition.validate",
    "definition.compile-preview",
    "operation.palette",
    "run.cancel",
    "run.advance",
    "run.create",
    "run.get",
    "run.pause",
    "run.reconcile-recovery",
    "run.resume",
    "run.step.execute",
    "run.step.resume",
    "run.step.retry",
)


class WorkflowProviderV4:
    """Expose exact Contract operations without HTTP or registry fallbacks."""

    def __init__(self, engine: WorkflowEngineV4) -> None:
        self._engine = engine

    def invoke(self, operation_id: str, payload: Mapping[str, Any]) -> Any:
        """Invoke one finite Workflow v4 operation."""

        if operation_id not in WORKFLOW_OPERATIONS:
            raise WorkflowValidationError("unknown Workflow v4 operation")
        handlers = {
            "definition.archive": self._archive,
            "definition.create": self._create,
            "definition.delete": self._delete,
            "definition.get": self._get,
            "definition.list": self._list,
            "definition.publish": self._publish,
            "definition.update": self._update,
            "definition.validate": self._validate,
            "definition.compile-preview": self._compile_preview,
            "operation.palette": self._palette,
            "run.cancel": self._cancel,
            "run.advance": self._advance,
            "run.create": self._run_create,
            "run.get": self._run_get,
            "run.pause": self._pause,
            "run.reconcile-recovery": self._reconcile,
            "run.resume": self._resume,
            "run.step.execute": self._execute,
            "run.step.resume": self._execute,
            "run.step.retry": self._retry,
        }
        return handlers[operation_id](payload)

    def _create(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.store.create_definition(
            self._required_string(payload, "definition_id"),
            require_mapping(payload.get("document"), "document"),
        )

    def _update(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.store.update_definition(
            self._required_string(payload, "definition_id"),
            require_mapping(payload.get("document"), "document"),
            if_match=self._required_string(payload, "if_match"),
        )

    def _delete(self, payload: Mapping[str, Any]) -> Any:
        self._engine.store.delete_draft(
            self._required_string(payload, "definition_id"),
            if_match=self._required_string(payload, "if_match"),
        )
        return {"deleted": True}

    def _archive(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.store.transition_definition(
            self._required_string(payload, "definition_id"),
            if_match=self._required_string(payload, "if_match"),
            expected=DefinitionState.PUBLISHED,
            target=DefinitionState.ARCHIVED,
        )

    def _publish(self, payload: Mapping[str, Any]) -> Any:
        definition_id = self._required_string(payload, "definition_id")
        current = self._engine.store.get_definition(definition_id)
        compiled = self._engine.compile_preview(current["document"])
        return self._engine.store.transition_definition(
            definition_id,
            if_match=self._required_string(payload, "if_match"),
            expected=DefinitionState.DRAFT,
            target=DefinitionState.PUBLISHED,
            compiled=compiled,
        )

    def _get(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.store.get_definition(self._required_string(payload, "definition_id"))

    def _list(self, payload: Mapping[str, Any]) -> Any:
        if payload:
            raise WorkflowValidationError("definition.list does not accept input")
        return {"definitions": self._engine.store.list_definitions()}

    def _validate(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.validate_definition(
            require_mapping(payload.get("document"), "document")
        )

    def _compile_preview(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.compile_preview(require_mapping(payload.get("document"), "document"))

    def _palette(self, payload: Mapping[str, Any]) -> Any:
        if payload:
            raise WorkflowValidationError("operation.palette does not accept input")
        return self._engine.operation_palette()

    def _run_create(self, payload: Mapping[str, Any]) -> Any:
        inputs = require_mapping(payload.get("inputs", {}), "inputs")
        return self._engine.start_run(
            definition_id=self._required_string(payload, "definition_id"),
            inputs=inputs,
            occurrence_id=self._optional_string(payload, "occurrence_id"),
            run_id=self._optional_string(payload, "run_id"),
        )

    def _run_get(self, payload: Mapping[str, Any]) -> Any:
        run_id = self._required_string(payload, "run_id")
        return {
            "run": self._engine.store.get_run(run_id),
            "attempts": self._engine.store.list_attempts(run_id),
        }

    def _execute(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.execute_step(
            self._required_string(payload, "run_id"),
            self._required_string(payload, "step_id"),
        )

    def _retry(self, payload: Mapping[str, Any]) -> Any:
        run_id = self._required_string(payload, "run_id")
        step_id = self._required_string(payload, "step_id")
        attempts = [
            item for item in self._engine.store.list_attempts(run_id) if item["step_id"] == step_id
        ]
        if not attempts or attempts[-1]["state"] != "failed":
            raise WorkflowValidationError("only a failed StepAttempt can be retried")
        return self._engine.execute_step(run_id, step_id)

    def _pause(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.pause_run(self._required_string(payload, "run_id"))

    def _resume(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.resume_run(self._required_string(payload, "run_id"))

    def _cancel(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.cancel_run(self._required_string(payload, "run_id"))

    def _advance(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.advance_run(self._required_string(payload, "run_id"))

    def _reconcile(self, payload: Mapping[str, Any]) -> Any:
        return self._engine.reconcile_recovery(self._required_string(payload, "run_id"))

    def _required_string(self, payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise WorkflowValidationError(f"{key} is required")
        return value

    def _optional_string(self, payload: Mapping[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise WorkflowValidationError(f"{key} must be a non-empty string")
        return value
