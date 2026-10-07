"""Authenticated same-run observations for a live Workflow child dispatch.

No HTTP endpoint or grant: the captured Host issuer registers this resolver.
A receiving owner must separately pin the issuer and its admitted run/revision.
"""
from collections.abc import Mapping
from typing import Any

from core_runtime.invocation_evidence import EvidenceBinding, EvidenceDenied
from tobkiri_protocol.canonical import canonical_digest

from .binding import is_step_reference, parse_step_reference, reference_path_parts, resolve_templates
from .models import WorkflowConflict, WorkflowValidationError
from .store import WorkflowStoreV4

EVIDENCE_KIND = "tobkiri.workflow.attempt.v1"


def _at(value: Any, path: str) -> Any:
    for part in reference_path_parts(path):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isascii() and part.isdecimal() and (
            part == "0" or not part.startswith("0")
        ) and int(part) < len(value):
            value = value[int(part)]
        else:
            raise EvidenceDenied("evidence input path is unavailable")
    return value


class WorkflowAttemptEvidence:
    """Revalidate sealed run and attempt records on each bounded read."""

    def __init__(self, store: WorkflowStoreV4, reference: str, binding: EvidenceBinding) -> None:
        self._store, self._reference, self._binding = store, reference, binding
        self._current()

    def _current(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        attempt = self._store.get_attempt_for_request(self._reference)
        run = self._store.get_run(attempt["run_id"])
        query = self._binding
        request = attempt["request"]
        if (
            run["run_id"] != attempt["run_id"]
            or run["activation_id"] != query.activation_id
            or run["activation_digest"] != query.activation_digest
            or run["security_epoch"] != query.security_epoch
            or run.get("cancel_requested")
            or run["state"] not in {"running", "waiting_approval"}
            or attempt["state"] != "running"
            or attempt["request_digest"] != canonical_digest(request)
            or request["function_principal_id"] != query.target_principal
            or request["contract_id"] != query.contract_id
            or request["operation_id"] != query.operation_id
            or canonical_digest(request["input"]) != query.payload_digest
            or request["idempotency_key"] != query.idempotency_key
        ):
            raise EvidenceDenied("Workflow attempt does not cover this dispatch")
        revision = self._store.get_revision(run["revision_digest"])
        if revision["definition_id"] != run["definition_id"] or revision["revision_digest"] != run["revision_digest"]:
            raise EvidenceDenied("Workflow revision does not match run")
        compiled = revision["compiled"]
        if compiled["catalog_digest"] != run["catalog_digest"] or compiled["security_epoch"] != run["security_epoch"] or (
            compiled["compile_digest"] != canonical_digest({key: value for key, value in compiled.items() if key != "compile_digest"})
        ):
            raise EvidenceDenied("Workflow compiled catalog binding differs")
        matches = [step for step in compiled["steps"] if step["step_id"] == attempt["step_id"]]
        if len(matches) != 1 or any(
            request.get(key) != matches[0]["contract_request"].get(key)
            for key in ("contract_id", "contract_revision_digest", "operation_id", "function_principal_id")
        ):
            raise EvidenceDenied("Workflow current node differs from compiled operation")
        expected_request_id = canonical_digest({
            "run_id": run["run_id"], "step_id": attempt["step_id"],
            "attempt_number": attempt["attempt_number"],
        })
        if request["request_id"] != expected_request_id or attempt["attempt_id"] != (
            f"{run['run_id']}:{attempt['step_id']}:{attempt['attempt_number']}"
        ):
            raise EvidenceDenied("Workflow attempt identity differs")
        return run, attempt, revision

    def read(self, selector: str) -> Mapping[str, Any]:
        run, current, revision = self._current()
        identity = {
            "run_id": run["run_id"], "revision_digest": run["revision_digest"],
            "definition_id": run["definition_id"],
            "run_inputs_digest": canonical_digest(run["inputs"]),
            "attempt_id": current["attempt_id"], "step_id": current["step_id"],
            "request_digest": current["request_digest"],
        }
        if selector != "current" and not selector.startswith("input:/"):
            raise EvidenceDenied("Workflow evidence selector is not supported")
        compiled = revision["compiled"]
        steps = {step["step_id"]: step for step in compiled["steps"]}
        attempts = self._store.list_attempts(run["run_id"])
        committed: dict[str, dict[str, Any]] = {}
        records: dict[str, dict[str, Any]] = {}
        for item in attempts:
            if item["state"] != "succeeded" or item.get("skipped"):
                continue
            if item["run_id"] != run["run_id"] or not isinstance(item.get("outcome"), Mapping):
                raise EvidenceDenied("committed outcome is unavailable")
            if item["outcome_digest"] != canonical_digest({
                "output": item["outcome"], "error_code": None,
                "ambiguous_effect": False, "timed_out": False,
            }):
                raise EvidenceDenied("committed outcome digest differs")
            if item["step_id"] in records:
                raise EvidenceDenied("multiple committed attempts for one node")
            records[item["step_id"]] = item
            committed[item["step_id"]] = {
                "attempt_id": item["attempt_id"], "output": item["outcome"],
                "output_digest": item["outcome_digest"],
            }
        visited: set[str] = set()
        lineage: list[dict[str, Any]] = []

        def verify(step_id: str, item: Mapping[str, Any], *, visit_inputs: bool = True) -> None:
            request = item["request"]
            declared = steps[step_id]["contract_request"]
            if item["request_digest"] != canonical_digest(request) or any(
                request.get(key) != declared.get(key) for key in (
                    "contract_id", "contract_revision_digest", "operation_id", "function_principal_id",
                )
            ):
                raise EvidenceDenied("executed operation differs from compiled node")
            bindings: list[Mapping[str, Any]] = []
            try:
                resolved = resolve_templates(
                    declared["input"], inputs=run["inputs"], step_ids=set(steps),
                    committed=committed, provenance=bindings,
                )
            except (WorkflowConflict, WorkflowValidationError) as error:
                raise EvidenceDenied("Workflow input materialization is unavailable") from error
            if canonical_digest(resolved) != canonical_digest(request["input"]) or (
                list(item.get("output_bindings", [])) != bindings
            ):
                raise EvidenceDenied("executed input lineage differs from compiled node")
            if visit_inputs:
                for bound in bindings:
                    visit(str(bound["step_id"]))

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if len(visited) >= 256 or step_id not in records:
                raise EvidenceDenied("committed lineage is missing or exceeds limit")
            visited.add(step_id)
            item = records[step_id]
            verify(step_id, item)
            lineage.append({
                "step_id": step_id, "attempt_id": item["attempt_id"],
                "operation": {key: item["request"][key] for key in (
                    "contract_id", "contract_revision_digest", "operation_id", "function_principal_id",
                )},
                "input_digest": canonical_digest(item["request"]["input"]),
                "outcome_digest": item["outcome_digest"],
                "completion_status": (
                    item["outcome"].get("status")
                    if item["outcome"].get("status") in ("ok", "error", "succeeded", "failed")
                    else None
                ),
                "has_tool_intents": bool(item["outcome"].get("tool_intents")),
            })

        if selector == "current":
            # Identity alone must not authenticate a request whose materialized
            # input differs from its published node. Verify every direct input
            # and upstream binding without returning their private contents.
            verify(current["step_id"], current)
            self._current()
            return identity
        source = _at(steps[current["step_id"]]["contract_request"]["input"], selector[6:])
        if not is_step_reference(source):
            raise EvidenceDenied("nominated input has no committed output binding")
        producer, output_path = parse_step_reference(source, set(steps))
        verify(current["step_id"], current, visit_inputs=False)
        visit(producer)
        value = _at(records[producer]["outcome"], output_path)
        if canonical_digest(value) != canonical_digest(_at(current["request"]["input"], selector[6:])):
            raise EvidenceDenied("selected committed value differs from invocation input")
        self._current()
        return {**identity, "value": value, "source_step": producer,
                "source_path": output_path, "lineage": lineage}
