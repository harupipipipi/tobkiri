"""Authority-aware Workflow v4 compiler and state machine."""

from __future__ import annotations

import contextvars
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core_runtime.workflow_v4.attempt_binding import authority_query

from tobkiri_protocol.data_codec import CodecError
from .data_codec import compiled_digest, document_digest, outcome_digest as digest_outcome
from tobkiri_protocol.errors import CanonicalizationError

from .binding import (
    DISPLAY_PROJECTION,
    PORT_BINDING_FORMAT,
    contains_step_reference,
    contains_template,
    is_input_template,
    is_step_reference,
    parse_step_reference,
    project_display_schema,
    project_ports,
    resolve_templates,
    step_references,
)
from .models import (
    ApprovalState,
    DefinitionState,
    InvocationOutcome,
    OperationBinding,
    RunState,
    StepAttemptState,
    WorkflowCancellationUnconfirmed,
    WorkflowConflict,
    WorkflowDenied,
    WorkflowValidationError,
    digest,
    require_mapping,
)
from .protocols import (
    AuthorityProvider,
    ContractCatalogProvider,
    ContractInvocationProvider,
    InputValidator,
)
from .store import WorkflowStoreV4
from .run_ownership import assert_run_owner, validate_owner_scope_digest
from .value_binding import value_binding_errors

_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ALLOWED_WHEN = re.compile(
    r"^(?:true|false|inputs\.[a-z][a-z0-9_.-]*\s*(?:==|!=)\s*"
    r"(?:true|false|null|-?[0-9]+|'[^']{0,256}'))$"
)


class WorkflowEngineV4:
    """Compile definitions and drive exact attempt-scoped Contract Requests."""

    def __init__(
        self,
        *,
        store: WorkflowStoreV4,
        catalog: ContractCatalogProvider,
        authority: AuthorityProvider,
        invoker: ContractInvocationProvider,
        validator: InputValidator,
        clock: Callable[[], float] = time.time,
        owner_scope_digest: str | None = None,
    ) -> None:
        validate_owner_scope_digest(owner_scope_digest)
        self._owner_scope_digest = owner_scope_digest
        self.store = store
        self._catalog = catalog
        self._authority = authority
        self._invoker = invoker
        self._validator = validator
        self._clock = clock

    def _owned_run(self, run_id: str) -> dict[str, Any]:
        """Authenticate immutable run ownership before any lifecycle mutation."""
        run = self.store.get_run(run_id)
        assert_run_owner(run, self._owner_scope_digest)
        return run

    def operation_palette(self) -> dict[str, Any]:
        """Project the editor palette from the exact active Contract catalog.

        Captured input/output schema documents may be attached as a bounded
        ``display-reduced`` projection for the editor's typed-port precheck.
        The projection is display metadata only: it never joins the persisted
        catalog digest, never verifies against the schema digests, and the
        runtime resolved-input schema validation remains authoritative.
        """

        snapshot = self._catalog.snapshot()
        bindings = self._catalog_bindings(snapshot)
        schemas = snapshot.get("schemas")
        schemas = schemas if isinstance(schemas, Mapping) else {}
        output_digests: dict[tuple[str, str, str, str], str] = {}
        declared = snapshot.get("operation_output_schemas")
        if isinstance(declared, list):
            for entry in declared:
                if not isinstance(entry, Mapping):
                    continue
                key = (
                    str(entry.get("contract_id") or ""),
                    str(entry.get("contract_revision_digest") or ""),
                    str(entry.get("operation_id") or ""),
                    str(entry.get("function_principal_id") or ""),
                )
                digest_value = entry.get("output_schema_digest")
                if isinstance(digest_value, str):
                    output_digests[key] = digest_value
        operations = []
        for item in sorted(bindings.values(), key=lambda value: value.key):
            output_digest = output_digests.get(item.key)
            input_schema = project_display_schema(
                schemas.get(item.input_schema_digest)
            )
            output_schema = project_display_schema(schemas.get(output_digest))
            operations.append(
                {
                    "contract_id": item.contract_id,
                    "contract_revision_digest": item.contract_revision_digest,
                    "operation_id": item.operation_id,
                    "function_principal_id": item.function_principal_id,
                    "provider_id": item.provider_id,
                    "input_schema_digest": item.input_schema_digest,
                    "effect_ceiling": list(item.effect_ceiling),
                    "output_schema_digest": output_digest,
                    "projection": DISPLAY_PROJECTION,
                    "schemas": {
                        "input": input_schema,
                        "output": output_schema,
                    },
                    "input_ports": project_ports(
                        schemas.get(item.input_schema_digest)
                    ),
                    "output_ports": project_ports(schemas.get(output_digest)),
                }
            )
        return {
            "catalog_digest": str(snapshot["catalog_digest"]),
            "security_epoch": int(snapshot["security_epoch"]),
            "operations": operations,
            "port_binding": {
                "reference_format": PORT_BINDING_FORMAT,
                "whole_value_reference_format": "${steps.<step_id>.output}",
                "json_pointer_reference_format": "${steps.<step_id>.output@<json_pointer>}",
                "requires_depends_on": True,
            },
        }

    def validate_definition(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a Definition without reserving authority or performing I/O."""

        errors: list[str] = []
        try:
            document_digest(document)
        except (CanonicalizationError, CodecError):
            return {
                "valid": False,
                "errors": [
                    "workflow document must use bounded canonical JSON metadata "
                    "and JSON data inputs: finite numbers, safe integers, valid "
                    "Unicode, bounded size, nesting and node count"
                ],
            }
        if document.get("workflow_api_version") != "io.tobkiri.workflow.v4":
            errors.append("workflow_api_version must be io.tobkiri.workflow.v4")
        steps = document.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("steps must be a non-empty array")
            steps = []
        concurrency = document.get("max_concurrency", 1)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
            errors.append("max_concurrency must be between 1 and 32")
        try:
            bindings = self._catalog_bindings(self._catalog.snapshot())
        except (KeyError, TypeError, ValueError, WorkflowValidationError) as exc:
            raise WorkflowDenied("active Contract catalog is invalid") from exc
        ids: set[str] = set()
        dependencies: dict[str, list[str]] = {}
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, Mapping):
                errors.append(f"steps[{index}] must be an object")
                continue
            step_id = str(raw_step.get("id") or "")
            if not _ID.fullmatch(step_id) or step_id in ids:
                errors.append(f"steps[{index}].id is invalid or duplicated")
                continue
            ids.add(step_id)
            label = raw_step.get("label")
            if label is not None and (
                not isinstance(label, str) or not 1 <= len(label) <= 128
            ):
                errors.append(f"steps[{index}].label must be 1-128 characters")
                continue
            request = raw_step.get("request")
            if not isinstance(request, Mapping):
                errors.append(f"steps[{index}].request must be an object")
                continue
            key = (
                str(request.get("contract_id") or ""),
                str(request.get("contract_revision_digest") or ""),
                str(request.get("operation_id") or ""),
                str(request.get("function_principal_id") or ""),
            )
            binding = bindings.get(key)
            if binding is None:
                errors.append(f"steps[{index}] is not an exact active catalog operation")
            else:
                input_value = request.get("input", {})
                if not isinstance(input_value, Mapping) and not (
                    is_input_template(input_value) or is_step_reference(input_value)
                ):
                    errors.append(f"steps[{index}].request.input must be an object or whole-value binding")
                elif not contains_template(input_value):
                    errors.extend(
                        f"steps[{index}].request.input: {error}"
                        for error in self._validator.validate(
                            binding.input_schema_digest, input_value
                        )
                    )
            retry = raw_step.get("retry", {})
            if not isinstance(retry, Mapping):
                errors.append(f"steps[{index}].retry must be an object")
            else:
                max_attempts = retry.get("max_attempts", 1)
                backoff_ms = retry.get("backoff_ms", 0)
                if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
                    errors.append(f"steps[{index}].retry.max_attempts is invalid")
                if not isinstance(backoff_ms, int) or not 0 <= backoff_ms <= 86_400_000:
                    errors.append(f"steps[{index}].retry.backoff_ms is invalid")
            when = raw_step.get("when")
            if when is not None and (
                not isinstance(when, str) or not _ALLOWED_WHEN.fullmatch(when)
            ):
                errors.append(f"steps[{index}].when is outside the restricted CEL subset")
            depends_on = raw_step.get("depends_on", [])
            if not isinstance(depends_on, list) or not all(
                isinstance(item, str) for item in depends_on
            ):
                errors.append(f"steps[{index}].depends_on must be a string array")
            else:
                dependencies[step_id] = list(depends_on)
        for step_id, parents in dependencies.items():
            unknown = set(parents) - ids
            if unknown or step_id in parents:
                errors.append(f"step {step_id} has invalid dependencies")
        if not errors and self._has_cycle(dependencies):
            errors.append("workflow step dependencies contain a cycle")
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, Mapping):
                continue
            request = raw_step.get("request")
            if not isinstance(request, Mapping):
                continue
            input_value = request.get("input", {})
            if not isinstance(input_value, Mapping) and not (
                is_input_template(input_value) or is_step_reference(input_value)
            ):
                continue
            declared = dependencies.get(str(raw_step.get("id") or ""), [])
            for text in step_references(input_value):
                try:
                    ref_step, _path = parse_step_reference(text, ids)
                except WorkflowValidationError as exc:
                    errors.append(f"steps[{index}].request.input: {exc}")
                    continue
                if ref_step not in declared:
                    errors.append(
                        f"steps[{index}] references {ref_step} output without "
                        "declaring it in depends_on"
                    )
        if not errors:
            errors.extend(value_binding_errors(steps, self._catalog.snapshot()))
        return {"valid": not errors, "errors": errors}

    def compile_preview(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Compile a deterministic, authority-free preview pinned to the catalog."""

        validation = self.validate_definition(document)
        if not validation["valid"]:
            raise WorkflowValidationError("; ".join(validation["errors"]))
        snapshot = self._catalog.snapshot()
        compiled_steps = []
        for raw in document["steps"]:
            request = raw["request"]
            compiled_steps.append(
                {
                    "step_id": raw["id"],
                    "depends_on": sorted(raw.get("depends_on", [])),
                    "when": raw.get("when", "true"),
                    "contract_request": {
                        "contract_id": request["contract_id"],
                        "contract_revision_digest": request["contract_revision_digest"],
                        "operation_id": request["operation_id"],
                        "function_principal_id": request["function_principal_id"],
                        "input": request.get("input", {}),
                    },
                    "retry": {
                        "max_attempts": int(raw.get("retry", {}).get("max_attempts", 1)),
                        "backoff_ms": int(raw.get("retry", {}).get("backoff_ms", 0)),
                    },
                    "timeout_ms": int(raw.get("timeout_ms", 30_000)),
                }
            )
        compiled = {
            "workflow_compile_api_version": "io.tobkiri.workflow-compile.v4",
            "catalog_digest": str(snapshot["catalog_digest"]),
            "security_epoch": int(snapshot["security_epoch"]),
            "max_concurrency": int(document.get("max_concurrency", 1)),
            "steps": compiled_steps,
        }
        return {**compiled, "compile_digest": compiled_digest(compiled)}

    def start_run(
        self,
        *,
        definition_id: str,
        inputs: Mapping[str, Any],
        occurrence_id: str | None = None,
        run_id: str | None = None,
        revision_digest: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued Run pinned to Definition, activation, and catalog."""

        definition = self.store.get_definition(definition_id)
        if revision_digest is not None and (
            not isinstance(revision_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", revision_digest) is None
            or definition["revision_digest"] != revision_digest
        ):
            raise WorkflowConflict("requested Workflow revision does not match the published definition")
        if definition["state"] != DefinitionState.PUBLISHED.value:
            raise WorkflowConflict("workflow definition is not published")
        compiled = require_mapping(definition.get("compiled"), "compiled definition")
        snapshot = self._catalog.snapshot()
        if snapshot.get("catalog_digest") != compiled.get("catalog_digest"):
            raise WorkflowDenied("published workflow Contract catalog is stale")
        activation = require_mapping(snapshot.get("activation"), "catalog activation")
        activation_record = {
            "activation_id": str(activation.get("activation_id") or ""),
            "activation_digest": str(activation.get("activation_digest") or ""),
            "catalog_digest": str(snapshot.get("catalog_digest") or ""),
            "security_epoch": int(snapshot["security_epoch"]),
        }
        if not all(activation_record.values()):
            raise WorkflowDenied("active Contract catalog activation is incomplete")
        return self.store.create_run(
            run_id=run_id or "workflow-run-" + secrets.token_hex(12),
            definition=definition,
            activation=activation_record,
            inputs=inputs,
            occurrence_id=occurrence_id,
            owner_scope_digest=self._owner_scope_digest,
        )

    def execute_step(self, run_id: str, step_id: str) -> dict[str, Any]:
        """Execute or resume one StepAttempt through reserve/commit authority."""

        run = self._owned_run(run_id)
        if RunState(run["state"]) in {RunState.QUEUED, RunState.PAUSED}:
            run = self.store.transition_run(
                run_id,
                expected={RunState.QUEUED, RunState.PAUSED},
                target=RunState.RUNNING,
            )
        if RunState(run["state"]) not in {
            RunState.RUNNING,
            RunState.WAITING_APPROVAL,
        }:
            raise WorkflowConflict("workflow run cannot execute a step")
        definition = self.store.get_revision(run["revision_digest"])
        compiled = require_mapping(definition.get("compiled"), "compiled definition")
        step = next((item for item in compiled["steps"] if item["step_id"] == step_id), None)
        if step is None:
            raise WorkflowValidationError("workflow step is unavailable")
        self._validate_run_snapshot(run, compiled)
        run_attempts = self.store.list_attempts(run_id)
        succeeded_steps = {
            item["step_id"]
            for item in run_attempts
            if item["state"] == StepAttemptState.SUCCEEDED.value
        }
        if not set(step["depends_on"]).issubset(succeeded_steps):
            raise WorkflowConflict("workflow step dependencies are not satisfied")
        attempts = [item for item in run_attempts if item["step_id"] == step_id]
        if attempts and attempts[-1]["state"] == StepAttemptState.WAITING_APPROVAL.value:
            return self._resume_waiting(run, step, attempts[-1])
        if attempts and attempts[-1]["state"] != StepAttemptState.FAILED.value:
            raise WorkflowConflict("only a failed workflow step can start another attempt")
        if attempts and attempts[-1]["state"] == StepAttemptState.FAILED.value:
            retry_not_before_ms = int(attempts[-1].get("retry_not_before_ms", 0))
            if int(self._clock() * 1000) < retry_not_before_ms:
                raise WorkflowConflict("workflow step retry backoff has not elapsed")
        attempt_number = len(attempts) + 1
        if attempt_number > int(step["retry"]["max_attempts"]):
            raise WorkflowConflict("workflow step retry limit is exhausted")
        try:
            request, output_bindings = self._materialize_request(
                run,
                step,
                attempt_number,
                step_ids={item["step_id"] for item in compiled["steps"]},
                run_attempts=run_attempts,
            )
        except WorkflowValidationError:
            # This immutable run's resolved input cannot satisfy its Contract.
            # Do not leave the UI showing a running step with no runnable attempt.
            self._run_transition_preserving_cancel(
                run_id, expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                target=RunState.FAILED,
            )
            raise
        attempt = self.store.create_attempt(
            run_id=run_id,
            step_id=step_id,
            attempt_number=attempt_number,
            request=request,
            output_bindings=output_bindings,
            owner_scope_digest=self._owner_scope_digest,
        )
        if not self._evaluate_when(str(step["when"]), run["inputs"]):
            attempt = self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.PENDING},
                target=StepAttemptState.SUCCEEDED,
                updates={"skipped": True, "condition": step["when"]},
            )
            if self._all_steps_succeeded(run_id, step_count=None):
                self._run_transition_preserving_cancel(
                    run_id,
                    expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                    target=RunState.SUCCEEDED,
                    keep_states={RunState.SUCCEEDED},
                )
            return attempt
        try:
            reservation = self._authority.reserve(self._authority_request(run, attempt))
            self._validate_reservation(run, attempt, reservation)
        except Exception:
            current = self.store.get_attempt(attempt["attempt_id"])
            if current["state"] == StepAttemptState.PENDING.value:
                self.store.transition_attempt(
                    attempt["attempt_id"], expected={StepAttemptState.PENDING},
                    target=StepAttemptState.FAILED,
                    updates={"error_code": "authority_reservation_failed"},
                )
                self._run_transition_preserving_cancel(
                    run_id, expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                    target=RunState.FAILED,
                )
            raise
        if reservation.state is ApprovalState.WAITING_APPROVAL:
            self.store.checkpoint(
                attempt["attempt_id"],
                self._checkpoint_payload(run, attempt, reservation.reservation_id),
            )
            attempt = self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.PENDING},
                target=StepAttemptState.WAITING_APPROVAL,
                updates={
                    "authority_reservation_id": reservation.reservation_id,
                    "approval_request_id": reservation.approval_request_id,
                },
            )
            self._run_transition_preserving_cancel(
                run_id,
                expected={RunState.RUNNING},
                target=RunState.WAITING_APPROVAL,
                keep_states={RunState.WAITING_APPROVAL},
            )
            return attempt
        if reservation.state not in {ApprovalState.RESERVED, ApprovalState.APPROVED}:
            return self._fail_approval(run, attempt, reservation.state)
        return self._dispatch(run, step, attempt, reservation.reservation_id)

    def advance_run(self, run_id: str) -> dict[str, Any]:
        """Execute dependency-ready steps with bounded real concurrency."""

        run = self._owned_run(run_id)
        if RunState(run["state"]) is RunState.QUEUED:
            run = self.store.transition_run(
                run_id, expected={RunState.QUEUED}, target=RunState.RUNNING
            )
        if RunState(run["state"]) not in {
            RunState.RUNNING,
            RunState.WAITING_APPROVAL,
        }:
            raise WorkflowConflict("workflow run cannot advance")
        definition = self.store.get_revision(run["revision_digest"])
        compiled = require_mapping(definition.get("compiled"), "compiled definition")
        self._validate_run_snapshot(run, compiled)
        attempts = self.store.list_attempts(run_id)
        if RunState(run["state"]) is RunState.WAITING_APPROVAL:
            waiting = next(
                (
                    item
                    for item in attempts
                    if item["state"] == StepAttemptState.WAITING_APPROVAL.value
                ),
                None,
            )
            if waiting is None:
                raise WorkflowConflict("workflow approval checkpoint is unavailable")
            result = self.execute_step(run_id, waiting["step_id"])
            return {"run": self._owned_run(run_id), "attempts": [result]}
        succeeded = {
            item["step_id"]
            for item in attempts
            if item["state"] == StepAttemptState.SUCCEEDED.value
        }
        latest = {item["step_id"]: item for item in attempts}
        ready: list[str] = []
        for step in compiled["steps"]:
            step_id = step["step_id"]
            previous = latest.get(step_id)
            if step_id in succeeded or not set(step["depends_on"]).issubset(succeeded):
                continue
            if previous and previous["state"] not in {
                StepAttemptState.FAILED.value,
                StepAttemptState.TIMED_OUT.value,
            }:
                continue
            if previous and int(previous["attempt_number"]) >= int(step["retry"]["max_attempts"]):
                continue
            ready.append(step_id)
        if not ready:
            return {"run": self._owned_run(run_id), "attempts": []}
        concurrency = min(int(compiled["max_concurrency"]), len(ready))
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="workflow-v4"
        ) as executor:
            futures = [
                executor.submit(contextvars.copy_context().run, self.execute_step, run_id, item)
                for item in ready
            ]
            results = [future.result() for future in futures]
        return {"run": self._owned_run(run_id), "attempts": results}

    def pause_run(self, run_id: str) -> dict[str, Any]:
        """Pause a running Run between effects."""

        self._owned_run(run_id)
        if any(
            item["state"] in {StepAttemptState.DISPATCHING.value, StepAttemptState.RUNNING.value}
            for item in self.store.list_attempts(run_id)
        ):
            raise WorkflowConflict("workflow Run has an in-flight effect")
        return self.store.transition_run(
            run_id, expected={RunState.RUNNING}, target=RunState.PAUSED
        )

    def resume_run(self, run_id: str) -> dict[str, Any]:
        """Resume a paused Run; authority is re-evaluated per next attempt."""

        self._owned_run(run_id)
        return self.store.transition_run(
            run_id, expected={RunState.PAUSED}, target=RunState.RUNNING
        )

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Fence reservations and propagate cancel to in-flight requests.

        The durable stop intent is committed first: ``create_attempt``
        refuses every new attempt after this point, so the drain list read
        below cannot miss an attempt admitted after Stop began.
        """

        self._owned_run(run_id)
        self.store.request_cancel(
            run_id, owner_scope_digest=self._owner_scope_digest,
        )
        unconfirmed: WorkflowCancellationUnconfirmed | None = None
        for attempt in self.store.list_attempts(run_id):
            state = StepAttemptState(attempt["state"])
            if state in {
                StepAttemptState.PENDING,
                StepAttemptState.DISPATCHING,
                StepAttemptState.RUNNING,
                StepAttemptState.WAITING_APPROVAL,
            }:
                reservation_id = attempt.get("authority_reservation_id")
                if reservation_id:
                    try:
                        self._authority.revoke(
                            reservation_id, reason="workflow_cancelled"
                        )
                    except Exception:
                        # Fencing is best-effort; the durable attempt
                        # transition below is the authoritative record.
                        pass
                try:
                    self._request_attempt_stop(
                        attempt["attempt_id"], str(attempt["request"]["request_id"]),
                    )
                    self._cancel_attempt_preserving_truth(
                        attempt["attempt_id"], state,
                        str(attempt["request"]["request_id"]),
                    )
                except WorkflowCancellationUnconfirmed as error:
                    # Still signal other tracked attempts. Preserve this
                    # attempt's uncertainty for its executor or recovery.
                    unconfirmed = error
        if any(item["state"] == StepAttemptState.AMBIGUOUS_EFFECT.value
               for item in self.store.list_attempts(run_id)):
            return self._run_transition_preserving_cancel(
                run_id, expected={RunState.RUNNING, RunState.WAITING_APPROVAL, RunState.PAUSED},
                target=RunState.NEEDS_RECONCILIATION,
            )
        if unconfirmed is not None:
            raise unconfirmed
        try:
            return self.store.transition_run(
                run_id,
                expected={
                    RunState.QUEUED,
                    RunState.RUNNING,
                    RunState.PAUSED,
                    RunState.WAITING_APPROVAL,
                },
                target=RunState.CANCELLED,
            )
        except WorkflowConflict:
            current = self._owned_run(run_id)
            if RunState(current["state"]) in {
                RunState.CANCELLED,
                RunState.SUCCEEDED,
                RunState.FAILED,
                RunState.TIMED_OUT,
                RunState.NEEDS_RECONCILIATION,
            }:
                # The run committed a coherent state during the drain; the
                # cancel arrived too late to rewrite it, and that truth
                # stands instead of a conflict.
                return current
            raise

    def _request_attempt_stop(self, attempt_id: str, request_id: str) -> None:
        """Require live drain or sealed proof that no dispatch was admitted."""
        try:
            self._invoker.cancel(request_id)
        except WorkflowCancellationUnconfirmed:
            current = self.store.get_attempt(attempt_id)
            run = self._owned_run(str(current["run_id"]))
            if current["request"].get("request_id") != request_id:
                raise WorkflowDenied("cancellation request identity changed")
            state = StepAttemptState(current["state"])
            if state in {
                StepAttemptState.SUCCEEDED, StepAttemptState.FAILED,
                StepAttemptState.CANCELLED, StepAttemptState.TIMED_OUT,
                StepAttemptState.AMBIGUOUS_EFFECT,
            }:
                return
            if run.get("cancel_requested") and (
                state in {StepAttemptState.PENDING, StepAttemptState.WAITING_APPROVAL}
                or current.get("dispatch_admission") == "not_admitted"
            ):
                return
            raise

    def _cancel_attempt_preserving_truth(
        self,
        attempt_id: str,
        expected_state: StepAttemptState,
        request_id: str,
    ) -> None:
        """Record the cancel, keeping a concurrently committed outcome.

        A stale ``active_for``/read is never proof of drain: when the
        durable transition conflicts because execute committed a terminal
        outcome first, that truth stands and the run still cancels.  A
        still in-flight state is signalled once more and retried exactly
        once before failing loud.
        """

        try:
            self.store.transition_attempt(
                attempt_id,
                expected={expected_state},
                target=StepAttemptState.CANCELLED,
            )
            return
        except WorkflowConflict:
            pass
        current = self.store.get_attempt(attempt_id)
        current_state = StepAttemptState(current["state"])
        if current_state is StepAttemptState.CANCELLED or current_state in {
            StepAttemptState.SUCCEEDED,
            StepAttemptState.FAILED,
            StepAttemptState.TIMED_OUT,
            StepAttemptState.AMBIGUOUS_EFFECT,
        }:
            # Already cancelled, or the committed outcome is the truth.
            return
        self._request_attempt_stop(attempt_id, request_id)
        self.store.transition_attempt(
            attempt_id,
            expected={current_state},
            target=StepAttemptState.CANCELLED,
        )

    def reconcile_recovery(self, run_id: str) -> dict[str, Any]:
        """Mark crash-surviving in-flight effects ambiguous, never auto-retry."""

        self._owned_run(run_id)
        changed = False
        interrupted = False
        for attempt in self.store.list_attempts(run_id):
            state = StepAttemptState(attempt["state"])
            if state is StepAttemptState.PENDING:
                self.store.transition_attempt(
                    attempt["attempt_id"], expected={state},
                    target=StepAttemptState.FAILED,
                    updates={"error_code": "interrupted_before_dispatch"},
                )
                interrupted = True
            if state in {StepAttemptState.DISPATCHING, StepAttemptState.RUNNING}:
                self.store.transition_attempt(
                    attempt["attempt_id"],
                    expected={state},
                    target=StepAttemptState.AMBIGUOUS_EFFECT,
                )
                changed = True
        if not changed:
            if interrupted:
                return self._run_transition_preserving_cancel(
                    run_id, expected={RunState.RUNNING, RunState.WAITING_APPROVAL, RunState.PAUSED},
                    target=RunState.FAILED,
                )
            return self._owned_run(run_id)
        return self._run_transition_preserving_cancel(
            run_id,
            expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
            target=RunState.NEEDS_RECONCILIATION,
        )

    def _resume_waiting(
        self, run: Mapping[str, Any], step: Mapping[str, Any], attempt: Mapping[str, Any]
    ) -> dict[str, Any]:
        reservation_id = str(attempt.get("authority_reservation_id") or "")
        if not reservation_id:
            raise WorkflowDenied("approval checkpoint lost its reservation fence")
        reservation = self._authority.inspect(reservation_id)
        self._validate_reservation(run, attempt, reservation)
        if reservation.state is ApprovalState.WAITING_APPROVAL:
            return dict(attempt)
        if reservation.state in {ApprovalState.DENIED, ApprovalState.REVOKED}:
            return self._fail_approval(run, attempt, reservation.state)
        if reservation.state is ApprovalState.EXPIRED:
            self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.WAITING_APPROVAL},
                target=StepAttemptState.TIMED_OUT,
            )
            return self._run_transition_preserving_cancel(
                run["run_id"],
                expected={RunState.WAITING_APPROVAL, RunState.RUNNING},
                target=RunState.TIMED_OUT,
            )
        # The run only returns to RUNNING when no other attempt still
        # waits for approval; otherwise the UI must keep the approval
        # checkpoint for the remaining waiters instead of ending up
        # RUNNING with nothing ready.
        other_waiting = any(
            item["attempt_id"] != attempt["attempt_id"]
            and item["state"] == StepAttemptState.WAITING_APPROVAL.value
            for item in self.store.list_attempts(run["run_id"])
        )
        if not other_waiting:
            self._run_transition_preserving_cancel(
                run["run_id"],
                expected={RunState.WAITING_APPROVAL},
                target=RunState.RUNNING,
                keep_states={RunState.RUNNING},
            )
        attempt = self.store.transition_attempt(
            attempt["attempt_id"],
            expected={StepAttemptState.WAITING_APPROVAL},
            target=StepAttemptState.PENDING,
        )
        return self._dispatch(run, step, attempt, reservation_id)

    def _dispatch(
        self,
        run: Mapping[str, Any],
        step: Mapping[str, Any],
        attempt: Mapping[str, Any],
        reservation_id: str,
    ) -> dict[str, Any]:
        self.store.checkpoint(
            attempt["attempt_id"],
            self._checkpoint_payload(run, attempt, reservation_id),
        )
        try:
            authority = self._authority.commit(
                reservation_id,
                request_digest=attempt["request_digest"],
                security_epoch=int(run["security_epoch"]),
            )
            if (
                authority.reservation_id != reservation_id
                or authority.request_digest != attempt["request_digest"]
                or authority.security_epoch != int(run["security_epoch"])
                or not authority.dispatch_token
            ):
                raise WorkflowDenied("committed authority is stale or altered")
        except Exception as exc:
            self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.PENDING},
                target=StepAttemptState.FAILED,
                updates={"error_code": "authority_commit_denied"},
            )
            self._run_transition_preserving_cancel(
                run["run_id"],
                expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                target=RunState.FAILED,
            )
            if isinstance(exc, WorkflowDenied):
                raise
            raise WorkflowDenied("authority commit failed closed") from exc
        attempt = self.store.transition_attempt(
            attempt["attempt_id"],
            expected={StepAttemptState.PENDING},
            target=StepAttemptState.DISPATCHING,
            updates={"authority_reservation_id": reservation_id},
        )
        attempt = self.store.transition_attempt(
            attempt["attempt_id"],
            expected={StepAttemptState.DISPATCHING},
            target=StepAttemptState.RUNNING,
        )
        try:
            outcome = self._invoker.invoke(
                attempt["request"],
                authority=authority,
                dispatch_fence=self._attempt_dispatch_fence(attempt["attempt_id"]),
            )
        except Exception:
            outcome = InvocationOutcome(error_code="provider_interrupted", ambiguous_effect=True)
        try:
            outcome_digest = digest_outcome(
                {
                    "output": outcome.output,
                    "error_code": outcome.error_code,
                    "ambiguous_effect": outcome.ambiguous_effect,
                    "timed_out": outcome.timed_out,
                }
            )
        except Exception:
            outcome = InvocationOutcome(
                error_code="invalid_provider_outcome", ambiguous_effect=True
            )
            outcome_digest = digest({"error_code": outcome.error_code, "ambiguous_effect": True})
        current = self.store.get_attempt(attempt["attempt_id"])
        if StepAttemptState(current["state"]) is StepAttemptState.CANCELLED:
            return self._record_cancelled_dispatch_outcome(
                run, attempt, outcome, outcome_digest, reservation_id
            )
        if outcome.ambiguous_effect:
            try:
                self._authority.finish(
                    reservation_id,
                    outcome_digest=outcome_digest,
                    state="ambiguous_effect",
                )
            except Exception:
                pass
            committed = self._attempt_transition_preserving_cancel(
                attempt["attempt_id"],
                target=StepAttemptState.AMBIGUOUS_EFFECT,
                updates={"outcome_digest": outcome_digest},
            )
            if committed is None:
                return self._record_cancelled_dispatch_outcome(
                    run, attempt, outcome, outcome_digest, reservation_id
                )
            return self._run_transition_preserving_cancel(
                run["run_id"],
                expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                target=RunState.NEEDS_RECONCILIATION,
            )
        if outcome.timed_out:
            target = StepAttemptState.TIMED_OUT
            run_target = RunState.TIMED_OUT
            finish_state = "timed_out"
        elif outcome.error_code:
            target = StepAttemptState.FAILED
            run_target = RunState.FAILED
            finish_state = "failed"
        else:
            target = StepAttemptState.SUCCEEDED
            run_target = RunState.RUNNING
            finish_state = "succeeded"
        try:
            self._authority.finish(
                reservation_id, outcome_digest=outcome_digest, state=finish_state
            )
        except Exception as exc:
            committed = self._attempt_transition_preserving_cancel(
                attempt["attempt_id"],
                target=StepAttemptState.AMBIGUOUS_EFFECT,
                updates={"outcome_digest": outcome_digest},
            )
            if committed is None:
                return self._record_cancelled_dispatch_outcome(
                    run, attempt, outcome, outcome_digest, reservation_id
                )
            self._run_transition_preserving_cancel(
                run["run_id"],
                expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                target=RunState.NEEDS_RECONCILIATION,
            )
            raise WorkflowDenied("authority outcome commit failed closed") from exc
        result = self._attempt_transition_preserving_cancel(
            attempt["attempt_id"],
            target=target,
            updates={
                "outcome": dict(outcome.output or {}),
                "error_code": outcome.error_code,
                "outcome_digest": outcome_digest,
                "retry_not_before_ms": (
                    int(self._clock() * 1000) + int(step["retry"]["backoff_ms"])
                    if target is StepAttemptState.FAILED
                    else 0
                ),
            },
        )
        if result is None:
            return self._record_cancelled_dispatch_outcome(
                run, attempt, outcome, outcome_digest, reservation_id
            )
        if target is StepAttemptState.SUCCEEDED:
            if self._all_steps_succeeded(run["run_id"], step_count=None):
                self._run_transition_preserving_cancel(
                    run["run_id"],
                    expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
                    target=RunState.SUCCEEDED,
                    keep_states={RunState.SUCCEEDED},
                )
            return result
        if target is StepAttemptState.FAILED and int(attempt["attempt_number"]) < int(
            step["retry"]["max_attempts"]
        ):
            return result
        self._run_transition_preserving_cancel(
            run["run_id"],
            expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
            target=run_target,
        )
        return result

    def _attempt_transition_preserving_cancel(
        self,
        attempt_id: str,
        *,
        target: StepAttemptState,
        updates: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Commit the terminal attempt transition, or defer to the cancel.

        ``None`` means a concurrent ``cancel_run`` committed CANCELLED in
        the window between the post-invoke read and this transition; the
        caller must then take ``_record_cancelled_dispatch_outcome`` so the
        outcome is preserved as evidence instead of lost to a conflict.
        """

        try:
            return self.store.transition_attempt(
                attempt_id,
                expected={StepAttemptState.RUNNING},
                target=target,
                updates=dict(updates),
            )
        except WorkflowConflict:
            current = self.store.get_attempt(attempt_id)
            if StepAttemptState(current["state"]) is StepAttemptState.CANCELLED:
                return None
            raise

    def _run_transition_preserving_cancel(
        self,
        run_id: str,
        *,
        expected: frozenset[RunState] | set[RunState],
        target: RunState,
        keep_states: set[RunState] | None = None,
    ) -> dict[str, Any]:
        """Transition the run, keeping a concurrently committed state.

        A committed cancel (or a reconciliation another finalizer already
        wrote) is itself a truthful record — an attempt outcome committed
        earlier stays as evidence, so losing this race keeps that state
        instead of surfacing a conflict.
        """

        keep = {RunState.CANCELLED, RunState.NEEDS_RECONCILIATION} | (
            keep_states or set()
        )
        try:
            return self.store.transition_run(
                run_id, expected=set(expected), target=target
            )
        except WorkflowConflict:
            current = self._owned_run(run_id)
            if (RunState(current["state"]) is RunState.CANCELLED
                    and target is RunState.NEEDS_RECONCILIATION):
                try:
                    return self.store.transition_run(
                        run_id, expected={RunState.CANCELLED}, target=target,
                    )
                except WorkflowConflict:
                    observed = self._owned_run(run_id)
                    if RunState(observed["state"]) is RunState.NEEDS_RECONCILIATION:
                        return observed
                    raise
            if RunState(current["state"]) in keep:
                return current
            raise

    def _attempt_dispatch_fence(self, attempt_id: str) -> Callable[[str], None]:
        """Build the durable cancellation fence for one dispatching attempt.

        The fence re-reads the committed attempt row after the invoker has
        registered its tracked child but before Broker dispatch.  Either
        stop then finds the tracked child, or this durable guard prevents
        dispatch — ``active_for == False`` is never treated as proof that
        an in-flight attempt was drained.
        """

        def fence(request_id: str) -> None:
            self.store.admit_dispatch(attempt_id, request_id)

        return fence

    def _record_cancelled_dispatch_outcome(
        self,
        run: Mapping[str, Any],
        attempt: Mapping[str, Any],
        outcome: InvocationOutcome,
        outcome_digest: str,
        reservation_id: str,
    ) -> dict[str, Any]:
        """Reconcile a dispatch that lost the durable race to cancellation.

        A pre-dispatch fence outcome records no effect and the committed
        cancel stays truthful.  A dispatched outcome may have committed a
        real effect before or while stop drained the tracked child, so the
        honest record keeps the cancel, attaches the outcome evidence, and
        escalates the run to reconciliation instead of pretending nothing
        happened.
        """

        if not outcome.dispatched:
            self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.CANCELLED},
                target=StepAttemptState.CANCELLED,
                updates={
                    "error_code": outcome.error_code,
                    "outcome_digest": outcome_digest,
                },
            )
            return self._owned_run(run["run_id"])
        self.store.transition_attempt(
            attempt["attempt_id"],
            expected={StepAttemptState.CANCELLED},
            target=StepAttemptState.CANCELLED,
            updates={
                "outcome": dict(outcome.output or {}),
                "error_code": outcome.error_code or "cancelled_with_effect",
                "outcome_digest": outcome_digest,
            },
        )
        try:
            self._authority.finish(
                reservation_id,
                outcome_digest=outcome_digest,
                state="ambiguous_effect",
            )
        except Exception:
            pass
        # Several cancelled in-flight outcomes can race to reconcile; the
        # first keeps reconciliation and later writers preserve it rather
        # than conflict.
        return self._run_transition_preserving_cancel(
            run["run_id"],
            expected={
                RunState.CANCELLED,
                RunState.RUNNING,
                RunState.WAITING_APPROVAL,
            },
            target=RunState.NEEDS_RECONCILIATION,
        )

    def _fail_approval(
        self,
        run: Mapping[str, Any],
        attempt: Mapping[str, Any],
        state: ApprovalState,
    ) -> dict[str, Any]:
        current = StepAttemptState(attempt["state"])
        result = self.store.transition_attempt(
            attempt["attempt_id"],
            expected={current},
            target=StepAttemptState.FAILED,
            updates={"error_code": f"approval_{state.value}"},
        )
        self._run_transition_preserving_cancel(
            run["run_id"],
            expected={RunState.RUNNING, RunState.WAITING_APPROVAL},
            target=RunState.FAILED,
        )
        return result

    def _validate_run_snapshot(self, run: Mapping[str, Any], compiled: Mapping[str, Any]) -> None:
        snapshot = self._catalog.snapshot()
        if (
            snapshot.get("catalog_digest") != run["catalog_digest"]
            or int(snapshot.get("security_epoch", -1)) != int(run["security_epoch"])
            or compiled.get("catalog_digest") != run["catalog_digest"]
        ):
            raise WorkflowDenied("workflow Run snapshot is stale")

    def _validate_reservation(
        self, run: Mapping[str, Any], attempt: Mapping[str, Any], reservation: Any
    ) -> None:
        if (
            reservation.request_digest != attempt["request_digest"]
            or reservation.security_epoch != int(run["security_epoch"])
            or (
                reservation.state is not ApprovalState.EXPIRED
                and reservation.expires_at <= self._clock()
            )
            or not reservation.reservation_id
        ):
            raise WorkflowDenied("authority reservation is stale or altered")

    def _authority_request(
        self, run: Mapping[str, Any], attempt: Mapping[str, Any]
    ) -> dict[str, Any]:
        return authority_query(run, attempt)

    def _materialize_request(
        self,
        run: Mapping[str, Any],
        step: Mapping[str, Any],
        attempt_number: int,
        *,
        step_ids: set[str],
        run_attempts: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
        bindings = self._catalog_bindings(self._catalog.snapshot())
        source = step["contract_request"]
        key = (
            source["contract_id"],
            source["contract_revision_digest"],
            source["operation_id"],
            source["function_principal_id"],
        )
        binding = bindings.get(key)
        if binding is None:
            raise WorkflowDenied("compiled Contract operation is no longer active")
        source_input = source.get("input", {})
        provenance: list[Mapping[str, Any]] = []
        if contains_step_reference(source_input):
            prior = [
                item for item in run_attempts if item["step_id"] == step["step_id"]
            ]
            first_request = prior[0].get("request") if prior else None
            pinned = (
                first_request.get("input")
                if isinstance(first_request, Mapping)
                else None
            )
            if isinstance(pinned, Mapping):
                # A resolved request is immutable across retries: reuse the
                # first attempt's pinned input and recorded bindings verbatim.
                input_value = dict(pinned)
                provenance = list(prior[0].get("output_bindings") or [])
            else:
                input_value = self._resolve_bound_input(
                    run, source_input, step_ids, run_attempts, provenance
                )
        else:
            input_value = self._resolve_bound_input(
                run, source_input, step_ids, run_attempts, provenance
            )
        if not isinstance(input_value, Mapping):
            raise WorkflowValidationError("resolved Contract input must be an object")
        errors = self._validator.validate(binding.input_schema_digest, input_value)
        if errors:
            raise WorkflowValidationError("; ".join(errors))
        request_id = digest(
            {
                "run_id": run["run_id"],
                "step_id": step["step_id"],
                "attempt_number": attempt_number,
            }
        )
        return {
            "request_api_version": "io.tobkiri.contract-request.v4",
            "request_id": request_id,
            "contract_id": binding.contract_id,
            "contract_revision_digest": binding.contract_revision_digest,
            "operation_id": binding.operation_id,
            "function_principal_id": binding.function_principal_id,
            "provider_id": binding.provider_id,
            "input_schema_digest": binding.input_schema_digest,
            "input": input_value,
            "effect_ceiling": list(binding.effect_ceiling),
            "timeout_ms": int(step["timeout_ms"]),
            "idempotency_key": digest(
                {
                    "run_id": run["run_id"],
                    "revision_digest": run["revision_digest"],
                    "step_id": step["step_id"],
                }
            ),
            "call_chain": [run["definition_id"], step["step_id"], request_id],
        }, provenance

    def _resolve_bound_input(
        self,
        run: Mapping[str, Any],
        source_input: Any,
        step_ids: set[str],
        run_attempts: Sequence[Mapping[str, Any]],
        provenance: list[Mapping[str, Any]],
    ) -> Any:
        """Resolve input templates plus same-run committed step outputs."""

        committed = (
            self._committed_step_outputs(run_attempts)
            if contains_step_reference(source_input)
            else {}
        )
        return resolve_templates(
            source_input,
            inputs=run["inputs"],
            step_ids=step_ids,
            committed=committed,
            provenance=provenance,
        )

    def _committed_step_outputs(
        self, run_attempts: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Select the latest succeeded, non-skipped, committed output per step.

        Attempt history is ordered by step and attempt number, so the last
        eligible record wins.  Skipped, failed, timed-out, ambiguous, and
        otherwise uncommitted attempts never publish an output.
        """

        committed: dict[str, dict[str, Any]] = {}
        for item in run_attempts:
            if item["state"] != StepAttemptState.SUCCEEDED.value or item.get("skipped"):
                continue
            outcome = item.get("outcome")
            if not isinstance(outcome, Mapping):
                continue
            committed[item["step_id"]] = {
                "attempt_id": item["attempt_id"],
                "output": outcome,
                "output_digest": item.get("outcome_digest") or "",
            }
        return committed

    def _checkpoint_payload(
        self,
        run: Mapping[str, Any],
        attempt: Mapping[str, Any],
        reservation_id: str,
    ) -> dict[str, Any]:
        request = attempt["request"]
        return {
            "request_digest": attempt["request_digest"],
            "effect_digest": digest(request["effect_ceiling"]),
            "call_chain": request["call_chain"],
            "idempotency_key": request["idempotency_key"],
            "authority_reservation_id": reservation_id,
            "security_epoch": run["security_epoch"],
        }

    def _catalog_bindings(
        self, snapshot: Mapping[str, Any]
    ) -> dict[tuple[str, str, str, str], OperationBinding]:
        if (
            not isinstance(snapshot.get("catalog_digest"), str)
            or not isinstance(snapshot.get("security_epoch"), int)
            or not isinstance(snapshot.get("operations"), list)
        ):
            raise WorkflowValidationError("Contract catalog snapshot is incomplete")
        result: dict[tuple[str, str, str, str], OperationBinding] = {}
        for raw in snapshot["operations"]:
            if not isinstance(raw, Mapping):
                raise WorkflowValidationError("Contract catalog operation is invalid")
            item = OperationBinding(
                contract_id=str(raw["contract_id"]),
                contract_revision_digest=str(raw["contract_revision_digest"]),
                operation_id=str(raw["operation_id"]),
                function_principal_id=str(raw["function_principal_id"]),
                provider_id=str(raw["provider_id"]),
                input_schema_digest=str(raw["input_schema_digest"]),
                effect_ceiling=tuple(sorted(str(value) for value in raw["effect_ceiling"])),
            )
            if item.key in result:
                raise WorkflowValidationError("Contract catalog has duplicate operation identity")
            result[item.key] = item
        return result

    def _evaluate_when(self, expression: str, inputs: Mapping[str, Any]) -> bool:
        """Evaluate the validated I/O-free condition subset."""

        if expression == "true":
            return True
        if expression == "false":
            return False
        match = re.fullmatch(
            r"inputs\.([a-z][a-z0-9_.-]*)\s*(==|!=)\s*"
            r"(true|false|null|-?[0-9]+|'[^']{0,256}')",
            expression,
        )
        if match is None:
            raise WorkflowValidationError("workflow condition is invalid")
        current: Any = inputs
        for part in match.group(1).split("."):
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        token = match.group(3)
        if token == "true":
            expected: Any = True
        elif token == "false":
            expected = False
        elif token == "null":
            expected = None
        elif token.startswith("'"):
            expected = token[1:-1]
        else:
            expected = int(token)
        result = current == expected
        return result if match.group(2) == "==" else not result

    def _has_cycle(self, graph: Mapping[str, list[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(parent) for parent in graph.get(node, [])):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)

    def _all_steps_succeeded(self, run_id: str, step_count: int | None) -> bool:
        del step_count
        run = self._owned_run(run_id)
        definition = self.store.get_revision(run["revision_digest"])
        compiled_steps = definition["compiled"]["steps"]
        attempts = self.store.list_attempts(run_id)
        succeeded = {
            item["step_id"]
            for item in attempts
            if item["state"] == StepAttemptState.SUCCEEDED.value
        }
        return len(succeeded) == len(compiled_steps)
