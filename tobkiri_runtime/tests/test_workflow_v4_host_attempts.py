"""Actual Workflow factory and normal Broker/Authority attempt regressions.

The outer authenticated invocation is a trusted capture fixture. Child requests
use the production adapter, real AuthorityKernel/Store, real approval mediator,
and normal RequestBroker. These tests make no Native/GUI acceptance claim.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
import time
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityKernel, FunctionPrincipal
from core_runtime.authority.v4_kernel import AuthorityBinding
from core_runtime.host_contract import bind_host_contract
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from core_runtime.workflow_v4.attempt_port import (
    CapturedWorkflowAttemptRouteV4,
    LateBoundWorkflowAttemptPortV4,
    WorkflowAttemptServiceConfigV4,
)
from core_runtime.workflow_v4.attempt_service import HostWorkflowAttemptServiceV4
from core_runtime.workflow_v4.attempt_store import WorkflowAttemptStoreV4
from core_runtime.workflow_v4.integration import WorkflowHostProviderFactoryV4
from core_runtime.workflow_v4.models import ApprovalState, WorkflowDenied, digest
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import OperationRoute, ResolvedOperationBinding
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.models import (
    ContractOperation,
    EffectClass,
    FunctionArtifact,
    OpaqueAuthorityRef,
)
from tobkiri_host.ports import OpaqueInvocationLease

from tests import test_authority_v4_lifecycle as authority_fixtures
from tests.test_tobkiri_host_authority_v4_adapter import (
    _adapter,
    _binding,
    _broker,
    _context,
)
from tests.test_interactive_approval_v4 import _CONTRACT, _decision_command


@dataclass(frozen=True)
class _Capture(HostProviderCaptureContextV4):
    workflow_attempt_port: Any = None


class _Invocation:
    def __init__(self, fixture: _Fixture, operation: str = "run.advance") -> None:
        binding = fixture.bindings[operation]
        self.presentation_owner_principal_id = fixture.harness.caller.principal_id
        self.presentation_owner_session_id = "session-caller"
        self.live = True
        self.envelope = RequestEnvelope(
            context=fixture.context,
            target_principal=binding.principal_ref,
            target_domain=OpaqueAuthorityRef("domain.workflow"),
            contract_id="tobkiri.workflow.v4",
            contract_version="4.0.0",
            operation_id=operation,
            payload={"run_id": "run.review"},
            request_digest=digest({"operation": operation}),
            deadline_monotonic=time.monotonic() + 30,
            lease=OpaqueInvocationLease(b"trusted-host-parent-fixture"),
            idempotency_key=None,
        )

    def assert_current(self) -> None:
        if not self.live:
            raise WorkflowDenied("authenticated invocation expired")


class _Fixture:
    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "profile_grant"
    ) -> None:
        original = authority_fixtures._principal

        def principal(seed: str, *, operation: str = "invoke") -> FunctionPrincipal:
            value = original(seed, operation=operation)
            if seed == "a":
                return replace(
                    value, function_id="tobkiri.workflow.provider", operation_id="run.advance"
                )
            return value

        with monkeypatch.context() as patch:
            patch.setattr(authority_fixtures, "_principal", principal)
            self.harness = authority_fixtures._Harness(tmp_path / "authority")
        self.activation = {"activation_id": "activation-1", "security_epoch": 1}
        self.context = replace(_context(self.harness), activation_digest=digest(self.activation))
        fixture = self

        class Resolver:
            def resolve_authority_binding(
                self, *, context: Any, caller: Any, target: Any
            ) -> AuthorityBinding:
                del context
                assert caller == fixture.harness.caller
                assert target == fixture.harness.target
                return AuthorityBinding(
                    caller_effect_ceiling=fixture.harness.scope,
                    runtime_safety_ceiling=fixture.harness.scope,
                    profile_admin_ceiling=fixture.harness.scope,
                    profile_id=fixture.context.profile_id,
                    activation_id=fixture.context.activation_id,
                    activation_digest=fixture.context.activation_digest,
                    plan_digest=fixture.context.plan_digest,
                    profile_authority_digest=fixture.context.profile_authority_digest,
                    fencing_token=fixture.context.fencing_token,
                    security_epoch=1,
                )

        self.harness.kernel = AuthorityKernel(
            self.harness.store,
            Resolver(),
            clock=self.harness.clock,
            lease_ttl_seconds=10,
        )
        self.authority = _adapter(self.harness)
        self.broker = _broker(self.harness, self.authority, ProviderOutcome({"review": "reached"}))
        self.target = _binding(self.harness)
        self.bindings = {}
        for operation in (
            "definition.create",
            "definition.publish",
            "run.create",
            "run.get",
            "run.advance",
            "run.cancel",
            "run.step.resume",
        ):
            metadata = ContractOperation(
                contract_id="tobkiri.workflow.v4",
                contract_version="4.0.0",
                revision_digest=self.harness.caller.contract_revision_digest,
                operation_id=operation,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                effect_class=EffectClass.PRIVILEGED,
            )
            function = FunctionArtifact(
                function_id="tobkiri.workflow.provider",
                implementation_digest=self.harness.caller.function_implementation_digest,
                variant_id=self.target.variant.variant_id,
                operations=(metadata,),
            )
            caller = replace(self.harness.caller, operation_id=operation)
            artifact = replace(
                self.target.artifact,
                digest=self.harness.caller.parent_artifact_digest,
                publisher_lineage="publisher.caller",
                functions=(function,),
            )
            route = OperationRoute(
                contract_id=metadata.contract_id,
                operation_id=operation,
                artifact_digest=artifact.digest,
                function_id=function.function_id,
                variant_id=function.variant_id,
                execution_domain_profile="workflow.host",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(caller.principal_id),
            )
            self.bindings[operation] = ResolvedOperationBinding(
                artifact,
                function,
                self.target.variant,
                metadata,
                route,
                route.target_principal_ref,
            )
        selected = tuple(
            self.bindings[operation]
            for operation in (
                "run.advance",
                "run.cancel",
                "run.step.resume",
            )
        )
        self.config = WorkflowAttemptServiceConfigV4(
            broker=self.broker,
            authority=self.authority,
            approvals=self.authority,
            routes=(
                CapturedWorkflowAttemptRouteV4(
                    OpaqueAuthorityRef(self.harness.caller.principal_id),
                    self.target,
                    self.harness.scope,
                    mode,  # type: ignore[arg-type]
                ),
            ),
            context_for_attempt=lambda route, invocation, identity: self.context,
            execution_scope=lambda context, invocation: nullcontext(),
            presentation_owner_scope=lambda context, principal, session: nullcontext(),
            assert_current_capture=lambda: None,
            state_path=tmp_path / "private" / "attempts.bin",
            coordinator_bindings=selected,
            coordinator_publisher_lineage="publisher.caller",
            profile_id=self.context.profile_id,
            activation_id=self.context.activation_id,
            activation_digest=self.context.activation_digest,
            plan_digest=self.context.plan_digest,
            security_epoch=1,
            clock=self.harness.clock,
        )
        self.service = HostWorkflowAttemptServiceV4(self.config)
        self.port = LateBoundWorkflowAttemptPortV4()
        self.port.bind(self.service)
        capture = _Capture(
            profile_id=self.context.profile_id,
            plan_digest=self.context.plan_digest,
            security_epoch=1,
            activation=self.activation,
            state_root=tmp_path / "public",
            provider_bindings=tuple(self.bindings.values()),
            catalog_bindings=(self.target,),
            domain_ids={
                item.key
                if hasattr(item, "key")
                else (
                    item.operation.contract_id,
                    item.operation.operation_id,
                    item.principal_ref.value,
                ): "domain.workflow"
                for item in self.bindings.values()
            },
            workflow_attempt_port=self.port,
        )
        self.provider = WorkflowHostProviderFactoryV4().capture(capture)

    def call(
        self, operation: str, payload: dict[str, Any], invocation: _Invocation | None = None
    ) -> Any:
        contribution = next(
            item for item in self.provider.contributions if item.operation_id == operation
        )
        return contribution.invoke(operation, payload, invocation or _Invocation(self, operation))

    def prepare_run(self) -> None:
        document = {
            "workflow_api_version": "io.tobkiri.workflow.v4",
            "name": "Flow review",
            "steps": [
                {
                    "id": "review",
                    "request": {
                        "contract_id": self.target.operation.contract_id,
                        "contract_revision_digest": self.target.operation.revision_digest,
                        "operation_id": self.target.operation.operation_id,
                        "function_principal_id": self.target.principal_ref.value,
                        "input": {"message": "review-input-private"},
                    },
                }
            ],
        }
        definition = self.call(
            "definition.create", {"definition_id": "workflow.review", "document": document}
        )
        self.call(
            "definition.publish",
            {"definition_id": "workflow.review", "if_match": definition["etag"]},
        )
        self.call(
            "run.create", {"definition_id": "workflow.review", "inputs": {}, "run_id": "run.review"}
        )

    def close(self) -> None:
        self.provider.close()
        self.broker.close()
        self.harness.store.close()


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = _Fixture(tmp_path, monkeypatch)
    try:
        value.prepare_run()
        yield value
    finally:
        value.close()


def test_actual_factory_profile_grant_reaches_flow_review_with_real_broker(
    fixture: _Fixture,
) -> None:
    result = fixture.call("run.advance", {"run_id": "run.review"})
    assert result["run"]["state"] == "succeeded"
    assert result["attempts"][0]["outcome"] == {"review": "reached"}
    assert fixture.harness.store.list_interactive_approval_requests() == []
    assert [event["event_state"] for event in fixture.harness.store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]
    with pytest.raises(Exception):
        fixture.call("run.advance", {"run_id": "run.review"})
    assert fixture.harness.store.grant_usage(fixture.harness.grant.grant_id) == (0, 1)


def test_real_approval_wait_then_new_invocation_resumes_frozen_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture(tmp_path, monkeypatch, "interactive_only")
    try:
        fixture.prepare_run()
        first = _Invocation(fixture)
        waiting = fixture.call("run.advance", {"run_id": "run.review"}, first)
        assert waiting["run"]["state"] == "waiting_approval"
        first.live = False
        records = fixture.service._store.list_attempts()
        _, record = records[0]
        pending = fixture.service._store.get_host_pending_effect(record["effect_id"])
        approval_id = pending[1]["approval_request_id"]
        with bind_host_contract(_CONTRACT):
            command = _decision_command(fixture.harness, approval_id)
            fixture.authority.approve_interactive_approval(
                replace(command, context=fixture.context)
            )
        fresh = _Invocation(fixture)
        fresh.envelope = replace(
            fresh.envelope,
            context=replace(fresh.envelope.context, caller_session_id="fresh-outer-session"),
        )
        result = fixture.call("run.advance", {"run_id": "run.review"}, fresh)
        assert result["run"]["state"] == "succeeded"
        assert result["attempts"][0]["request"] == record["request"]
        assert result["attempts"][0]["outcome"] == {"review": "reached"}
        assert first.envelope.cancellation_requested.is_set() is False
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "profile-foreign"),
        ("plan_digest", digest({"old": "plan"})),
        ("security_epoch", 2),
        ("activation_id", "activation-old"),
    ],
)
def test_capture_identity_mismatch_never_creates_target_lease(
    fixture: _Fixture,
    field: str,
    value: Any,
) -> None:
    invocation = _Invocation(fixture)
    invocation.envelope = replace(
        invocation.envelope, context=replace(invocation.envelope.context, **{field: value})
    )
    with pytest.raises(WorkflowDenied):
        fixture.call("run.advance", {"run_id": "run.review"}, invocation)
    assert fixture.harness.store.grant_usage(fixture.harness.grant.grant_id) == (0, 0)


def test_expired_or_cancelled_fresh_parent_never_dispatches(fixture: _Fixture) -> None:
    invocation = _Invocation(fixture)
    invocation.envelope = replace(invocation.envelope, deadline_monotonic=time.monotonic() - 1)
    with pytest.raises(WorkflowDenied):
        fixture.call("run.advance", {"run_id": "run.review"}, invocation)
    assert fixture.harness.store.grant_usage(fixture.harness.grant.grant_id) == (0, 0)


def test_missing_signed_target_edge_is_unavailable(fixture: _Fixture) -> None:
    fixture.service._routes.clear()
    with pytest.raises(WorkflowDenied, match="signed outgoing edge"):
        fixture.call("run.advance", {"run_id": "run.review"})
    assert fixture.harness.store.grant_usage(fixture.harness.grant.grant_id) == (0, 0)


def test_profile_grant_revocation_before_reserve_is_not_auto_approved(fixture: _Fixture) -> None:
    fixture.harness.kernel.revoke(
        target_kind="grant", target_id=fixture.harness.grant.grant_id, reason="test revoke"
    )
    with pytest.raises(AuthorityDenied):
        fixture.call("run.advance", {"run_id": "run.review"})
    assert fixture.service._store.list_host_pending_effects() == []


def test_private_attempt_snapshots_are_encrypted_at_rest(fixture: _Fixture) -> None:
    fixture.call("run.advance", {"run_id": "run.review"})
    stored = fixture.config.state_path.read_bytes()
    assert b"review-input-private" not in stored
    assert b"session-caller" not in stored
    assert b"normalized_payload" not in stored


def test_cas_and_recovery_never_reconstruct_dispatch_token(fixture: _Fixture) -> None:
    fixture.call("run.advance", {"run_id": "run.review"})
    revision, record = fixture.service._store.list_attempts()[0]
    record["state"] = "claimed"
    fixture.service._store.cas(record["reservation_id"], revision, record)
    restarted = HostWorkflowAttemptServiceV4(fixture.config)
    assert (
        restarted.inspect(_Invocation(fixture), record["reservation_id"]).state
        is ApprovalState.REVOKED
    )
    assert restarted._store.get(record["reservation_id"])[1]["state"] == "ambiguous"
    with pytest.raises(WorkflowDenied):
        restarted.commit(
            _Invocation(fixture),
            record["reservation_id"],
            request_digest=record["request_digest"],
            security_epoch=1,
        )


def test_cancel_operation_can_fence_but_cannot_dispatch_another_callers_snapshot(
    fixture: _Fixture,
) -> None:
    fixture.call("run.advance", {"run_id": "run.review"})
    _, record = fixture.service._store.list_attempts()[0]
    cancel = _Invocation(fixture, "run.cancel")
    with pytest.raises(WorkflowDenied, match="caller operation"):
        fixture.service.inspect(cancel, record["reservation_id"])
    fixture.service.revoke(cancel, record["reservation_id"], reason="test cancel")
    assert fixture.harness.store.grant_usage(fixture.harness.grant.grant_id) == (0, 1)


def test_state_key_permissions_and_journal_tamper_fail_closed(fixture: _Fixture) -> None:
    key = fixture.config.state_path.with_name(fixture.config.state_path.name + ".key")
    key.chmod(0o644)
    with pytest.raises(WorkflowDenied, match="not private"):
        WorkflowAttemptStoreV4(fixture.config.state_path)
    key.chmod(0o600)
    fixture.config.state_path.write_bytes(b"tampered ciphertext")
    with pytest.raises(WorkflowDenied, match="unavailable"):
        WorkflowAttemptStoreV4(fixture.config.state_path)


def test_late_bound_port_without_service_stays_unavailable() -> None:
    with pytest.raises(WorkflowDenied, match="unavailable"):
        LateBoundWorkflowAttemptPortV4().inspect(None, "opaque")  # type: ignore[arg-type]
