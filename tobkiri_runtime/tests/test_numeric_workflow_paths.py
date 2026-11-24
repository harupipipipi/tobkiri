"""Provisional overlay regressions: real Workflow engine/store, fixture dispatch.

No captured sealed production artifact, VM/native backend, network, model,
packaging generator, or end-to-end production authority claim is made here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import struct
from typing import Any, Mapping

import pytest
from jsonschema import Draft202012Validator

from core_runtime.workflow_v4 import (
    ApprovalState, WorkflowConflict, WorkflowDenied, WorkflowEngineV4,
    WorkflowProviderV4, WorkflowStoreV4,
)
from core_runtime.workflow_v4.models import (
    AuthorityReservation, DispatchAuthority, InvocationOutcome,
)
from tobkiri_protocol.canonical import canonical_digest

REVISION = "sha256:" + "1" * 64
INPUT_SCHEMA = {
    "type": "object", "required": ["value"],
    "properties": {"value": {"type": "number"}},
    "additionalProperties": False,
}
SCHEMA_DIGEST = canonical_digest(INPUT_SCHEMA)
VALUES = [0.375, -0.0, float.fromhex("0x0.0000000000001p-1022")]
TAG_OBJECT = {"_tobkiri_flow_data_encoding": {"version": "tobkiri.flow-data.ieee754.v1", "paths": [["value"]]}, "value": ["f", "3fd8000000000000"]}


def assert_float_bits(actual: Any, expected: float) -> None:
    """Check numeric type and all IEEE754 bits, including signed zero."""
    assert type(actual) is float
    assert struct.pack("!d", actual) == struct.pack("!d", expected)


class Catalog:
    """Fixed canonical authority/schema fixture for two numeric operations."""

    def snapshot(self) -> dict[str, Any]:
        return {
            "catalog_digest": "sha256:" + "2" * 64,
            "security_epoch": 7,
            "activation": {"activation_id": "test-activation", "activation_digest": "sha256:" + "3" * 64},
            "operations": [
                {
                    "contract_id": "example.numeric.v1",
                    "contract_revision_digest": REVISION,
                    "operation_id": operation,
                    "function_principal_id": "example.numeric.provider",
                    "provider_id": "example.numeric",
                    "input_schema_digest": SCHEMA_DIGEST,
                    "effect_ceiling": ["capability:numeric"],
                }
                for operation in ("produce", "consume")
            ],
        }


class Validator:
    """Real JSON Schema number validation with observed resolved inputs."""

    def __init__(self) -> None:
        self.values: list[Any] = []

    def validate(self, schema_digest: str, value: Mapping[str, Any]) -> list[str]:
        assert schema_digest == SCHEMA_DIGEST
        self.values.append(deepcopy(value))
        return [error.message for error in Draft202012Validator(INPUT_SCHEMA).iter_errors(value)]


class Authority:
    """Deterministic test authority; deliberately not production approval proof."""

    def __init__(self) -> None:
        self.state = ApprovalState.RESERVED
        self.reservations: dict[str, AuthorityReservation] = {}
        self.commit_count = 0

    def reserve(self, request: Mapping[str, Any]) -> AuthorityReservation:
        reservation = AuthorityReservation(
            reservation_id=f"reservation-{len(self.reservations) + 1}",
            state=self.state, request_digest=request["request_digest"],
            security_epoch=request["security_epoch"], expires_at=1000.0,
        )
        self.reservations[reservation.reservation_id] = reservation
        return reservation

    def inspect(self, reservation_id: str) -> AuthorityReservation:
        return self.reservations[reservation_id]

    def commit(self, reservation_id: str, *, request_digest: str, security_epoch: int) -> DispatchAuthority:
        reservation = self.reservations[reservation_id]
        if reservation.state not in {ApprovalState.RESERVED, ApprovalState.APPROVED}:
            raise WorkflowDenied("approval missing")
        assert request_digest == reservation.request_digest
        self.commit_count += 1
        return DispatchAuthority(f"test-token-{self.commit_count}", reservation_id, request_digest, security_epoch)

    def finish(self, reservation_id: str, *, outcome_digest: str, state: str) -> None:
        assert outcome_digest.startswith("sha256:")

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        del reservation_id, reason


class Invoker:
    """Fixture provider returns exact fractional inputs without computation."""

    def __init__(self) -> None:
        self.requests: list[Mapping[str, Any]] = []
        self.fail_consumer_once = False

    def invoke(self, request: Mapping[str, Any], *, authority: DispatchAuthority, dispatch_fence: Any = None) -> InvocationOutcome:
        assert authority.dispatch_token.startswith("test-token-")
        if dispatch_fence is not None:
            dispatch_fence(request["request_id"])
        self.requests.append(deepcopy(request))
        if request["operation_id"] == "consume" and self.fail_consumer_once:
            self.fail_consumer_once = False
            return InvocationOutcome(error_code="retryable_test_failure")
        return InvocationOutcome(output={"value": request["input"]["value"], "tag_object": deepcopy(TAG_OBJECT)})

    def cancel(self, request_id: str) -> None:
        del request_id


class Runtime:
    """A restartable engine using actual on-disk authenticated Workflow state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.authority = Authority()
        self.invoker = Invoker()
        self.validator = Validator()
        self.open()

    def open(self) -> None:
        self.store = WorkflowStoreV4(self.path, clock=lambda: 100.0)
        self.engine = WorkflowEngineV4(
            store=self.store, catalog=Catalog(), authority=self.authority,
            invoker=self.invoker, validator=self.validator, clock=lambda: 100.0,
        )
        self.provider = WorkflowProviderV4(self.engine)

    def restart(self) -> None:
        self.store.close()
        self.open()

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.provider.invoke(operation, payload)


@pytest.fixture
def runtime(tmp_path: Path):
    case = Runtime(tmp_path / "numeric.sqlite3")
    try:
        yield case
    finally:
        case.store.close()


def document(value: Any = "${inputs.value}") -> dict[str, Any]:
    """Return a two-step producer-to-consumer numeric flow."""
    def step(step_id: str, source: Any, dependencies: list[str]) -> dict[str, Any]:
        return {
            "id": step_id, "depends_on": dependencies,
            "request": {
                "contract_id": "example.numeric.v1", "contract_revision_digest": REVISION,
                "operation_id": step_id, "function_principal_id": "example.numeric.provider",
                "input": {"value": source},
            },
            "retry": {"max_attempts": 2, "backoff_ms": 0},
        }
    return {
        "workflow_api_version": "io.tobkiri.workflow.v4", "name": "Numeric chain",
        "max_concurrency": 1,
        "steps": [step("produce", value, []), step("consume", "${steps.produce.output.value}", ["produce"])],
    }


def start(runtime: Runtime, value: float, *, literal: bool = False) -> None:
    """Publish and create a numeric run through the actual public provider."""
    created = runtime.invoke("definition.create", {"definition_id": "numeric.flow", "document": document(value) if literal else document()})
    runtime.invoke("definition.publish", {"definition_id": "numeric.flow", "if_match": created["etag"]})
    runtime.invoke("run.create", {"definition_id": "numeric.flow", "run_id": "numeric-run", "inputs": {"value": value}})


def execute(runtime: Runtime, step_id: str) -> dict[str, Any]:
    """Execute an exact step through Workflow's normal provider surface."""
    return runtime.invoke("run.step.execute", {"run_id": "numeric-run", "step_id": step_id})


@pytest.mark.parametrize("value", VALUES, ids=["fraction", "negative-zero", "subnormal"])
@pytest.mark.parametrize("literal", [False, True], ids=["run-input", "definition-literal"])
def test_fractional_chain_survives_store_restart_and_exact_binding(runtime: Runtime, value: float, literal: bool) -> None:
    start(runtime, value, literal=literal)
    runtime.restart()
    assert_float_bits(runtime.store.get_run("numeric-run")["inputs"]["value"], value)
    produced = execute(runtime, "produce")
    assert produced["state"] == "succeeded"
    assert_float_bits(produced["outcome"]["value"], value)
    assert produced["outcome"]["tag_object"] == TAG_OBJECT
    runtime.restart()
    consumed = execute(runtime, "consume")
    assert consumed["state"] == "succeeded"
    assert_float_bits(consumed["request"]["input"]["value"], value)
    assert_float_bits(consumed["outcome"]["value"], value)
    assert consumed["output_bindings"] == [{
        "step_id": "produce", "attempt_id": produced["attempt_id"],
        "path": "value", "output_digest": produced["outcome_digest"],
    }]
    assert runtime.store.get_attempt_for_request(consumed["request"]["request_id"])["attempt_id"] == consumed["attempt_id"]
    assert runtime.authority.commit_count == 2
    assert len(runtime.invoker.requests) == 2
    assert_float_bits(runtime.validator.values[-1]["value"], value)
    runtime.restart()
    finished = runtime.invoke("run.get", {"run_id": "numeric-run"})
    assert finished["run"]["state"] == "succeeded"
    assert len(finished["attempts"]) == 2
    assert_float_bits(finished["attempts"][-1]["outcome"]["value"], value)
    with pytest.raises(WorkflowConflict):
        execute(runtime, "consume")
    assert len(runtime.invoker.requests) == 2


def test_fractional_bound_input_survives_approval_wait_restart_resume_and_retry(runtime: Runtime) -> None:
    start(runtime, 0.375)
    produced = execute(runtime, "produce")
    runtime.authority.state = ApprovalState.WAITING_APPROVAL
    waiting = execute(runtime, "consume")
    assert waiting["state"] == "waiting_approval"
    assert len(runtime.invoker.requests) == 1
    runtime.restart()
    reservation_id = waiting["authority_reservation_id"]
    runtime.authority.reservations[reservation_id] = replace(
        runtime.authority.reservations[reservation_id], state=ApprovalState.APPROVED,
    )
    runtime.invoker.fail_consumer_once = True
    failed = execute(runtime, "consume")
    assert failed["state"] == "failed"
    runtime.restart()
    runtime.authority.state = ApprovalState.RESERVED
    succeeded = execute(runtime, "consume")
    assert succeeded["state"] == "succeeded"
    assert succeeded["attempt_number"] == 2
    assert succeeded["output_bindings"] == failed["output_bindings"] == waiting["output_bindings"]
    assert succeeded["output_bindings"][0]["output_digest"] == produced["outcome_digest"]
    for request in runtime.invoker.requests:
        assert_float_bits(request["input"]["value"], 0.375)
    assert len(runtime.invoker.requests) == 3


@pytest.mark.parametrize("target", ["run", "producer-outcome"])
def test_tampered_fractional_record_fails_before_any_new_dispatch(runtime: Runtime, target: str) -> None:
    start(runtime, 0.375)
    if target == "producer-outcome":
        execute(runtime, "produce")
        table, key, identity, next_step = "workflow_attempts", "attempt_id", "numeric-run:produce:1", "consume"
    else:
        table, key, identity, next_step = "workflow_runs", "run_id", "numeric-run", "produce"
    row = runtime.store._connection.execute(f"SELECT payload FROM {table} WHERE {key}=?", (identity,)).fetchone()
    raw = row["payload"]
    assert "3fd8000000000000" in raw, "float record must use bit-preserving transport"
    changed = raw.replace("3fd8000000000000", "3fe8000000000000")
    assert changed != raw
    runtime.store._connection.execute(f"UPDATE {table} SET payload=? WHERE {key}=?", (changed, identity))
    dispatches = len(runtime.invoker.requests)
    commits = runtime.authority.commit_count
    runtime.restart()
    with pytest.raises(WorkflowDenied, match="authenticat"):
        execute(runtime, next_step)
    assert len(runtime.invoker.requests) == dispatches
    assert runtime.authority.commit_count == commits
