"""Regression tests for typed Workflow v4 step-output binding.

Covers the `${steps.<step_id>.output.<path>}` public reference format, the
explicit depends_on rule, ambiguous dotted-ID parsing, committed-output
eligibility, retry/approval pinning, cross-run isolation, and the
display-reduced palette schema projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from core_runtime.workflow_v4 import (
    ApprovalState,
    RunState,
    StepAttemptState,
    WorkflowConflict,
    WorkflowDenied,
    WorkflowEngineV4,
    WorkflowProviderV4,
    WorkflowStoreV4,
    WorkflowValidationError,
)
from core_runtime.workflow_v4.binding import (
    parse_step_reference,
    project_display_schema,
    project_ports,
    resolve_templates,
)
from core_runtime.workflow_v4.integration import _ResolvedCatalog
from core_runtime.workflow_v4.models import (
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    digest,
)
from tobkiri_protocol.canonical import canonical_digest

CATALOG_DIGEST = "sha256:" + "5" * 64
ACTIVATION_DIGEST = "sha256:" + "6" * 64

STT_INPUT_SCHEMA = {
    "type": "object",
    "required": ["audio"],
    "properties": {
        "audio": {"type": "string", "format": "uri-reference"},
        "language": {"type": "string"},
    },
}
STT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["transcript"],
    "properties": {"transcript": {"type": "string"}},
}
LLM_INPUT_SCHEMA = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string"},
        "model_profile": {
            "type": "string",
            "x-tobkiri-selector": "model-profile",
        },
        "max_tokens": {"type": "integer"},
    },
}
LLM_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["reply"],
    "properties": {"reply": {"type": "string"}},
}
TTS_INPUT_SCHEMA = {
    "type": "object",
    "required": ["text", "voice"],
    "properties": {
        "text": {"type": "string"},
        "voice": {"type": "string"},
        "speed": {"type": "number"},
    },
}
TTS_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["audio_url"],
    "properties": {"audio_url": {"type": "string"}},
}


def _operation(
    name: str,
    *,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> dict[str, Any]:
    contract = f"media.{name}.v1"
    return {
        "contract_id": contract,
        "contract_revision_digest": "sha256:" + "a" * 64,
        "operation_id": f"{name}.run",
        "function_principal_id": f"media.{name}.provider",
        "provider_id": f"media.{name}",
        "input_schema_digest": canonical_digest(input_schema),
        "effect_ceiling": [f"capability:{name}"],
        "_output_schema_digest": canonical_digest(output_schema),
        "_input_schema": input_schema,
        "_output_schema": output_schema,
    }


STT_OP = _operation("stt", input_schema=STT_INPUT_SCHEMA, output_schema=STT_OUTPUT_SCHEMA)
LLM_OP = _operation("llm", input_schema=LLM_INPUT_SCHEMA, output_schema=LLM_OUTPUT_SCHEMA)
TTS_OP = _operation("tts", input_schema=TTS_INPUT_SCHEMA, output_schema=TTS_OUTPUT_SCHEMA)


def _catalog_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: operation[key]
        for key in (
            "contract_id",
            "contract_revision_digest",
            "operation_id",
            "function_principal_id",
            "provider_id",
            "input_schema_digest",
            "effect_ceiling",
        )
    }


class Catalog:
    """Captured catalog fixture with digest-keyed canonical schemas."""

    def __init__(self) -> None:
        operations = [STT_OP, LLM_OP, TTS_OP]
        self.value = {
            "catalog_digest": CATALOG_DIGEST,
            "security_epoch": 7,
            "activation": {
                "activation_id": "activation-test",
                "activation_digest": ACTIVATION_DIGEST,
            },
            "operations": [_catalog_operation(item) for item in operations],
            "schemas": {},
            "operation_output_schemas": [
                {
                    "contract_id": item["contract_id"],
                    "contract_revision_digest": item["contract_revision_digest"],
                    "operation_id": item["operation_id"],
                    "function_principal_id": item["function_principal_id"],
                    "output_schema_digest": item["_output_schema_digest"],
                }
                for item in operations
            ],
        }
        for item in operations:
            self.value["schemas"][item["input_schema_digest"]] = item["_input_schema"]
            self.value["schemas"][item["_output_schema_digest"]] = item["_output_schema"]

    def snapshot(self) -> Mapping[str, Any]:
        return self.value


class SchemaValidator:
    """Validate resolved inputs against the captured canonical schemas."""

    def __init__(self, schemas: Mapping[str, Mapping[str, Any]]) -> None:
        self._schemas = dict(schemas)

    def validate(
        self, schema_digest: str, value: Mapping[str, Any]
    ) -> list[str]:
        schema = self._schemas.get(schema_digest)
        if schema is None:
            return ["input schema is outside the captured catalog"]
        validator = Draft202012Validator(schema)
        return sorted(
            error.message for error in validator.iter_errors(value)
        )


class Authority:
    """Authority fixture implementing reserve/inspect/commit fences."""

    def __init__(self, state: ApprovalState = ApprovalState.RESERVED) -> None:
        self.state = state
        self.reservations: dict[str, AuthorityReservation] = {}
        self.commit_count = 0

    def reserve(self, request: Mapping[str, Any]) -> AuthorityReservation:
        reservation = AuthorityReservation(
            reservation_id=f"reservation-{len(self.reservations) + 1}",
            state=self.state,
            request_digest=str(request["request_digest"]),
            security_epoch=int(request["security_epoch"]),
            expires_at=1000.0,
        )
        self.reservations[reservation.reservation_id] = reservation
        return reservation

    def inspect(self, reservation_id: str) -> AuthorityReservation:
        return self.reservations[reservation_id]

    def commit(
        self, reservation_id: str, *, request_digest: str, security_epoch: int
    ) -> DispatchAuthority:
        reservation = self.reservations[reservation_id]
        if reservation.state not in {ApprovalState.RESERVED, ApprovalState.APPROVED}:
            raise WorkflowDenied("authority is not approved")
        self.commit_count += 1
        return DispatchAuthority(
            dispatch_token=f"one-shot-{self.commit_count}",
            reservation_id=reservation_id,
            request_digest=request_digest,
            security_epoch=security_epoch,
        )

    def finish(self, reservation_id: str, *, outcome_digest: str, state: str) -> None:
        assert outcome_digest.startswith("sha256:")

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        del reservation_id, reason


class RoutingInvoker:
    """Fake provider chain: dispatch deterministic outputs per operation."""

    def __init__(self) -> None:
        self.requests: list[Mapping[str, Any]] = []
        self.call_counts: dict[str, int] = {}
        self.fail_operations: set[str] = set()
        self.transcript_override: Any = "hello world"

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
        dispatch_fence: Any = None,
    ) -> InvocationOutcome:
        if dispatch_fence is not None:
            try:
                dispatch_fence(str(request["request_id"]))
            except WorkflowDenied:
                return InvocationOutcome(error_code="dispatch_fenced", dispatched=False)
        assert authority.dispatch_token.startswith("one-shot-")
        self.requests.append(request)
        operation = request["operation_id"]
        self.call_counts[operation] = self.call_counts.get(operation, 0) + 1
        if operation in self.fail_operations:
            return InvocationOutcome(error_code="provider_failed")
        if operation == "stt.run":
            transcript = self.transcript_override
            if callable(transcript):
                transcript = transcript(self.call_counts[operation])
            return InvocationOutcome(output={"transcript": transcript})
        if operation == "llm.run":
            return InvocationOutcome(
                output={"reply": f"echo:{request['input']['prompt']}"}
            )
        if operation == "tts.run":
            return InvocationOutcome(
                output={"audio_url": f"file://{request['input']['voice']}.mp3"}
            )
        return InvocationOutcome(error_code="unknown_operation")

    def cancel(self, request_id: str) -> None:
        del request_id


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker]:
    catalog = Catalog()
    authority = Authority()
    invoker = RoutingInvoker()
    engine = WorkflowEngineV4(
        store=WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0),
        catalog=catalog,
        authority=authority,
        invoker=invoker,
        validator=SchemaValidator(catalog.value["schemas"]),
        clock=lambda: 100.0,
    )
    return WorkflowProviderV4(engine), catalog, authority, invoker


def _step(step_id: str, operation: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    request = {
        "contract_id": operation["contract_id"],
        "contract_revision_digest": operation["contract_revision_digest"],
        "operation_id": operation["operation_id"],
        "function_principal_id": operation["function_principal_id"],
        "input": extra.pop("input"),
    }
    return {"id": step_id, "request": request, **extra}


def chain_document(**overrides: Any) -> dict[str, Any]:
    """Build the MP3 -> STT -> LLM -> TTS bound definition."""

    steps = [
        _step("stt", STT_OP, input={"audio": "${inputs.audio}"}),
        _step(
            "llm",
            LLM_OP,
            depends_on=["stt"],
            input={"prompt": "${steps.stt.output.transcript}"},
        ),
        _step(
            "tts",
            TTS_OP,
            depends_on=["llm"],
            input={
                "text": "${steps.llm.output.reply}",
                "voice": "${inputs.voice}",
            },
        ),
    ]
    steps += overrides.pop("steps", [])
    document = {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "name": "MP3 to speech chain",
        "max_concurrency": 1,
        "steps": steps,
    }
    document.update(overrides)
    return document


def publish(
    provider: WorkflowProviderV4,
    definition_id: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    created = provider.invoke(
        "definition.create", {"definition_id": definition_id, "document": document}
    )
    return provider.invoke(
        "definition.publish",
        {"definition_id": definition_id, "if_match": created["etag"]},
    )


def start_chain_run(provider: WorkflowProviderV4, run_id: str) -> None:
    publish(provider, f"workflow.{run_id}", chain_document())
    provider.invoke(
        "run.create",
        {
            "definition_id": f"workflow.{run_id}",
            "run_id": run_id,
            "inputs": {"audio": "file://in.mp3", "voice": "ann"},
        },
    )


def test_three_step_chain_binds_committed_outputs(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    start_chain_run(provider, "run-chain")
    with pytest.raises(WorkflowConflict, match="dependencies"):
        provider.invoke("run.step.execute", {"run_id": "run-chain", "step_id": "llm"})
    advanced: Mapping[str, Any] = {}
    for _ in range(4):
        advanced = provider.invoke("run.advance", {"run_id": "run-chain"})
        if advanced["run"]["state"] != RunState.RUNNING.value:
            break
    assert advanced["run"]["state"] == RunState.SUCCEEDED.value
    assert [request["operation_id"] for request in invoker.requests] == [
        "stt.run",
        "llm.run",
        "tts.run",
    ]
    assert invoker.requests[0]["input"] == {"audio": "file://in.mp3"}
    assert invoker.requests[1]["input"] == {"prompt": "hello world"}
    assert invoker.requests[2]["input"] == {
        "text": "echo:hello world",
        "voice": "ann",
    }
    attempts = provider.invoke("run.get", {"run_id": "run-chain"})["attempts"]
    llm_attempt = next(item for item in attempts if item["step_id"] == "llm")
    assert llm_attempt["output_bindings"] == [
        {
            "step_id": "stt",
            "attempt_id": "run-chain:stt:1",
            "path": "transcript",
            "output_digest": llm_attempt["output_bindings"][0]["output_digest"],
        }
    ]
    tts_attempt = next(item for item in attempts if item["step_id"] == "tts")
    assert tts_attempt["output_bindings"][0]["step_id"] == "llm"
    assert authority.commit_count == 3


def test_reference_requires_depends_on_and_rejects_ambiguous_dotted_ids(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, _catalog, _authority, _invoker = runtime
    missing_dep = chain_document()
    missing_dep["steps"][1]["depends_on"] = []
    result = provider.invoke("definition.validate", {"document": missing_dep})
    assert not result["valid"]
    assert any("depends_on" in error for error in result["errors"])

    unknown = chain_document()
    unknown["steps"][1]["request"]["input"] = {
        "prompt": "${steps.ghost.output.transcript}"
    }
    unknown["steps"][1]["depends_on"] = ["stt"]
    result = provider.invoke("definition.validate", {"document": unknown})
    assert not result["valid"]
    assert any("no declared step match" in error for error in result["errors"])

    ambiguous = {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "steps": [
            _step("a", STT_OP, input={"audio": "${inputs.audio}"}),
            _step(
                "a.output",
                LLM_OP,
                depends_on=["a"],
                input={"prompt": "${steps.a.output.transcript}"},
            ),
            _step(
                "sink",
                TTS_OP,
                depends_on=["a", "a.output"],
                input={
                    "text": "${steps.a.output.output.reply}",
                    "voice": "ann",
                },
            ),
        ],
    }
    result = provider.invoke("definition.validate", {"document": ambiguous})
    assert not result["valid"]
    assert any("ambiguous" in error for error in result["errors"])

    # A dotted suffix that is not itself a declared step still resolves
    # uniquely: `a.output` never matches `${steps.a.output.transcript}`
    # because its declared prefix would need a second `.output.` segment.
    unique_match = {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "steps": [
            _step("a", STT_OP, input={"audio": "${inputs.audio}"}),
            _step(
                "a.output",
                LLM_OP,
                depends_on=["a"],
                input={"prompt": "${steps.a.output.transcript}"},
            ),
            _step(
                "sink",
                TTS_OP,
                depends_on=["a", "a.output"],
                input={"text": "${steps.a.output.transcript}", "voice": "ann"},
            ),
        ],
    }
    result = provider.invoke("definition.validate", {"document": unique_match})
    assert result["valid"], result["errors"]

    dotted_id = {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "steps": [
            _step("a.b.c", STT_OP, input={"audio": "${inputs.audio}"}),
            _step(
                "sink",
                TTS_OP,
                depends_on=["a.b.c"],
                input={
                    "text": "${steps.a.b.c.output.transcript}",
                    "voice": "ann",
                },
            ),
        ],
    }
    result = provider.invoke("definition.validate", {"document": dotted_id})
    assert result["valid"], result["errors"]

    cyclic = {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "steps": [
            _step(
                "one",
                STT_OP,
                depends_on=["two"],
                input={"audio": "${steps.two.output.reply}"},
            ),
            _step(
                "two",
                LLM_OP,
                depends_on=["one"],
                input={"prompt": "${steps.one.output.transcript}"},
            ),
        ],
    }
    result = provider.invoke("definition.validate", {"document": cyclic})
    assert not result["valid"]
    assert any("cycle" in error for error in result["errors"])

    self_ref = chain_document()
    self_ref["steps"][0]["request"]["input"] = {
        "audio": "${steps.stt.output.transcript}"
    }
    self_ref["steps"][0]["depends_on"] = ["stt"]
    result = provider.invoke("definition.validate", {"document": self_ref})
    assert not result["valid"]
    assert any("dependencies" in error for error in result["errors"])


def test_wrong_type_and_absent_output_fail_closed(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, _catalog, _authority, invoker = runtime
    start_chain_run(provider, "run-typed")
    invoker.transcript_override = 42
    provider.invoke("run.step.execute", {"run_id": "run-typed", "step_id": "stt"})
    with pytest.raises(WorkflowValidationError):
        provider.invoke("run.step.execute", {"run_id": "run-typed", "step_id": "llm"})
    assert len(invoker.requests) == 1
    attempts = provider.invoke("run.get", {"run_id": "run-typed"})["attempts"]
    assert not [item for item in attempts if item["step_id"] == "llm"]

    missing_path = chain_document()
    missing_path["steps"][1]["request"]["input"] = {
        "prompt": "${steps.stt.output.missing}"
    }
    publish(provider, "workflow.run-missing", missing_path)
    provider.invoke(
        "run.create",
        {
            "definition_id": "workflow.run-missing",
            "run_id": "run-missing",
            "inputs": {"audio": "a", "voice": "v"},
        },
    )
    invoker.transcript_override = "hello world"
    provider.invoke("run.step.execute", {"run_id": "run-missing", "step_id": "stt"})
    with pytest.raises(WorkflowValidationError, match="output path is unresolved"):
        provider.invoke("run.step.execute", {"run_id": "run-missing", "step_id": "llm"})


def test_skipped_and_failed_upstream_outputs_are_rejected(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, _catalog, _authority, invoker = runtime
    document = chain_document()
    document["steps"][0]["when"] = "inputs.enabled == true"
    publish(provider, "workflow.run-skip", document)
    provider.invoke(
        "run.create",
        {
            "definition_id": "workflow.run-skip",
            "run_id": "run-skip",
            "inputs": {"enabled": False, "audio": "a", "voice": "v"},
        },
    )
    skipped = provider.invoke(
        "run.step.execute", {"run_id": "run-skip", "step_id": "stt"}
    )
    assert skipped["state"] == StepAttemptState.SUCCEEDED.value
    assert skipped["skipped"] is True
    with pytest.raises(WorkflowConflict, match="no succeeded, non-skipped"):
        provider.invoke("run.step.execute", {"run_id": "run-skip", "step_id": "llm"})
    assert not [item for item in invoker.requests if item["operation_id"] == "llm.run"]

    start_chain_run(provider, "run-failed")
    invoker.fail_operations.add("stt.run")
    failed = provider.invoke(
        "run.step.execute", {"run_id": "run-failed", "step_id": "stt"}
    )
    assert failed["state"] == StepAttemptState.FAILED.value
    # A terminally failed upstream step fails the run closed before any
    # bound downstream materialization or authority reservation.
    with pytest.raises(WorkflowConflict, match="cannot execute"):
        provider.invoke("run.step.execute", {"run_id": "run-failed", "step_id": "llm"})


def test_revoked_catalog_denies_bound_step_materialization(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, catalog, _authority, invoker = runtime
    start_chain_run(provider, "run-revoked")
    provider.invoke("run.step.execute", {"run_id": "run-revoked", "step_id": "stt"})
    catalog.value["operations"] = [
        item for item in catalog.value["operations"] if item["operation_id"] != "llm.run"
    ]
    with pytest.raises(WorkflowDenied, match="no longer active"):
        provider.invoke("run.step.execute", {"run_id": "run-revoked", "step_id": "llm"})
    assert len(invoker.requests) == 1


def test_retry_and_approval_pin_the_resolved_request(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    document = {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "max_concurrency": 1,
        "steps": [
            _step(
                "stt",
                STT_OP,
                input={"audio": "${inputs.audio}"},
                retry={"max_attempts": 3, "backoff_ms": 0},
            ),
            _step(
                "llm",
                LLM_OP,
                depends_on=["stt"],
                input={"prompt": "${steps.stt.output.transcript}"},
                retry={"max_attempts": 3, "backoff_ms": 0},
            ),
        ],
    }
    publish(provider, "workflow.run-retry-pin", document)
    provider.invoke(
        "run.create",
        {
            "definition_id": "workflow.run-retry-pin",
            "run_id": "run-retry-pin",
            "inputs": {"audio": "a"},
        },
    )
    provider.invoke("run.step.execute", {"run_id": "run-retry-pin", "step_id": "stt"})
    invoker.fail_operations.add("llm.run")
    first = provider.invoke(
        "run.step.execute", {"run_id": "run-retry-pin", "step_id": "llm"}
    )
    assert first["state"] == StepAttemptState.FAILED.value
    # A completed upstream step cannot be replayed to change its output.
    # The downstream retry must retain its first pinned resolution.
    invoker.transcript_override = lambda count: f"transcript-v{count}"
    with pytest.raises(WorkflowConflict, match="failed workflow step"):
        provider.invoke("run.step.execute", {"run_id": "run-retry-pin", "step_id": "stt"})
    invoker.fail_operations.discard("llm.run")
    retried = provider.invoke(
        "run.step.retry", {"run_id": "run-retry-pin", "step_id": "llm"}
    )
    assert retried["state"] == StepAttemptState.SUCCEEDED.value
    llm_inputs = [
        request["input"]
        for request in invoker.requests
        if request["operation_id"] == "llm.run"
    ]
    assert llm_inputs == [
        {"prompt": "hello world"},
        {"prompt": "hello world"},
    ]
    attempts = provider.invoke("run.get", {"run_id": "run-retry-pin"})["attempts"]
    bindings = [
        item.get("output_bindings") for item in attempts if item["step_id"] == "llm"
    ]
    assert bindings[0] == bindings[1]

    authority.state = ApprovalState.WAITING_APPROVAL
    publish(provider, "workflow.run-approval-pin", document)
    provider.invoke(
        "run.create",
        {
            "definition_id": "workflow.run-approval-pin",
            "run_id": "run-approval-pin",
            "inputs": {"audio": "a"},
        },
    )
    authority.state = ApprovalState.RESERVED
    provider.invoke(
        "run.step.execute", {"run_id": "run-approval-pin", "step_id": "stt"}
    )
    authority.state = ApprovalState.WAITING_APPROVAL
    waiting = provider.invoke(
        "run.step.execute", {"run_id": "run-approval-pin", "step_id": "llm"}
    )
    assert waiting["state"] == StepAttemptState.WAITING_APPROVAL.value
    authority.state = ApprovalState.RESERVED
    with pytest.raises(WorkflowConflict, match="failed workflow step"):
        provider.invoke(
            "run.step.execute", {"run_id": "run-approval-pin", "step_id": "stt"}
        )
    pending = authority.reservations[waiting["authority_reservation_id"]]
    authority.reservations[pending.reservation_id] = replace(
        pending, state=ApprovalState.APPROVED
    )
    resumed = provider.invoke(
        "run.step.execute", {"run_id": "run-approval-pin", "step_id": "llm"}
    )
    assert resumed["state"] == StepAttemptState.SUCCEEDED.value
    assert resumed["request"]["input"] == waiting["request"]["input"]


def test_outputs_never_cross_run_boundaries(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, _catalog, _authority, invoker = runtime
    publish(provider, "workflow.shared", chain_document())
    for run_id in ("run-one", "run-two"):
        provider.invoke(
            "run.create",
            {
                "definition_id": "workflow.shared",
                "run_id": run_id,
                "inputs": {"audio": "a", "voice": "v"},
            },
        )
    invoker.transcript_override = "first"
    provider.invoke("run.step.execute", {"run_id": "run-one", "step_id": "stt"})
    # run-two cannot see run-one's committed stt output at all.
    with pytest.raises(WorkflowConflict, match="dependencies"):
        provider.invoke("run.step.execute", {"run_id": "run-two", "step_id": "llm"})
    invoker.transcript_override = "second"
    provider.invoke("run.step.execute", {"run_id": "run-two", "step_id": "stt"})
    provider.invoke("run.step.execute", {"run_id": "run-one", "step_id": "llm"})
    provider.invoke("run.step.execute", {"run_id": "run-two", "step_id": "llm"})
    llm_prompts = [
        request["input"]["prompt"]
        for request in invoker.requests
        if request["operation_id"] == "llm.run"
    ]
    assert llm_prompts == ["first", "second"]


def test_palette_projects_bounded_display_ports(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, catalog, _authority, _invoker = runtime
    palette = provider.invoke("operation.palette", {})
    assert palette["catalog_digest"] == CATALOG_DIGEST
    assert palette["port_binding"] == {
        "reference_format": "${steps.<step_id>.output.<path>}",
        "whole_value_reference_format": "${steps.<step_id>.output}",
        "json_pointer_reference_format": "${steps.<step_id>.output@<json_pointer>}",
        "requires_depends_on": True,
    }
    llm = next(item for item in palette["operations"] if item["operation_id"] == "llm.run")
    assert llm["projection"] == "display-reduced"
    assert llm["output_schema_digest"] == canonical_digest(LLM_OUTPUT_SCHEMA)
    assert llm["schemas"]["input"] == LLM_INPUT_SCHEMA
    assert llm["schemas"]["output"] == LLM_OUTPUT_SCHEMA
    input_names = [port["name"] for port in llm["input_ports"]]
    assert input_names == sorted(input_names)
    prompt = next(port for port in llm["input_ports"] if port["name"] == "prompt")
    assert prompt == {"name": "prompt", "required": True, "schema": {"type": "string"}}
    model_port = next(
        port for port in llm["input_ports"] if port["name"] == "model_profile"
    )
    assert model_port["schema"]["x-tobkiri-selector"] == "model-profile"
    reply = next(port for port in llm["output_ports"] if port["name"] == "reply")
    assert reply["required"] is True

    deep = {"type": "object", "properties": {}}
    current = deep
    for _ in range(40):
        current["properties"] = {"nested": {"type": "object", "properties": {}}}
        current = current["properties"]["nested"]
    assert project_display_schema(deep) is None

    oversized = {
        "type": "object",
        "properties": {
            f"prop{i}": {"type": "string", "description": "x" * 1024}
            for i in range(64)
        },
    }
    assert project_display_schema(oversized) is None

    hinted = project_display_schema(
        {"type": "object", "properties": {"model": {"type": "object",
        "x-tobkiri-selector": "model-profile"}}}
    )
    assert hinted["properties"]["model"].get("x-tobkiri-selector") is None
    unknown = project_display_schema(
        {"type": "string", "x-tobkiri-selector": "steal-credentials"}
    )
    assert "x-tobkiri-selector" not in unknown
    listed = project_display_schema(
        {"type": "string", "x-tobkiri-selector": ["model-profile", "other"]}
    )
    assert listed["x-tobkiri-selector"] == ["model-profile"]
    assert project_ports({"type": "object"}) == [{"name": "$", "required": True, "schema": {"type": "object"}, "path": []}]
    ports = project_ports(catalog.value["schemas"][TTS_OP["input_schema_digest"]])
    assert [port["name"] for port in ports] == ["speed", "text", "voice"]


def test_display_projection_never_changes_captured_catalog_digest(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, catalog, _authority, _invoker = runtime
    snapshot = catalog.snapshot()
    palette = provider.invoke("operation.palette", {})
    assert palette["catalog_digest"] == snapshot["catalog_digest"]

    document = chain_document()
    preview = provider.invoke("definition.compile-preview", {"document": document})
    llm_step = next(item for item in preview["steps"] if item["step_id"] == "llm")
    assert llm_step["contract_request"]["input"] == {
        "prompt": "${steps.stt.output.transcript}"
    }
    assert preview["compile_digest"] == digest(
        {key: value for key, value in preview.items() if key != "compile_digest"}
    )

    class FakeRef:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeOperation:
        contract_id = "media.stt.v1"
        contract_version = "1.0.0"
        revision_digest = "sha256:" + "a" * 64
        operation_id = "stt.run"
        input_schema = STT_INPUT_SCHEMA
        output_schema = STT_OUTPUT_SCHEMA
        error_schema = None
        progress_schema = None

        def __init__(self) -> None:
            self.effect_class = FakeRef("capability:stt")

    class FakeFunction:
        def __init__(self, function_id: str) -> None:
            self.function_id = function_id

    class FakeBinding:
        def __init__(self) -> None:
            self.operation = FakeOperation()
            self.principal_ref = FakeRef("media.stt.provider")
            self.function = FakeFunction("media.stt")

    class FakeContext:
        security_epoch = 7

        def __init__(self) -> None:
            self.activation = {"activation_id": "activation-test"}
            self.catalog_bindings = (FakeBinding(),)

    resolved = _ResolvedCatalog(FakeContext())
    resolved_snapshot = resolved.snapshot()
    resolved_body = {
        "security_epoch": resolved_snapshot["security_epoch"],
        "activation": resolved_snapshot["activation"],
        "operations": resolved_snapshot["operations"],
    }
    assert canonical_digest(resolved_body) == resolved_snapshot["catalog_digest"]
    assert resolved_snapshot["schemas"][
        resolved_snapshot["operation_output_schemas"][0]["output_schema_digest"]
    ] == STT_OUTPUT_SCHEMA
    operation = resolved_snapshot["operations"][0]
    assert set(operation) == {
        "contract_id",
        "contract_revision_digest",
        "operation_id",
        "function_principal_id",
        "provider_id",
        "input_schema_digest",
        "effect_ceiling",
    }


def test_templates_stay_whole_string_and_inputs_semantics_unchanged() -> None:
    step_ids = {"stt"}
    committed = {
        "stt": {
            "attempt_id": "run:stt:1",
            "output": {"transcript": "hello"},
            "output_digest": "sha256:" + "7" * 64,
        }
    }
    resolved = resolve_templates(
        {"mixed": "say ${steps.stt.output.transcript} now"},
        inputs={},
        step_ids=step_ids,
        committed=committed,
    )
    assert resolved == {"mixed": "say ${steps.stt.output.transcript} now"}
    with pytest.raises(WorkflowValidationError, match="input template"):
        resolve_templates(
            {"x": "${inputs.missing}"},
            inputs={},
            step_ids=step_ids,
            committed=committed,
        )
    provenance: list[Mapping[str, Any]] = []
    resolved = resolve_templates(
        {"text": "${steps.stt.output.transcript}", "n": [1, "${steps.stt.output.transcript}"]},
        inputs={},
        step_ids=step_ids,
        committed=committed,
        provenance=provenance,
    )
    assert resolved == {"text": "hello", "n": [1, "hello"]}
    assert len(provenance) == 2
    with pytest.raises(WorkflowValidationError, match="malformed"):
        parse_step_reference("not-a-ref", step_ids)
    with pytest.raises(WorkflowValidationError, match="no declared step match"):
        parse_step_reference("${steps.stt.output..}", step_ids)


def test_published_runs_never_stale_from_projection_metadata(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, RoutingInvoker],
) -> None:
    provider, catalog, _authority, invoker = runtime
    start_chain_run(provider, "run-stable")
    provider.invoke("run.step.execute", {"run_id": "run-stable", "step_id": "stt"})
    schema_digest = canonical_digest(
        {"type": "object", "properties": {"extra": {"type": "boolean"}}}
    )
    catalog.value["schemas"][schema_digest] = {
        "type": "object",
        "properties": {"extra": {"type": "boolean"}},
    }
    assert catalog.snapshot()["catalog_digest"] == CATALOG_DIGEST
    attempt = provider.invoke(
        "run.step.execute", {"run_id": "run-stable", "step_id": "llm"}
    )
    assert attempt["state"] == StepAttemptState.SUCCEEDED.value
    assert invoker.requests[-1]["input"] == {"prompt": "hello world"}


def test_whole_object_binding_preserves_committed_provenance_and_rejects_ambiguity(runtime, tmp_path):
    provider, catalog, authority, invoker = runtime
    document = chain_document()
    document['steps'] = [document['steps'][0], document['steps'][1]]
    # A separately authored consumer accepts the complete producer object.
    schema = {'type': 'object', 'properties': {'transcript': {'type': 'string'}}, 'required': ['transcript']}
    input_digest = canonical_digest(schema)
    catalog.value['schemas'][input_digest] = schema
    for item in catalog.value['operations']:
        if item['operation_id'] == 'llm.run':
            item['input_schema_digest'] = input_digest
    document['steps'][1]['request']['input'] = '${steps.stt.output}'
    provider = WorkflowProviderV4(WorkflowEngineV4(
        store=WorkflowStoreV4(tmp_path / 'whole.sqlite3', clock=lambda: 100.0),
        catalog=catalog, authority=authority, invoker=invoker,
        validator=SchemaValidator(catalog.value['schemas']), clock=lambda: 100.0,
    ))
    publish(provider, 'whole.object', document)
    provider.invoke('run.create', {'definition_id': 'whole.object', 'run_id': 'whole-run', 'inputs': {'audio': 'fixture'}})
    original = invoker.invoke
    def accept(request, **kwargs):
        if request['operation_id'] == 'llm.run':
            invoker.requests.append(request)
            return InvocationOutcome(output={'reply': request['input']['transcript']})
        return original(request, **kwargs)
    invoker.invoke = accept
    provider.invoke('run.step.execute', {'run_id': 'whole-run', 'step_id': 'stt'})
    provider.invoke('run.step.execute', {'run_id': 'whole-run', 'step_id': 'llm'})
    assert invoker.requests[1]['input'] == {'transcript': 'hello world'}
    run = provider.invoke('run.get', {'run_id': 'whole-run'})
    assert next(a for a in run['attempts'] if a['step_id'] == 'llm')['output_bindings'][0]['path'] == ''
    assert authority.commit_count == 2
    assert parse_step_reference('${steps.stt.output}', {'stt'}) == ('stt', '')
    with pytest.raises(WorkflowValidationError, match='ambiguous'):
        parse_step_reference('${steps.a.output.output}', {'a', 'a.output'})
    assert project_ports({'type': 'object'})[0]['path'] == []
    assert project_ports({'type': 'object', 'additionalProperties': False}) == []


@pytest.mark.parametrize('pointer,expected', [
    ('/camelCase', 1), ('/a.b', 2), ('/x~1y', 3), ('/t~0u', 4),
    ('/日本語', 5), ('/items/0/Value', 6),
])
def test_json_pointer_values_are_literal_keys_not_code(pointer, expected):
    output = {'camelCase': 1, 'a.b': 2, 'x/y': 3, 't~u': 4, '日本語': 5, 'items': [{'Value': 6}]}
    reference = '${steps.source.output@' + pointer + '}'
    provenance = []
    assert resolve_templates(reference, inputs={}, step_ids={'source'}, committed={
        'source': {'output': output, 'attempt_id': 'one', 'output_digest': canonical_digest(output)},
    }, provenance=provenance) == expected
    assert provenance[0]['path'] == pointer
    assert parse_step_reference('${steps.a.output@}', {'a', 'a.output'}) == ('a', '')


@pytest.mark.parametrize('reference', [
    '${steps.source.output@not-pointer}', '${steps.source.output@/bad~2escape}',
    '${steps.missing.output@/text}', '${steps.source.output@/bad\nkey}',
])
def test_invalid_pointer_bindings_are_rejected(reference):
    with pytest.raises(WorkflowValidationError):
        parse_step_reference(reference, {'source'})
