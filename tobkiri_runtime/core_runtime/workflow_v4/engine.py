"""Authority-aware Workflow v4 compiler and state machine."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import secrets
import time
from typing import Any, Callable, Mapping

from .models import (
    ApprovalState,
    DefinitionState,
    InvocationOutcome,
    OperationBinding,
    RunState,
    StepAttemptState,
    WorkflowConflict,
    WorkflowDenied,
    WorkflowValidationError,
    digest,
    require_mapping,
)
from .graph_compiler import GraphCompilerV4
from .protocols import (
    AuthorityProvider,
    ContractCatalogProvider,
    ContractInvocationProvider,
    InputValidator,
)
from .store import WorkflowStoreV4

_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TEMPLATE = re.compile(r"^\$\{inputs\.([a-z][a-z0-9_.-]*)\}$")
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
    ) -> None:
        self.store = store
        self._catalog = catalog
        self._authority = authority
        self._invoker = invoker
        self._validator = validator
        self._clock = clock

    def operation_palette(self) -> dict[str, Any]:
        """Project the editor palette from the exact active Contract catalog."""

        snapshot = self._catalog.snapshot()
        bindings = self._catalog_bindings(snapshot)
        operations = [
            {
                "contract_id": item.contract_id,
                "contract_revision_digest": item.contract_revision_digest,
                "operation_id": item.operation_id,
                "function_principal_id": item.function_principal_id,
                "provider_id": item.provider_id,
                "input_schema_digest": item.input_schema_digest,
                "output_schema_digest": item.output_schema_digest,
                "effect_ceiling": list(item.effect_ceiling),
            }
            for item in sorted(bindings.values(), key=lambda value: value.key)
        ]
        return {
            "catalog_digest": str(snapshot["catalog_digest"]),
            "security_epoch": int(snapshot["security_epoch"]),
            "operations": operations,
        }

    def validate_definition(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a Definition without reserving authority or performing I/O."""

        errors: list[str] = []
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
                if not isinstance(input_value, Mapping):
                    errors.append(f"steps[{index}].request.input must be an object")
                elif not self._contains_template(input_value):
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
        return {**compiled, "compile_digest": digest(compiled)}

    def compile_graph_preview(self, graph: Mapping[str, Any]) -> dict[str, Any]:
        """Compile editor metadata into exact Workflow v4 runtime steps.

        Port contracts come only from explicit graph metadata or schema digests
        in the captured active Contract catalog.  The result reserves no
        authority and cannot execute a Provider.
        """

        snapshot = self._catalog.snapshot()
        adapter = GraphCompilerV4(snapshot)
        graph_result = adapter.compile(graph)
        document = require_mapping(graph_result["document"], "compiled graph document")
        compiled = self.compile_preview(document)
        if compiled["catalog_digest"] != adapter.catalog_digest:
            raise WorkflowDenied("active Contract catalog changed during graph compilation")
        body = {
            "graph_compile_api_version": "io.tobkiri.rumi-graph-compile.v4",
            "catalog_digest": adapter.catalog_digest,
            "security_epoch": adapter.security_epoch,
            "document": dict(document),
            "normalized_graph": graph_result["normalized_graph"],
            "compiled": compiled,
        }
        return {**body, "graph_compile_digest": digest(body)}

    def start_run(
        self,
        *,
        definition_id: str,
        inputs: Mapping[str, Any],
        occurrence_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued Run pinned to Definition, activation, and catalog."""

        definition = self.store.get_definition(definition_id)
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
        )

    def execute_step(self, run_id: str, step_id: str) -> dict[str, Any]:
        """Execute or resume one StepAttempt through reserve/commit authority."""

        run = self.store.get_run(run_id)
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
        if attempts and attempts[-1]["state"] == StepAttemptState.FAILED.value:
            retry_not_before_ms = int(attempts[-1].get("retry_not_before_ms", 0))
            if int(self._clock() * 1000) < retry_not_before_ms:
                raise WorkflowConflict("workflow step retry backoff has not elapsed")
        attempt_number = len(attempts) + 1
        if attempt_number > int(step["retry"]["max_attempts"]):
            raise WorkflowConflict("workflow step retry limit is exhausted")
        request = self._materialize_request(run, step, attempt_number)
        attempt = self.store.create_attempt(
            run_id=run_id,
            step_id=step_id,
            attempt_number=attempt_number,
            request=request,
        )
        if not self._evaluate_when(str(step["when"]), run["inputs"]):
            attempt = self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.PENDING},
                target=StepAttemptState.SUCCEEDED,
                updates={"skipped": True, "condition": step["when"]},
            )
            if self._all_steps_succeeded(run_id, step_count=None):
                self.store.transition_run(
                    run_id,
                    expected={RunState.RUNNING},
                    target=RunState.SUCCEEDED,
                )
            return attempt
        reservation = self._authority.reserve(self._authority_request(run, attempt))
        self._validate_reservation(run, attempt, reservation)
        if reservation.state is ApprovalState.WAITING_APPROVAL:
            self.store.checkpoint(
                attempt["attempt_id"],
                self._checkpoint_payload(run, attempt, reservation.reservation_id),
            )
            attempt = self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.PENDING},
                target=StepAttemptState.WAITING_APPROVAL,
                updates={"authority_reservation_id": reservation.reservation_id},
            )
            self.store.transition_run(
                run_id,
                expected={RunState.RUNNING},
                target=RunState.WAITING_APPROVAL,
            )
            return attempt
        if reservation.state not in {ApprovalState.RESERVED, ApprovalState.APPROVED}:
            return self._fail_approval(run, attempt, reservation.state)
        return self._dispatch(run, step, attempt, reservation.reservation_id)

    def advance_run(self, run_id: str) -> dict[str, Any]:
        """Execute dependency-ready steps with bounded real concurrency."""

        run = self.store.get_run(run_id)
        if RunState(run["state"]) is RunState.QUEUED:
            run = self.store.transition_run(
                run_id, expected={RunState.QUEUED}, target=RunState.RUNNING
            )
        if RunState(run["state"]) is not RunState.RUNNING:
            raise WorkflowConflict("workflow run cannot advance")
        definition = self.store.get_revision(run["revision_digest"])
        compiled = require_mapping(definition.get("compiled"), "compiled definition")
        self._validate_run_snapshot(run, compiled)
        attempts = self.store.list_attempts(run_id)
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
            return {"run": self.store.get_run(run_id), "attempts": []}
        concurrency = min(int(compiled["max_concurrency"]), len(ready))
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="workflow-v4"
        ) as executor:
            results = list(executor.map(lambda item: self.execute_step(run_id, item), ready))
        return {"run": self.store.get_run(run_id), "attempts": results}

    def pause_run(self, run_id: str) -> dict[str, Any]:
        """Pause a running Run between effects."""

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

        return self.store.transition_run(
            run_id, expected={RunState.PAUSED}, target=RunState.RUNNING
        )

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Fence reservations and propagate cancel to in-flight requests."""

        self.store.get_run(run_id)
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
                    self._authority.revoke(reservation_id, reason="workflow_cancelled")
                self._invoker.cancel(attempt["request"]["request_id"])
                self.store.transition_attempt(
                    attempt["attempt_id"],
                    expected={state},
                    target=StepAttemptState.CANCELLED,
                )
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

    def reconcile_recovery(self, run_id: str) -> dict[str, Any]:
        """Mark crash-surviving in-flight effects ambiguous, never auto-retry."""

        changed = False
        for attempt in self.store.list_attempts(run_id):
            state = StepAttemptState(attempt["state"])
            if state in {StepAttemptState.DISPATCHING, StepAttemptState.RUNNING}:
                self.store.transition_attempt(
                    attempt["attempt_id"],
                    expected={state},
                    target=StepAttemptState.AMBIGUOUS_EFFECT,
                )
                changed = True
        if not changed:
            return self.store.get_run(run_id)
        return self.store.transition_run(
            run_id,
            expected={RunState.RUNNING},
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
            return self.store.transition_run(
                run["run_id"],
                expected={RunState.WAITING_APPROVAL},
                target=RunState.TIMED_OUT,
            )
        self.store.transition_run(
            run["run_id"],
            expected={RunState.WAITING_APPROVAL},
            target=RunState.RUNNING,
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
            self.store.transition_run(
                run["run_id"],
                expected={RunState.RUNNING},
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
            outcome = self._invoker.invoke(attempt["request"], authority=authority)
        except Exception:
            outcome = InvocationOutcome(error_code="provider_interrupted", ambiguous_effect=True)
        try:
            outcome_digest = digest(
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
        if outcome.ambiguous_effect:
            try:
                self._authority.finish(
                    reservation_id,
                    outcome_digest=outcome_digest,
                    state="ambiguous_effect",
                )
            except Exception:
                pass
            self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.RUNNING},
                target=StepAttemptState.AMBIGUOUS_EFFECT,
                updates={"outcome_digest": outcome_digest},
            )
            return self.store.transition_run(
                run["run_id"],
                expected={RunState.RUNNING},
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
            self.store.transition_attempt(
                attempt["attempt_id"],
                expected={StepAttemptState.RUNNING},
                target=StepAttemptState.AMBIGUOUS_EFFECT,
                updates={"outcome_digest": outcome_digest},
            )
            self.store.transition_run(
                run["run_id"],
                expected={RunState.RUNNING},
                target=RunState.NEEDS_RECONCILIATION,
            )
            raise WorkflowDenied("authority outcome commit failed closed") from exc
        result = self.store.transition_attempt(
            attempt["attempt_id"],
            expected={StepAttemptState.RUNNING},
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
        if target is StepAttemptState.SUCCEEDED:
            if self._all_steps_succeeded(run["run_id"], step_count=None):
                try:
                    self.store.transition_run(
                        run["run_id"],
                        expected={RunState.RUNNING},
                        target=RunState.SUCCEEDED,
                    )
                except WorkflowConflict:
                    if self.store.get_run(run["run_id"])["state"] != RunState.SUCCEEDED.value:
                        raise
            return result
        if target is StepAttemptState.FAILED and int(attempt["attempt_number"]) < int(
            step["retry"]["max_attempts"]
        ):
            return result
        self.store.transition_run(run["run_id"], expected={RunState.RUNNING}, target=run_target)
        return result

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
        self.store.transition_run(
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

    def _materialize_request(
        self, run: Mapping[str, Any], step: Mapping[str, Any], attempt_number: int
    ) -> dict[str, Any]:
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
        input_value = self._resolve_templates(source.get("input", {}), run["inputs"])
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
        }

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
                output_schema_digest=str(raw["output_schema_digest"]),
                effect_ceiling=tuple(sorted(str(value) for value in raw["effect_ceiling"])),
            )
            if item.key in result:
                raise WorkflowValidationError("Contract catalog has duplicate operation identity")
            result[item.key] = item
        return result

    def _resolve_templates(self, value: Any, inputs: Mapping[str, Any]) -> Any:
        if isinstance(value, str):
            match = _TEMPLATE.fullmatch(value)
            if not match:
                return value
            current: Any = inputs
            for part in match.group(1).split("."):
                if not isinstance(current, Mapping) or part not in current:
                    raise WorkflowValidationError("workflow input template is unresolved")
                current = current[part]
            return current
        if isinstance(value, Mapping):
            return {key: self._resolve_templates(item, inputs) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_templates(item, inputs) for item in value]
        return value

    def _contains_template(self, value: Any) -> bool:
        if isinstance(value, str):
            return bool(_TEMPLATE.fullmatch(value))
        if isinstance(value, Mapping):
            return any(self._contains_template(item) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_template(item) for item in value)
        return False

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
        run = self.store.get_run(run_id)
        definition = self.store.get_revision(run["revision_digest"])
        compiled_steps = definition["compiled"]["steps"]
        attempts = self.store.list_attempts(run_id)
        succeeded = {
            item["step_id"]
            for item in attempts
            if item["state"] == StepAttemptState.SUCCEEDED.value
        }
        return len(succeeded) == len(compiled_steps)
