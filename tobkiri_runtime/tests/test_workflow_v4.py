"""Focused conformance and security tests for Workflow v4."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from core_runtime.workflow_v4 import (
    ApprovalState,
    DefinitionState,
    RunState,
    StepAttemptState,
    WorkflowConflict,
    WorkflowDenied,
    WorkflowEngineV4,
    WorkflowProviderV4,
    WorkflowStoreV4,
    WorkflowValidationError,
)
from core_runtime.workflow_v4.models import (
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    digest,
)
from ecosystem.tobkiri_workflow_pack.generate_v4 import PACK_ROOT, generate
from tobkiri_protocol.validation import validate_document

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_BACKEND_ROOT = RUNTIME_ROOT / "core_runtime" / "workflow_v4"

CATALOG_REVISION = "sha256:" + "1" * 64
CATALOG_DIGEST = "sha256:" + "2" * 64
ACTIVATION_DIGEST = "sha256:" + "3" * 64
INPUT_SCHEMA_DIGEST = "sha256:" + "4" * 64


class Catalog:
    """Mutable captured catalog fixture."""

    def __init__(self) -> None:
        self.value = {
            "catalog_digest": CATALOG_DIGEST,
            "security_epoch": 7,
            "activation": {
                "activation_id": "activation-test",
                "activation_digest": ACTIVATION_DIGEST,
            },
            "operations": [
                {
                    "contract_id": "example.echo.v1",
                    "contract_revision_digest": CATALOG_REVISION,
                    "operation_id": "echo",
                    "function_principal_id": "example.echo.provider",
                    "provider_id": "example.echo",
                    "input_schema_digest": INPUT_SCHEMA_DIGEST,
                    "effect_ceiling": ["capability:echo"],
                }
            ],
        }

    def snapshot(self) -> Mapping[str, Any]:
        return self.value


class Validator:
    """Reject a deterministic invalid marker."""

    def validate(self, schema_digest: str, value: Mapping[str, Any]) -> list[str]:
        assert schema_digest == INPUT_SCHEMA_DIGEST
        return ["invalid input"] if value.get("invalid") else []


class Authority:
    """Authority fixture implementing reserve/inspect/commit fences."""

    def __init__(self, state: ApprovalState = ApprovalState.RESERVED) -> None:
        self.state = state
        self.reservations: dict[str, AuthorityReservation] = {}
        self.finished: list[tuple[str, str]] = []
        self.revoked: list[str] = []
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
        self.finished.append((reservation_id, state))

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        assert reason == "workflow_cancelled"
        self.revoked.append(reservation_id)


class Invoker:
    """Brokered invocation fixture."""

    def __init__(self, outcome: InvocationOutcome | None = None) -> None:
        self.outcome = outcome or InvocationOutcome(output={"ok": True})
        self.requests: list[Mapping[str, Any]] = []
        self.cancelled: list[str] = []

    def invoke(
        self, request: Mapping[str, Any], *, authority: DispatchAuthority
    ) -> InvocationOutcome:
        assert authority.dispatch_token.startswith("one-shot-")
        self.requests.append(request)
        return self.outcome

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[WorkflowProviderV4, Catalog, Authority, Invoker]:
    """Create an isolated local-first provider."""

    catalog = Catalog()
    authority = Authority()
    invoker = Invoker()
    engine = WorkflowEngineV4(
        store=WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0),
        catalog=catalog,
        authority=authority,
        invoker=invoker,
        validator=Validator(),
        clock=lambda: 100.0,
    )
    return WorkflowProviderV4(engine), catalog, authority, invoker


def definition(input_value: Any = "${inputs.message}") -> dict[str, Any]:
    """Build one exact Contract-bound Definition."""

    return {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "name": "Exact echo",
        "max_concurrency": 2,
        "steps": [
            {
                "id": "echo",
                "request": {
                    "contract_id": "example.echo.v1",
                    "contract_revision_digest": CATALOG_REVISION,
                    "operation_id": "echo",
                    "function_principal_id": "example.echo.provider",
                    "input": {"message": input_value},
                },
                "retry": {"max_attempts": 2, "backoff_ms": 0},
            }
        ],
    }


def publish(provider: WorkflowProviderV4) -> dict[str, Any]:
    """Create and publish a fixture Definition."""

    created = provider.invoke(
        "definition.create",
        {"definition_id": "workflow.echo", "document": definition()},
    )
    return provider.invoke(
        "definition.publish",
        {"definition_id": "workflow.echo", "if_match": created["etag"]},
    )


def test_definition_lifecycle_etag_digest_validate_compile_and_delete(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, _authority, _invoker = runtime
    validation = provider.invoke("definition.validate", {"document": definition()})
    assert validation == {"valid": True, "errors": []}
    preview = provider.invoke("definition.compile-preview", {"document": definition()})
    assert preview["catalog_digest"] == CATALOG_DIGEST
    assert preview["compile_digest"] == digest(
        {key: value for key, value in preview.items() if key != "compile_digest"}
    )
    created = provider.invoke(
        "definition.create",
        {"definition_id": "workflow.echo", "document": definition("one")},
    )
    assert created["state"] == DefinitionState.DRAFT.value
    with pytest.raises(WorkflowConflict, match="ETag"):
        provider.invoke(
            "definition.update",
            {
                "definition_id": "workflow.echo",
                "document": definition("two"),
                "if_match": '"stale"',
            },
        )
    updated = provider.invoke(
        "definition.update",
        {
            "definition_id": "workflow.echo",
            "document": definition("two"),
            "if_match": created["etag"],
        },
    )
    assert updated["revision"] == 2
    published = provider.invoke(
        "definition.publish",
        {"definition_id": "workflow.echo", "if_match": updated["etag"]},
    )
    archived = provider.invoke(
        "definition.archive",
        {"definition_id": "workflow.echo", "if_match": published["etag"]},
    )
    assert archived["state"] == DefinitionState.ARCHIVED.value
    draft = provider.invoke(
        "definition.create",
        {"definition_id": "workflow.delete", "document": definition()},
    )
    assert provider.invoke(
        "definition.delete",
        {"definition_id": "workflow.delete", "if_match": draft["etag"]},
    ) == {"deleted": True}


def test_palette_is_exact_catalog_and_rejects_unpinned_operation(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, _authority, _invoker = runtime
    palette = provider.invoke("operation.palette", {})
    assert palette["operations"] == [
        {
            "contract_id": "example.echo.v1",
            "contract_revision_digest": CATALOG_REVISION,
            "operation_id": "echo",
            "function_principal_id": "example.echo.provider",
            "provider_id": "example.echo",
            "input_schema_digest": INPUT_SCHEMA_DIGEST,
            "effect_ceiling": ["capability:echo"],
        }
    ]
    altered = definition()
    altered["steps"][0]["request"]["function_principal_id"] = "legacy.registry"
    result = provider.invoke("definition.validate", {"document": altered})
    assert not result["valid"]
    assert "exact active catalog operation" in result["errors"][0]


def test_palette_rejects_conflicting_effects_for_same_operation(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, catalog, _authority, _invoker = runtime
    catalog.value["operations"].append(
        {**catalog.value["operations"][0], "effect_ceiling": ["capability:write"]}
    )
    with pytest.raises(WorkflowValidationError, match="duplicate operation identity"):
        provider.invoke("operation.palette", {})


def test_run_pins_revision_activation_and_commits_atomic_authority(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    published = publish(provider)
    run = provider.invoke(
        "run.create",
        {
            "definition_id": "workflow.echo",
            "run_id": "run-one",
            "occurrence_id": "occurrence-one",
            "inputs": {"message": "hello"},
        },
    )
    assert run["revision_digest"] == published["revision_digest"]
    assert run["activation_digest"] == ACTIVATION_DIGEST
    attempt = provider.invoke("run.step.execute", {"run_id": "run-one", "step_id": "echo"})
    assert attempt["state"] == StepAttemptState.SUCCEEDED.value
    assert invoker.requests[0]["input"] == {"message": "hello"}
    assert authority.commit_count == 1
    assert authority.finished == [("reservation-1", "succeeded")]
    assert (
        provider.invoke("run.get", {"run_id": "run-one"})["run"]["state"]
        == RunState.SUCCEEDED.value
    )
    with pytest.raises(WorkflowConflict, match="occurrence"):
        provider.invoke(
            "run.create",
            {
                "definition_id": "workflow.echo",
                "run_id": "run-replay",
                "occurrence_id": "occurrence-one",
                "inputs": {"message": "replay"},
            },
        )


def test_approval_wait_resume_denial_and_timeout_fail_closed(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    authority.state = ApprovalState.WAITING_APPROVAL
    publish(provider)
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "run-wait", "inputs": {"message": "x"}},
    )
    waiting = provider.invoke("run.step.execute", {"run_id": "run-wait", "step_id": "echo"})
    assert waiting["state"] == StepAttemptState.WAITING_APPROVAL.value
    assert not invoker.requests
    reservation = authority.reservations["reservation-1"]
    authority.reservations["reservation-1"] = replace(reservation, state=ApprovalState.APPROVED)
    resumed = provider.invoke("run.step.execute", {"run_id": "run-wait", "step_id": "echo"})
    assert resumed["state"] == StepAttemptState.SUCCEEDED.value

    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "run-deny", "inputs": {"message": "x"}},
    )
    authority.state = ApprovalState.DENIED
    denied = provider.invoke("run.step.execute", {"run_id": "run-deny", "step_id": "echo"})
    assert denied["error_code"] == "approval_denied"
    assert len(invoker.requests) == 1

    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "run-timeout", "inputs": {"message": "x"}},
    )
    authority.state = ApprovalState.WAITING_APPROVAL
    provider.invoke("run.step.execute", {"run_id": "run-timeout", "step_id": "echo"})
    last_id = f"reservation-{len(authority.reservations)}"
    authority.reservations[last_id] = replace(
        authority.reservations[last_id], state=ApprovalState.EXPIRED, expires_at=99.0
    )
    timed_out = provider.invoke("run.step.execute", {"run_id": "run-timeout", "step_id": "echo"})
    assert timed_out["state"] == RunState.TIMED_OUT.value


def test_retry_ambiguous_cancel_checkpoint_and_recovery(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    publish(provider)
    invoker.outcome = InvocationOutcome(error_code="retryable")
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "run-retry", "inputs": {"message": "x"}},
    )
    first = provider.invoke("run.step.execute", {"run_id": "run-retry", "step_id": "echo"})
    assert first["state"] == StepAttemptState.FAILED.value
    invoker.outcome = InvocationOutcome(output={"ok": True})
    second = provider.invoke("run.step.execute", {"run_id": "run-retry", "step_id": "echo"})
    assert second["attempt_number"] == 2

    invoker.outcome = InvocationOutcome(ambiguous_effect=True)
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "run-ambiguous", "inputs": {"message": "x"}},
    )
    state = provider.invoke("run.step.execute", {"run_id": "run-ambiguous", "step_id": "echo"})
    assert state["state"] == RunState.NEEDS_RECONCILIATION.value

    authority.state = ApprovalState.WAITING_APPROVAL
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "run-cancel", "inputs": {"message": "x"}},
    )
    waiting = provider.invoke("run.step.execute", {"run_id": "run-cancel", "step_id": "echo"})
    checkpoint = provider._engine.store.latest_checkpoint(waiting["attempt_id"])
    assert checkpoint is not None
    assert not {"dispatch_token", "invocation_lease", "credential", "host_handle"}.intersection(
        checkpoint
    )
    cancelled = provider.invoke("run.cancel", {"run_id": "run-cancel"})
    assert cancelled["state"] == RunState.CANCELLED.value
    assert authority.revoked


def test_stale_catalog_tampered_authority_and_store_records_fail_closed(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, catalog, authority, invoker = runtime
    publish(provider)
    catalog.value["catalog_digest"] = "sha256:" + "9" * 64
    with pytest.raises(WorkflowDenied, match="catalog is stale"):
        provider.invoke(
            "run.create",
            {"definition_id": "workflow.echo", "run_id": "stale", "inputs": {"message": "x"}},
        )
    catalog.value["catalog_digest"] = CATALOG_DIGEST
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.echo", "run_id": "tampered", "inputs": {"message": "x"}},
    )
    original_commit = authority.commit

    def altered_commit(*args: Any, **kwargs: Any) -> DispatchAuthority:
        result = original_commit(*args, **kwargs)
        return replace(result, request_digest="sha256:" + "0" * 64)

    authority.commit = altered_commit  # type: ignore[method-assign]
    with pytest.raises(WorkflowDenied, match="altered"):
        provider.invoke("run.step.execute", {"run_id": "tampered", "step_id": "echo"})
    assert not invoker.requests

    store = provider._engine.store
    store._connection.execute("UPDATE workflow_runs SET payload='{}' WHERE run_id='tampered'")
    with pytest.raises(WorkflowDenied, match="authentication"):
        store.get_run("tampered")


def test_pack_artifacts_are_deterministic_valid_and_have_no_legacy_dispatch() -> None:
    assert generate(check=True) == {"packs": 1, "contracts": 1, "operations": 20}
    pack = validate_document((PACK_ROOT / "pack.v4.json").read_bytes(), "pack")
    contracts = validate_document(
        (PACK_ROOT / "contracts.v4.json").read_bytes(), "pack_contract_catalog"
    )
    executables = validate_document(
        (PACK_ROOT / "executables.v4.json").read_bytes(), "executable_catalog"
    )
    validate_document((PACK_ROOT / "artifact-index.v4.json").read_bytes(), "pack_artifact_index")
    frontend = json.loads((PACK_ROOT / "frontend_contract_map.v4.json").read_text(encoding="utf-8"))
    frontend_operations = {
        target["operation_id"] for route in frontend["routes"] for target in route["targets"]
    }
    assert frontend_operations == set(pack["functions"][0]["operations"])
    assert {
        "definition.create",
        "definition.update",
        "definition.delete",
        "definition.validate",
        "definition.compile-preview",
        "run.create",
        "run.step.retry",
        "run.step.resume",
        "run.cancel",
    }.issubset(frontend_operations)
    integrity = json.loads((PACK_ROOT / "backend-integrity.v4.json").read_text(encoding="utf-8"))
    assert set(integrity["files"]) == {
        path.relative_to(RUNTIME_ROOT).as_posix()
        for path in WORKFLOW_BACKEND_ROOT.glob("*.py")
    }
    assert pack["migration"] == {
        "compatibility": "none",
        "legacy_ids": [],
        "removal_wave": 13,
        "sunset_at": "2026-08-10",
    }
    operations = tuple(item["operation_id"] for item in contracts["contracts"][0]["operations"])
    assert operations == tuple(pack["functions"][0]["operations"])
    assert operations == tuple(
        item["operation_id"] for item in executables["variants"][0]["operations"]
    )
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in WORKFLOW_BACKEND_ROOT.glob("*.py")
    )
    for forbidden in (
        "FunctionRegistry",
        "InterfaceRegistry",
        "flow_scheduler",
        "pack_api_server",
        "requests.post",
        "urllib.request",
    ):
        assert forbidden not in sources


def test_state_catalogs_are_complete() -> None:
    assert {item.value for item in DefinitionState} == {"draft", "published", "archived"}
    assert {item.value for item in RunState} == {
        "queued",
        "running",
        "paused",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
        "needs_reconciliation",
    }
    assert {item.value for item in StepAttemptState} == {
        "pending",
        "dispatching",
        "running",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
        "ambiguous_effect",
    }


def test_run_advance_executes_dependency_ready_steps_with_bound(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    document = definition("one")
    second = json.loads(json.dumps(document["steps"][0]))
    second["id"] = "echo-two"
    second["request"]["input"] = {"message": "two"}
    document["steps"].append(second)
    created = provider.invoke(
        "definition.create",
        {"definition_id": "workflow.parallel", "document": document},
    )
    provider.invoke(
        "definition.publish",
        {"definition_id": "workflow.parallel", "if_match": created["etag"]},
    )
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.parallel", "run_id": "run-parallel", "inputs": {}},
    )
    advanced = provider.invoke("run.advance", {"run_id": "run-parallel"})
    assert advanced["run"]["state"] == RunState.SUCCEEDED.value
    assert len(advanced["attempts"]) == 2
    assert authority.commit_count == 2
    assert {request["input"]["message"] for request in invoker.requests} == {"one", "two"}


def test_dependencies_and_restricted_condition_are_enforced_without_authority(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, authority, invoker = runtime
    document = definition("one")
    second = json.loads(json.dumps(document["steps"][0]))
    second["id"] = "echo-two"
    second["depends_on"] = ["echo"]
    document["steps"].append(second)
    created = provider.invoke(
        "definition.create",
        {"definition_id": "workflow.dependencies", "document": document},
    )
    provider.invoke(
        "definition.publish",
        {"definition_id": "workflow.dependencies", "if_match": created["etag"]},
    )
    provider.invoke(
        "run.create",
        {"definition_id": "workflow.dependencies", "run_id": "run-deps", "inputs": {}},
    )
    with pytest.raises(WorkflowConflict, match="dependencies"):
        provider.invoke("run.step.execute", {"run_id": "run-deps", "step_id": "echo-two"})

    conditional = definition("never")
    conditional["steps"][0]["when"] = "inputs.enabled == true"
    created = provider.invoke(
        "definition.create",
        {"definition_id": "workflow.condition", "document": conditional},
    )
    provider.invoke(
        "definition.publish",
        {"definition_id": "workflow.condition", "if_match": created["etag"]},
    )
    provider.invoke(
        "run.create",
        {
            "definition_id": "workflow.condition",
            "run_id": "run-condition",
            "inputs": {"enabled": False},
        },
    )
    skipped = provider.invoke("run.step.execute", {"run_id": "run-condition", "step_id": "echo"})
    assert skipped["state"] == StepAttemptState.SUCCEEDED.value
    assert skipped["skipped"] is True
    assert authority.commit_count == 0
    assert invoker.requests == []


def test_unknown_operation_and_restricted_expression_are_rejected(
    runtime: tuple[WorkflowProviderV4, Catalog, Authority, Invoker],
) -> None:
    provider, _catalog, _authority, _invoker = runtime
    with pytest.raises(WorkflowValidationError, match="unknown"):
        provider.invoke("legacy.flow.write", {})
    invalid = definition()
    invalid["steps"][0]["when"] = "http.get('https://example.com')"
    result = provider.invoke("definition.validate", {"document": invalid})
    assert not result["valid"]
    assert "restricted CEL subset" in result["errors"][0]
