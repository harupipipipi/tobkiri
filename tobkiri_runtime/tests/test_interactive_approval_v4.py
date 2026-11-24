"""Focused adversarial coverage for the Host-owned v4 approval port."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from typing import Mapping

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityMode,
    AuthorityScope,
    AuthorityValidationError,
    DomainBoundary,
    GrantLifetime,
    GrantRecord,
    InteractiveApprovalDecision,
    ProviderAuthorityRecord,
)
from core_runtime.host_contract import bind_host_contract
from tests.conformance_support.host_contract import host_contract
from tests.test_authority_v4_lifecycle import _Harness, _digest, _domain, _principal
from tests.test_tobkiri_host_authority_v4_adapter import (
    _Admission,
    _NoAdapters,
    _Principals,
    _context,
)
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.broker import RequestBroker
from tobkiri_host.contracts import AdapterPlanner, OperationCatalog, OperationRoute
from tobkiri_host.effects import InMemoryReconciliationStore, ProviderOutcome
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.interactive_effects import (
    PendingEffectController,
    PendingEffectState,
    PendingEffectStatus,
)
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    EffectClass,
    ExecutionKind,
    FunctionArtifact,
    InvocationFrame,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
    RequestContext,
    RuntimeEvidence,
)
from tobkiri_host.ports import (
    FinalAuthorizationQuery,
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGetQuery,
    InteractiveApprovalGrantAttestation,
    InteractiveApprovalListQuery,
    InteractiveApprovalRequestCommand,
    StaticAuthorityQuery,
)

_CONTRACT = host_contract(
    profile_id="profile-1",
    values={"panel_bootstrap_secret": "interactive-approval-test-secret-" + "x" * 32},
)


def _adapter(harness: _Harness) -> AuthorityV4Adapter:
    return AuthorityV4Adapter(
        harness.kernel,
        _Principals(harness.caller, harness.target),
    )


def _scope(
    harness: _Harness,
    *,
    request_digest: str,
    invocation_owner_id: str = "owner-1",
) -> dict[str, object]:
    """Build a scope with the exact bindings required for an approval."""

    value = harness.scope.to_dict()
    value["dimensions"] = {
        **value["dimensions"],
        "invocation_owner_id": [invocation_owner_id],
        "caller_session_id": ["session-caller"],
        "plan_digest": [_digest("plan")],
    }
    value["exact_request_digest"] = request_digest
    value["opaque"] = True
    return value


def _request_command(
    harness: _Harness,
    *,
    request_id: str = "interactive-request-1",
    request_digest: str | None = None,
    phrase: str | None = "APPROVE",
    scope: dict[str, object] | None = None,
) -> InteractiveApprovalRequestCommand:
    digest = request_digest or _digest(request_id)
    return InteractiveApprovalRequestCommand(
        context=_context(harness, request_id=request_id),
        target_principal=OpaqueAuthorityRef(harness.target.principal_id),
        request_digest=digest,
        base_scope=scope or _scope(harness, request_digest=digest),
        invocation_owner_id="owner-1",
        presentation_owner_principal_id=harness.caller.principal_id,
        presentation_owner_session_id="session-caller",
        caller_publisher_lineage="publisher.caller",
        target_publisher_lineage="publisher.target",
        expires_at=harness.clock() + 60,
        redacted_metadata={
            "action": "restart",
            "summary": "Restart local host",
            **({"confirmation_phrase": phrase} if phrase is not None else {}),
        },
        typed_confirmation_phrase=phrase,
    )


def _wire_digest(value: str) -> str:
    """Return the exact untagged digest required by native UI-operator v3."""

    assert value.startswith("sha256:")
    return value.removeprefix("sha256:")


def _operator(
    harness: _Harness,
    request_id: str,
    *,
    action: str,
    nonce: str,
    request_snapshot_digest: str | None = None,
    typed_confirmation_digest: str | None = None,
) -> dict[str, object]:
    request, _state = harness.kernel.interactive_approval(request_id)
    expected_confirmation_digest = (
        _wire_digest(request.typed_confirmation_digest)
        if action == "approve" and request.typed_confirmation_digest is not None
        else None
    )
    return sign_ui_operator(
        request_id,
        nonce=nonce,
        decision=action,
        request_snapshot_digest=request_snapshot_digest or _wire_digest(request.digest),
        typed_confirmation_digest=(
            expected_confirmation_digest
            if typed_confirmation_digest is None
            else typed_confirmation_digest
        ),
    )


def _decision_command(
    harness: _Harness,
    request_id: str,
    *,
    phrase: str = "APPROVE",
    nonce: str = "interactive-operator-nonce",
    action: str = "approve",
    request_snapshot_digest: str | None = None,
    typed_confirmation_digest: str | None = None,
) -> InteractiveApprovalDecisionCommand:
    return InteractiveApprovalDecisionCommand(
        context=_context(harness, request_id=request_id),
        request_id=request_id,
        actor_id="user-1",
        confirmation_text=phrase,
        ui_operator=_operator(
            harness,
            request_id,
            action=action,
            nonce=nonce,
            request_snapshot_digest=request_snapshot_digest,
            typed_confirmation_digest=typed_confirmation_digest,
        ),
    )


def test_approve_mints_only_one_shot_authority_and_returns_no_material(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        approved = adapter.approve_interactive_approval(
            _decision_command(harness, pending.request_id)
        )

    assert pending.state == "pending"
    assert approved.state == "approved"
    assert approved.typed_confirmation_required is True
    request, _state = harness.kernel.interactive_approval(pending.request_id)
    assert asdict(approved) == {
        "request_id": "interactive-request-1",
        "state": "approved",
        "expires_at": 1060.0,
        "typed_confirmation_required": True,
        "request_snapshot_digest": _wire_digest(request.digest),
        "typed_confirmation_digest": _wire_digest(
            str(request.typed_confirmation_digest)
        ),
        "redacted_metadata": {
            "action": "restart",
            "confirmation_phrase": "APPROVE",
            "summary": "Restart local host",
        },
        "target_principal_id": harness.target.principal_id,
        "base_scope": request.base_scope.to_dict(),
        "max_uses": 1,
        "remaining_uses": 1,
    }
    decision = harness.store.get_interactive_approval_decision(pending.request_id)
    assert decision is not None
    assert decision.typed_confirmation_verified is True
    approval = harness.store.get_approval(str(decision.approval_id))
    grant = harness.store.get_grant(str(decision.grant_id))
    assert approval is not None and grant is not None
    assert grant.lifetime.value == "one_shot"
    assert grant.max_uses == 1
    assert grant.session_id == "session-caller"
    assert grant.scope.exact_request_digest == _digest("interactive-request-1")


def test_approve_rejects_wrong_phrase_even_though_decision_has_no_boolean_input(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        with pytest.raises(AuthorityDenied, match="typed confirmation"):
            adapter.approve_interactive_approval(
                _decision_command(harness, pending.request_id, phrase="approve")
            )

    assert adapter.interactive_approval_status(pending.request_id).state == "pending"
    assert harness.store.get_interactive_approval_decision(pending.request_id) is None


@pytest.mark.parametrize(
    (
        "operator_action",
        "request_snapshot_digest",
        "typed_confirmation_digest",
        "match",
    ),
    [
        ("deny", None, None, "decision mismatch"),
        ("approve", "0" * 64, None, "request snapshot mismatch"),
        ("approve", None, "f" * 64, "confirmation mismatch"),
    ],
)
def test_approve_rejects_each_tampered_v3_ui_operator_binding(
    tmp_path,
    operator_action: str,
    request_snapshot_digest: str | None,
    typed_confirmation_digest: str | None,
    match: str,
) -> None:
    """The signed native proof cannot be replayed into a different decision."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        with pytest.raises(AuthorityDenied, match=match):
            adapter.approve_interactive_approval(
                _decision_command(
                    harness,
                    pending.request_id,
                    action=operator_action,
                    request_snapshot_digest=request_snapshot_digest,
                    typed_confirmation_digest=typed_confirmation_digest,
                )
            )

    assert adapter.interactive_approval_status(pending.request_id).state == "pending"
    assert harness.store.get_interactive_approval_decision(pending.request_id) is None


def test_failed_confirmation_does_not_consume_proof_but_settlement_does(
    tmp_path,
) -> None:
    """A transient local failure may retry, while a settled request rejects reuse."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        command = _decision_command(
            harness,
            pending.request_id,
            phrase="wrong phrase",
            nonce="interactive-retry-proof",
        )
        with pytest.raises(AuthorityDenied, match="typed confirmation"):
            adapter.approve_interactive_approval(command)

        approved = adapter.approve_interactive_approval(
            replace(command, confirmation_text="APPROVE")
        )
        assert approved.state == "approved"
        with pytest.raises(AuthorityDenied, match="unavailable"):
            adapter.approve_interactive_approval(command)


def test_typed_confirmation_requires_the_exact_display_phrase_in_metadata(
    tmp_path,
) -> None:
    """The UI may display the phrase only when it matches the stored digest."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    command = replace(
        _request_command(harness),
        redacted_metadata={"action": "restart", "summary": "Restart local host"},
    )
    with bind_host_contract(_CONTRACT):
        with pytest.raises(AuthorityDenied, match="confirmation display"):
            adapter.request_interactive_approval(command)

    assert (
        harness.store.get_interactive_approval_request(command.context.request_id)
        is None
    )


def test_authenticated_owner_queries_are_redacted_and_foreign_access_fails(
    tmp_path,
) -> None:
    """Presentation reads bind the durable owner principal and session exactly."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        query = InteractiveApprovalGetQuery(
            context=_context(harness, request_id=pending.request_id),
            request_id=pending.request_id,
        )
        assert adapter.get_interactive_approval(query).state == "pending"
        listed = adapter.list_interactive_approvals(
            InteractiveApprovalListQuery(context=query.context, state="pending")
        )

    assert [status.request_id for status in listed] == [pending.request_id]
    foreign = replace(
        query.context,
        caller_principal=OpaqueAuthorityRef(harness.target.principal_id),
    )
    with pytest.raises(AuthorityDenied, match="unavailable"):
        adapter.get_interactive_approval(
            InteractiveApprovalGetQuery(context=foreign, request_id=pending.request_id)
        )


def test_kernel_does_not_trust_typed_confirmation_boolean(tmp_path) -> None:
    """A forged audit assertion cannot replace the actual confirmation phrase."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    pending = adapter.request_interactive_approval(_request_command(harness))
    request, _state = harness.kernel.interactive_approval(pending.request_id)
    decided_at = harness.clock()
    decision = InteractiveApprovalDecision(
        decision_id=request.request_id,
        request_id=request.request_id,
        request_snapshot_digest=request.digest,
        decision="approved",
        actor_id="user-1",
        decided_at=decided_at,
        security_epoch=request.security_epoch,
        ui_operator_digest=_digest("forged-ui-proof"),
        typed_confirmation_verified=True,
        approval_id="interactive-approval-forged",
        grant_id="interactive-grant-forged",
    )
    approval = ApprovalRecord(
        approval_id="interactive-approval-forged",
        snapshot_digest=request.digest,
        actor_id="user-1",
        decision="approved",
        decided_at=decided_at,
        caller=request.caller,
        target=request.target,
        profile_id=request.profile_id,
        effect_bundle_digest=request.base_scope.digest,
        security_epoch=request.security_epoch,
    )
    grant = GrantRecord(
        grant_id="interactive-grant-forged",
        caller=request.caller,
        target=request.target,
        profile_id=request.profile_id,
        activation_id=request.activation_id,
        profile_authority_digest=request.profile_authority_digest,
        caller_publisher_lineage=request.caller_publisher_lineage,
        target_publisher_lineage=request.target_publisher_lineage,
        scope=request.base_scope,
        lifetime=GrantLifetime.ONE_SHOT,
        security_epoch=request.security_epoch,
        approval_id=approval.approval_id,
        issued_at=decided_at,
        expires_at=request.expires_at,
        max_uses=1,
        session_id=request.caller_session_id,
    )

    with pytest.raises(AuthorityDenied, match="typed confirmation"):
        harness.kernel.settle_interactive_approval(
            decision,
            approval=approval,
            grant=grant,
            confirmation_text=None,
        )
    assert harness.store.get_interactive_approval_decision(request.request_id) is None


def test_request_rejects_scope_without_exact_owner_session_and_plan_bindings(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    digest = _digest("interactive-bad-scope")
    scope = _scope(harness, request_digest=digest)
    del scope["dimensions"]["plan_digest"]  # type: ignore[index]

    with pytest.raises(AuthorityDenied, match="plan_digest"):
        adapter.request_interactive_approval(
            _request_command(
                harness,
                request_id="interactive-bad-scope",
                request_digest=digest,
                scope=scope,
            )
        )


def test_settlement_reresolves_session_domain_before_minting_authority(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        harness.store.transition_domain(
            harness.caller_domain.domain_id,
            expected_boot_epoch=harness.caller_domain.boot_epoch,
            expected_state=harness.caller_domain.state,
            new_state=type(harness.caller_domain.state).DRAINING,
        )
        with pytest.raises(AuthorityDenied, match="unavailable"):
            adapter.approve_interactive_approval(
                _decision_command(harness, pending.request_id)
            )

    assert harness.store.get_interactive_approval_decision(pending.request_id) is None


def test_approve_and_deny_race_has_exactly_one_durable_winner(tmp_path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    request = _request_command(
        harness,
        request_id="interactive-race-1",
        phrase=None,
    )
    with bind_host_contract(_CONTRACT):
        adapter.request_interactive_approval(request)

    def settle(action: str) -> str:
        with bind_host_contract(_CONTRACT):
            command = _decision_command(
                harness,
                request.context.request_id,
                nonce=f"interactive-race-{action}",
                action=action,
            )
            try:
                if action == "approve":
                    return adapter.approve_interactive_approval(command).state
                return adapter.deny_interactive_approval(command).state
            except AuthorityDenied:
                return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(settle, ("approve", "deny")))

    assert outcomes.count("lost") == 1
    assert {outcome for outcome in outcomes if outcome != "lost"} <= {
        "approved",
        "denied",
    }
    stored = harness.store.get_interactive_approval_decision(request.context.request_id)
    assert stored is not None


def test_generic_record_insertion_cannot_bypass_interactive_state_machine(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    pending = adapter.request_interactive_approval(_request_command(harness))
    request = harness.store.get_interactive_approval_request(pending.request_id)
    assert request is not None

    with pytest.raises(ValueError, match="dedicated state machine"):
        harness.store.put_record(request)
    with pytest.raises(ValueError, match="dedicated state machine"):
        harness.store.put_records_atomically((request,))
    assert isinstance(
        AuthorityScope.from_dict(request.base_scope.to_dict()), AuthorityScope
    )


# ---------------------------------------------------------------------------
# Settlement boundary proofs over the real kernel, store, Host adapter,
# PendingEffectController, and RequestBroker.  The only test double is the
# Provider transport counter, so every gate before Provider entry is the
# production code path the approval window ultimately relies on.
# ---------------------------------------------------------------------------


class _InteractiveEdge:
    """A production-shaped ``interactive_only`` edge carrying no caller Grant.

    Production capture persists only Provider reachability for these edges;
    the sole caller Grant is minted atomically when a human decision settles.
    """

    def __init__(self, harness: _Harness) -> None:
        self.coordinator = _principal("interactive-coordinator")
        self.execute_target = _principal("interactive-execute")
        self.coordinator_domain = _domain("coordinator", self.coordinator)
        self.execute_domain = _domain(
            "execute",
            self.execute_target,
            boundary=DomainBoundary.DEDICATED_PROCESS,
        )
        harness.kernel.register_execution_domain(
            self.coordinator_domain,
            session_id="session-coordinator",
            channel_digest=self.coordinator_domain.authenticated_channel_digest,
            principal=self.coordinator,
        )
        harness.kernel.register_execution_domain(
            self.execute_domain,
            session_id="session-execute",
            channel_digest=self.execute_domain.authenticated_channel_digest,
            principal=self.execute_target,
        )
        harness.kernel.commit_provider_authority_bundle(
            provider_authorities=(
                ProviderAuthorityRecord(
                    record_id="provider-authority-interactive-execute",
                    provider=self.execute_target,
                    execution_domain_id=self.execute_domain.domain_id,
                    execution_domain_identity_digest=(
                        self.execute_domain.identity_digest
                    ),
                    scope=harness.scope,
                    authority_mode=AuthorityMode.LEASE_ONLY,
                    security_epoch=1,
                    trust_provenance_digest=_digest("interactive-edge"),
                    publisher_lineage="publisher.execute-target",
                    host_extension_id="runtime-tcb",
                    valid_from=harness.clock(),
                    host_broker_binding="tobkiri.request-broker.v4",
                ),
            ),
        )


class _ExecuteBackend:
    """Counting Provider transport bound to the interactive execute domain."""

    def __init__(self, edge: _InteractiveEdge) -> None:
        self.status = BackendStatus(
            backend_id="execute-backend",
            execution_kind=ExecutionKind.WASM,
            platform="macos-arm64",
            backend_digest=_digest("execute-backend"),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self.evidence = RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(edge.execute_domain.domain_id),
            executable_digest=edge.execute_target.function_implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )
        self.invocations = 0
        self.events: list[str] = []

    def materialize(
        self,
        binding: object,
        reservation_id: str,
    ) -> RuntimeEvidence:
        self.events.append("materialized")
        return self.evidence

    def invoke(self, request: object) -> ProviderOutcome:
        self.invocations += 1
        self.events.append("provider_invoked")
        return ProviderOutcome({"executed": True})

    def cancel(self, request_id: str) -> None:
        pass

    def terminate(self, domain_id: str) -> None:
        pass


def _execute_artifact(edge: _InteractiveEdge) -> PackArtifact:
    operation = ContractOperation(
        contract_id="host.http",
        contract_version="1.0.0",
        revision_digest=edge.execute_target.contract_revision_digest,
        operation_id="invoke",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effect_class=EffectClass.EXTERNAL_EFFECT,
        timeout_default_ms=5_000,
        timeout_hard_max_ms=30_000,
        idempotency="keyed",
        reconcile_operation="status",
    )
    function = FunctionArtifact(
        function_id=edge.execute_target.function_id,
        implementation_digest=edge.execute_target.function_implementation_digest,
        variant_id="execute.variant",
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id="execute.variant",
        digest=_digest("execute-variant"),
        execution_kind=ExecutionKind.WASM,
        os="macos",
        architecture="arm64",
        runtime_abi="component-v1",
        backend="execute-backend",
    )
    return PackArtifact(
        pack_id="execute.pack",
        version="1.0.0",
        digest=edge.execute_target.parent_artifact_digest,
        publisher_lineage="publisher.execute-target",
        package_kind=PackageKind.HOST_EXTENSION,
        functions=(function,),
        variants=(variant,),
    )


def _execute_broker(
    edge: _InteractiveEdge,
    adapter: AuthorityV4Adapter,
    backend: _ExecuteBackend,
) -> RequestBroker:
    artifact = _execute_artifact(edge)
    catalog = OperationCatalog(
        (artifact,),
        (
            OperationRoute(
                contract_id="host.http",
                operation_id="invoke",
                artifact_digest=artifact.digest,
                function_id=edge.execute_target.function_id,
                variant_id="execute.variant",
                execution_domain_profile="dedicated.provider",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(
                    edge.execute_target.principal_id
                ),
            ),
        ),
    )
    return RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=_Admission(),
        authority=adapter,
        audit=adapter,
        reconciliation=InMemoryReconciliationStore(),
    )


def _execute_context(
    harness: _Harness,
    edge: _InteractiveEdge,
    *,
    request_id: str,
) -> RequestContext:
    """Return the Host execute context a pending effect durably records."""

    return replace(
        _context(harness, request_id=request_id),
        caller_principal=OpaqueAuthorityRef(edge.coordinator.principal_id),
        caller_session_id="session-coordinator",
        caller_domain_id=edge.coordinator_domain.domain_id,
        caller_boot_epoch=edge.coordinator_domain.boot_epoch,
        target_domain_id=edge.execute_domain.domain_id,
        target_boot_epoch=edge.execute_domain.boot_epoch,
        target_backend_digest=_digest("execute-backend"),
    )


def _execute_scope(
    harness: _Harness,
    context: RequestContext,
    request_digest: str,
) -> dict[str, object]:
    """Mirror the coordinator's exact one-request execute ceiling."""

    value = harness.scope.to_dict()
    value["dimensions"] = {
        **value["dimensions"],
        "invocation_owner_id": ["owner-1"],
        "caller_session_id": [context.caller_session_id],
        "plan_digest": [context.plan_digest],
    }
    value["exact_request_digest"] = request_digest
    value["opaque"] = False
    return value


def _interactive_fixture(
    tmp_path,
) -> tuple[
    _Harness,
    AuthorityV4Adapter,
    _InteractiveEdge,
    _ExecuteBackend,
    RequestBroker,
    PendingEffectController,
]:
    harness = _Harness(tmp_path)
    edge = _InteractiveEdge(harness)
    adapter = AuthorityV4Adapter(
        harness.kernel,
        _Principals(
            harness.caller,
            harness.target,
            edge.coordinator,
            edge.execute_target,
        ),
    )
    backend = _ExecuteBackend(edge)
    broker = _execute_broker(edge, adapter, backend)
    controller = PendingEffectController(
        persistence=adapter,
        approvals=adapter,
        coordinator_principal=OpaqueAuthorityRef(edge.coordinator.principal_id),
        coordinator_publisher_lineage="publisher.coordinator",
        clock=harness.clock,
    )
    return harness, adapter, edge, backend, broker, controller


def _frame(request_id: str, message: str) -> InvocationFrame:
    return InvocationFrame(
        contract_id="host.http",
        version_range=">=1,<2",
        operation_id="invoke",
        payload={"message": message},
        idempotency_key=f"execute:{request_id}",
    )


def _prepare_execute_effect(
    harness: _Harness,
    edge: _InteractiveEdge,
    broker: RequestBroker,
    controller: PendingEffectController,
    *,
    request_id: str,
    message: str,
) -> tuple[PendingEffectStatus, RequestContext, dict[str, object], str]:
    """Prepare one durable pending effect exactly as the coordinator does."""

    context = _execute_context(harness, edge, request_id=request_id)
    prepared = broker.prepare(_frame(request_id, message), context)
    scope = _execute_scope(harness, context, prepared.request_digest)
    pending = controller.prepare(
        prepared=prepared,
        context=context,
        effect_scope=scope,
        invocation_owner_id="owner-1",
        presentation_owner_principal_id=harness.caller.principal_id,
        presentation_owner_session_id="session-caller",
        presentation_metadata={
            "action": "Run local command",
            "summary": f"Run the prepared command for {message}.",
            "detail": f"message: {message}",
            "confirmation_phrase": "EXECUTE",
        },
        expires_at=harness.clock() + 60,
        typed_confirmation_phrase="EXECUTE",
    )
    return pending, context, scope, prepared.request_digest


def _grant_attestation(
    *,
    request_id: str,
    context: RequestContext,
    edge: _InteractiveEdge,
    scope: Mapping[str, object],
    request_digest: str,
    expires_at: float,
) -> InteractiveApprovalGrantAttestation:
    return InteractiveApprovalGrantAttestation(
        request_id=request_id,
        context=context,
        target_principal=OpaqueAuthorityRef(edge.execute_target.principal_id),
        request_digest=request_digest,
        base_scope=scope,
        invocation_owner_id="owner-1",
        caller_publisher_lineage="publisher.coordinator",
        target_publisher_lineage="publisher.execute-target",
        expires_at=expires_at,
    )


def test_deny_settlement_mints_no_authority_and_blocks_the_execute_edge(
    tmp_path,
) -> None:
    """A denied request cannot dispatch even through the canonical Broker."""

    (
        harness,
        adapter,
        edge,
        backend,
        broker,
        controller,
    ) = _interactive_fixture(tmp_path)
    try:
        pending, context, scope, request_digest = _prepare_execute_effect(
            harness,
            edge,
            broker,
            controller,
            request_id="interactive-deny-1",
            message="denied-operation",
        )
        with bind_host_contract(_CONTRACT):
            denied = adapter.deny_interactive_approval(
                _decision_command(
                    harness,
                    pending.approval_request_id,
                    action="deny",
                )
            )
        assert denied.state == "denied"
        decision = harness.store.get_interactive_approval_decision(
            pending.approval_request_id
        )
        assert decision is not None
        assert decision.approval_id is None and decision.grant_id is None

        # The durable effect folds to CANCELLED for its presentation owner;
        # the Provider transport is never reached through resume either.
        resumed = controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id=harness.caller.principal_id,
            presentation_owner_session_id="session-caller",
            broker=broker,
            wall_clock=harness.clock,
        )
        assert resumed.state is PendingEffectState.CANCELLED
        assert backend.invocations == 0

        # The Grant attestation resume relies on is denied for this request.
        with pytest.raises(AuthorityDenied, match="interactive approval"):
            adapter.assert_interactive_approval_grant(
                _grant_attestation(
                    request_id=pending.approval_request_id,
                    context=context,
                    edge=edge,
                    scope=scope,
                    request_digest=request_digest,
                    expires_at=harness.clock() + 60,
                )
            )

        # A direct Broker attempt with the exact denied scope and digest
        # fails at the static authority gate before admission, materialize,
        # audit reservation, or Provider entry.
        with pytest.raises(AuthorizationError):
            broker.invoke(
                _frame("interactive-deny-1", "denied-operation"),
                context,
                effect_scope=scope,
            )
        assert backend.invocations == 0
        assert backend.events == []
    finally:
        broker.close()


def test_denied_decision_cannot_carry_authority_material(tmp_path) -> None:
    """Settlement itself rejects a denied decision bundled with a Grant."""

    harness, _adapter, edge, _backend, broker, controller = _interactive_fixture(
        tmp_path
    )
    try:
        pending, _ctx, _scp, _req_digest = _prepare_execute_effect(
            harness,
            edge,
            broker,
            controller,
            request_id="interactive-deny-material",
            message="denied-material",
        )
        request, _state = harness.kernel.interactive_approval(
            pending.approval_request_id
        )
        decided_at = harness.clock()
        decision = InteractiveApprovalDecision(
            decision_id=request.request_id,
            request_id=request.request_id,
            request_snapshot_digest=request.digest,
            decision="denied",
            actor_id="user-1",
            decided_at=decided_at,
            security_epoch=request.security_epoch,
            ui_operator_digest=_digest("ui-proof"),
            typed_confirmation_verified=False,
        )
        approval = ApprovalRecord(
            approval_id="interactive-approval-forged",
            snapshot_digest=request.digest,
            actor_id="user-1",
            decision="approved",
            decided_at=decided_at,
            caller=request.caller,
            target=request.target,
            profile_id=request.profile_id,
            effect_bundle_digest=request.base_scope.digest,
            security_epoch=request.security_epoch,
        )
        grant = GrantRecord(
            grant_id="interactive-grant-forged",
            caller=request.caller,
            target=request.target,
            profile_id=request.profile_id,
            activation_id=request.activation_id,
            profile_authority_digest=request.profile_authority_digest,
            caller_publisher_lineage=request.caller_publisher_lineage,
            target_publisher_lineage=request.target_publisher_lineage,
            scope=request.base_scope,
            lifetime=GrantLifetime.ONE_SHOT,
            security_epoch=request.security_epoch,
            approval_id=approval.approval_id,
            issued_at=decided_at,
            expires_at=request.expires_at,
            max_uses=1,
            session_id=request.caller_session_id,
        )

        with pytest.raises(
            AuthorityValidationError, match="cannot mint authority"
        ):
            harness.kernel.settle_interactive_approval(
                decision,
                approval=approval,
                grant=grant,
            )
        assert (
            harness.store.get_interactive_approval_decision(
                pending.approval_request_id
            )
            is None
        )
    finally:
        broker.close()


def test_allow_once_executes_exactly_the_approved_request_once(
    tmp_path,
) -> None:
    """Approval mints one-shot authority for one request digest and scope."""

    (
        harness,
        adapter,
        edge,
        backend,
        broker,
        controller,
    ) = _interactive_fixture(tmp_path)
    try:
        approved, context, scope, request_digest = _prepare_execute_effect(
            harness,
            edge,
            broker,
            controller,
            request_id="interactive-allow-1",
            message="approved-operation",
        )
        unrelated, other_context, other_scope, other_digest = (
            _prepare_execute_effect(
                harness,
                edge,
                broker,
                controller,
                request_id="interactive-unrelated-2",
                message="unrelated-operation",
            )
        )
        with bind_host_contract(_CONTRACT):
            approved_status = adapter.approve_interactive_approval(
                _decision_command(
                    harness,
                    approved.approval_request_id,
                    phrase="EXECUTE",
                )
            )
        assert approved_status.state == "approved"
        decision = harness.store.get_interactive_approval_decision(
            approved.approval_request_id
        )
        assert decision is not None and decision.grant_id is not None
        grant = harness.store.get_grant(decision.grant_id)
        assert grant is not None
        assert grant.lifetime is GrantLifetime.ONE_SHOT
        assert grant.max_uses == 1
        assert grant.scope == AuthorityScope.from_dict(scope)
        assert grant.scope.exact_request_digest == request_digest
        assert grant.session_id == "session-coordinator"

        # While the Grant is still unused, the approved scope cannot be
        # borrowed by a different request digest, widened, or rebound to a
        # foreign caller session; the unrelated pending operation never had
        # a Grant at all.
        foreign_digest = _digest("foreign-request")
        for bad_scope, bad_digest in (
            (scope, foreign_digest),
            (_widened_scope(scope), request_digest),
        ):
            with pytest.raises(AuthorityDenied):
                adapter.check_static_path(
                    StaticAuthorityQuery(
                        context=context,
                        target_principal=OpaqueAuthorityRef(
                            edge.execute_target.principal_id
                        ),
                        request_digest=bad_digest,
                        effect_scope=bad_scope,
                    )
                )
        with pytest.raises(AuthorityDenied):
            adapter.check_static_path(
                StaticAuthorityQuery(
                    context=replace(context, caller_session_id="session-caller"),
                    target_principal=OpaqueAuthorityRef(
                        edge.execute_target.principal_id
                    ),
                    request_digest=request_digest,
                    effect_scope=scope,
                )
            )
        with pytest.raises(AuthorityDenied):
            adapter.check_static_path(
                StaticAuthorityQuery(
                    context=other_context,
                    target_principal=OpaqueAuthorityRef(
                        edge.execute_target.principal_id
                    ),
                    request_digest=other_digest,
                    effect_scope=other_scope,
                )
            )

        resumed = controller.resume_for_presentation(
            effect_id=approved.effect_id,
            presentation_owner_principal_id=harness.caller.principal_id,
            presentation_owner_session_id="session-caller",
            broker=broker,
            wall_clock=harness.clock,
        )
        assert resumed.state is PendingEffectState.SUCCEEDED
        assert backend.invocations == 1
        assert harness.store.grant_usage(grant.grant_id) == (0, 1)

        # A consumed one-shot Grant cannot be claimed or authorized again.
        with pytest.raises(AuthorityDenied, match="interactive approval"):
            harness.kernel.assert_interactive_approval_grant(
                approved.approval_request_id
            )
        with pytest.raises(AuthorizationError):
            broker.invoke(
                _frame("interactive-allow-1", "approved-operation"),
                context,
                effect_scope=scope,
            )
        with pytest.raises(AuthorityDenied):
            adapter.authorize_and_issue_lease(
                FinalAuthorizationQuery(
                    context=replace(context, request_id="interactive-allow-2"),
                    target_principal=OpaqueAuthorityRef(
                        edge.execute_target.principal_id
                    ),
                    request_digest=request_digest,
                    effect_scope=scope,
                    evidence=backend.evidence,
                )
            )

        # The unrelated pending operation remains constrained: its resume
        # stays approval_pending and a direct Broker attempt fails before
        # admission exactly like the denied case.
        assert (
            controller.status(unrelated.effect_id).state
            is PendingEffectState.APPROVAL_PENDING
        )
        with pytest.raises(AuthorizationError):
            broker.invoke(
                _frame("interactive-unrelated-2", "unrelated-operation"),
                other_context,
                effect_scope=other_scope,
            )
        assert backend.invocations == 1
    finally:
        broker.close()


def _widened_scope(scope: Mapping[str, object]) -> dict[str, object]:
    """Return a scope which exceeds the approved Grant in one dimension."""

    widened = AuthorityScope.from_dict(scope).to_dict()
    widened["dimensions"] = {
        **widened["dimensions"],
        "path": ["/safe", "/other"],
    }
    return widened
