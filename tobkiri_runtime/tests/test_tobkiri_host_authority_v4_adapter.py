"""End-to-end and adversarial tests for the canonical Host/v4 adapter."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Mapping

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityKernel,
    AuditUnavailable,
    FunctionPrincipal,
    LeaseState,
    authority_digest,
)
from tobkiri_host.admission import AdmissionEstimate, ResourceReservation
from tobkiri_host.artifact_materialization import (
    MaterializedArtifactFile,
    MaterializedPackArtifact,
    _materialization_digest,
)
from tobkiri_host.authority_v4 import (
    AuthorityV4Adapter,
    TriggerAuthorityBinding,
)
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.broker import AdmissionTicket, RequestBroker
from tobkiri_host.contracts import AdapterPlanner, OperationCatalog, OperationRoute
from tobkiri_host.effects import (
    EffectDisposition,
    InMemoryReconciliationStore,
    ProviderOutcome,
)
from tobkiri_host.errors import (
    AmbiguousEffectError,
    BackendUnavailableError,
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
from tobkiri_host.ports import FinalAuthorizationQuery, StaticAuthorityQuery
from tobkiri_host.platform_backends import (
    IsolationLaunch,
    PlatformAttestation,
    ProductionIsolationBackend,
    _platform_attestation_digest,
)

from tests.test_authority_v4_lifecycle import _Harness


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class _Principals:
    def __init__(self, *principals: FunctionPrincipal) -> None:
        self._values = {item.principal_id: item for item in principals}

    def resolve_principal(self, reference: OpaqueAuthorityRef) -> FunctionPrincipal:
        try:
            return self._values[reference.value]
        except KeyError as exc:
            raise AuthorityDenied("unknown Host principal reference") from exc


def _context(harness: _Harness, *, request_id: str = "request-1") -> RequestContext:
    return RequestContext(
        request_id=request_id,
        trace_id="trace-1",
        caller_principal=OpaqueAuthorityRef(harness.caller.principal_id),
        profile_id="profile-1",
        activation_id="activation-1",
        activation_digest=_digest("activation"),
        plan_digest=_digest("plan"),
        security_epoch=1,
        caller_session_id="session-caller",
        caller_domain_id=harness.caller_domain.domain_id,
        caller_boot_epoch=harness.caller_domain.boot_epoch,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        target_backend_digest=_digest("backend"),
        profile_authority_digest=_digest("4"),
        fencing_token=harness.target_domain.fencing_token,
        handle_namespace=harness.caller_domain.resource_namespace,
    )


def _adapter(harness: _Harness, **kwargs: object) -> AuthorityV4Adapter:
    return AuthorityV4Adapter(
        harness.kernel,
        _Principals(harness.caller, harness.target),
        **kwargs,
    )


def _queries(harness: _Harness, context: RequestContext, request_digest: str):
    target = OpaqueAuthorityRef(harness.target.principal_id)
    static = StaticAuthorityQuery(
        context=context,
        target_principal=target,
        request_digest=request_digest,
        effect_scope=harness.scope.to_dict(),
    )
    final = FinalAuthorizationQuery(
        context=context,
        target_principal=target,
        request_digest=request_digest,
        effect_scope=harness.scope.to_dict(),
        evidence=RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(harness.target_domain.domain_id),
            executable_digest=harness.target.function_implementation_digest,
            backend_digest=_digest("backend"),
            authenticated_channel=True,
            nonce_fresh=True,
        ),
    )
    return static, final


def test_adapter_authorizes_dispatches_and_finishes_authoritative_audit(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    request_digest = _digest("host-request")
    static, final = _queries(harness, context, request_digest)

    adapter.check_static_path(static)
    lease = adapter.authorize_and_issue_lease(final)
    reservation = adapter.reserve_effect(
        context,
        _binding(harness),
        request_digest,
    )
    adapter.recheck_effect_boundary(
        context,
        OpaqueAuthorityRef(harness.target.principal_id),
        lease,
    )
    adapter.mark_dispatched(reservation)
    adapter.commit_effect(reservation, _digest("outcome"))

    stored = harness.store.get_lease(reservation.value)
    assert stored is not None and stored[1] is LeaseState.COMMITTED
    assert [event["event_state"] for event in harness.store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]


def test_broker_close_is_idempotent_and_context_managed(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    frame = InvocationFrame(
        contract_id="host.http",
        version_range=">=1,<2",
        operation_id="invoke",
        payload={"message": "hello"},
        idempotency_key="request-close",
    )
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))

    broker.close()
    broker.close()
    with pytest.raises(RuntimeError, match="broker is closed"):
        broker.invoke(frame, _context(harness), effect_scope=harness.scope.to_dict())

    scoped = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    with scoped as entered:
        assert entered is scoped
    with pytest.raises(RuntimeError, match="broker is closed"):
        scoped.invoke(frame, _context(harness), effect_scope=harness.scope.to_dict())


class _Admission:
    def estimate(self, context, binding, payload) -> AdmissionEstimate:
        return AdmissionEstimate(1, 1, 1, 1, 1)

    def acquire(self, scope, estimate, wait_timeout_seconds) -> AdmissionTicket:
        return AdmissionTicket(ResourceReservation("reservation-1", scope.profile_id, 1))

    def release(self, ticket: AdmissionTicket) -> None:
        return None


class _CountingAdmission(_Admission):
    def __init__(self) -> None:
        self.count = 0

    def acquire(self, scope, estimate, wait_timeout_seconds) -> AdmissionTicket:
        self.count += 1
        return AdmissionTicket(
            ResourceReservation(f"reservation-{self.count}", scope.profile_id, 1)
        )


class _Backend:
    def __init__(self, harness: _Harness, outcome: ProviderOutcome) -> None:
        self.outcome = outcome
        self.status = BackendStatus(
            backend_id="provider-backend",
            execution_kind=ExecutionKind.WASM,
            platform="macos-arm64",
            backend_digest=_digest("backend"),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self.evidence = RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(harness.target_domain.domain_id),
            executable_digest=harness.target.function_implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def materialize(self, binding, reservation_id) -> RuntimeEvidence:
        return self.evidence

    def invoke(self, request) -> ProviderOutcome:
        return self.outcome

    def cancel(self, request_id: str) -> None:
        return None

    def terminate(self, domain_id: str) -> None:
        return None


class _DirectVZDriver:
    backend_id = "tobkiri.python-pack-v4"
    substrate_id = "macos-vz"
    backend_digest = _digest("backend")
    platform = "macos-arm64"

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.launch_count = 0
        self.invocations: list[object] = []

    def capability(self) -> tuple[bool, str | None]:
        return True, None

    def launch(self, request: IsolationLaunch) -> PlatformAttestation:
        self.launch_count += 1
        attestation = PlatformAttestation(
            domain_id=(
                "domain:attacker"
                if self.mode == "identity-mismatch"
                else request.target_domain_id
            ),
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            platform=self.platform,
            executable_digest=request.executable_digest,
            artifact_digest=request.artifact_digest,
            materialization_digest=request.artifact.materialization_digest,
            guest_artifact_identity=_digest("guest-artifact"),
            isolation_profile=request.isolation_profile,
            attestation_digest=_digest("pending-attestation"),
            attestation_nonce=f"direct-vz-nonce-{self.launch_count}",
            lease_id=request.lease.lease_id,
            reservation_id=request.reservation_id,
            authenticated_channel=True,
            nonce_fresh=self.mode != "replay",
        )
        attestation = replace(
            attestation,
            attestation_digest=_platform_attestation_digest(attestation),
        )
        if self.mode == "attestation-tamper":
            return replace(attestation, attestation_digest=_digest("tampered"))
        return attestation

    def invoke(self, request: object) -> ProviderOutcome:
        self.invocations.append(request)
        return ProviderOutcome({"ok": True})

    def cancel(self, request_id: str) -> None:
        return None

    def terminate(self, domain_id: str) -> None:
        return None


class _NoAdapters:
    def execute(self, adapter, payload: Mapping[str, object]):
        raise AssertionError("no structural adapter is configured")


def _broker(
    harness: _Harness,
    adapter: AuthorityV4Adapter,
    outcome: ProviderOutcome,
) -> RequestBroker:
    artifact = _artifact(harness)
    catalog = OperationCatalog(
        (artifact,),
        (
            OperationRoute(
                contract_id="host.http",
                operation_id="invoke",
                artifact_digest=artifact.digest,
                function_id=harness.target.function_id,
                variant_id="provider.variant",
                execution_domain_profile="dedicated.provider",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(harness.target.principal_id),
            ),
        ),
    )
    return RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry((_Backend(harness, outcome),)),
        materialization=MaterializationCoordinator(),
        admission=_Admission(),
        authority=adapter,
        audit=adapter,
        reconciliation=InMemoryReconciliationStore(),
    )


def _packvm_artifact(harness: _Harness) -> PackArtifact:
    artifact = _artifact(harness)
    return replace(
        artifact,
        variants=(
            replace(
                artifact.variants[0],
                execution_kind=ExecutionKind.PACK_VM,
                runtime_abi="packvm-v1",
                backend="tobkiri.python-pack-v4",
            ),
        ),
    )


def _materialized_packvm_artifact(harness: _Harness) -> MaterializedPackArtifact:
    artifact_file = MaterializedArtifactFile(
        path="runtime/handler.py",
        digest=harness.target.function_implementation_digest,
        executable=False,
        content=b"B",
    )
    files = (artifact_file,)
    return MaterializedPackArtifact(
        pack_id="provider.pack",
        artifact_digest=harness.target.parent_artifact_digest,
        function_id=harness.target.function_id,
        implementation_digest=harness.target.function_implementation_digest,
        implementation_path=artifact_file.path,
        materialization_digest=_materialization_digest(
            "provider.pack",
            harness.target.parent_artifact_digest,
            harness.target.function_id,
            harness.target.function_implementation_digest,
            artifact_file.path,
            files,
        ),
        root_device=1,
        root_inode=1,
        files=files,
    )


def _packvm_broker(
    harness: _Harness,
    adapter: AuthorityV4Adapter,
    driver: _DirectVZDriver,
) -> RequestBroker:
    artifact = _packvm_artifact(harness)
    catalog = OperationCatalog(
        (artifact,),
        (
            OperationRoute(
                contract_id="host.http",
                operation_id="invoke",
                artifact_digest=artifact.digest,
                function_id=harness.target.function_id,
                variant_id="provider.variant",
                execution_domain_profile="dedicated.provider",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(harness.target.principal_id),
            ),
        ),
    )
    backend = ProductionIsolationBackend(
        driver,
        artifact_resolver=lambda _binding: _materialized_packvm_artifact(harness),
        target_domain_resolver=lambda _binding: harness.target_domain.domain_id,
        clock=harness.clock,
    )
    return RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=_CountingAdmission(),
        authority=adapter,
        audit=adapter,
        reconciliation=InMemoryReconciliationStore(),
    )


def test_packvm_broker_binds_authority_domain_and_invokes_direct_vz(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    driver = _DirectVZDriver()
    broker = _packvm_broker(harness, _adapter(harness), driver)
    frame = InvocationFrame(
        contract_id="host.http",
        version_range=">=1,<2",
        operation_id="invoke",
        payload={"message": "hello"},
        idempotency_key="packvm-request-1",
    )
    context = _context(harness)
    try:
        assert broker.invoke(
            frame,
            context,
            effect_scope=harness.scope.to_dict(),
        ) == {"ok": True}
    finally:
        broker.close()
    assert len(driver.invocations) == 1


@pytest.mark.parametrize(
    "mode",
    ("identity-mismatch", "replay", "attestation-tamper"),
)
def test_packvm_broker_rejects_mismatched_or_tampered_attestation(
    tmp_path: Path,
    mode: str,
) -> None:
    harness = _Harness(tmp_path)
    driver = _DirectVZDriver(mode)
    broker = _packvm_broker(harness, _adapter(harness), driver)
    try:
        with pytest.raises(BackendUnavailableError, match="attestation"):
            broker.invoke(
                InvocationFrame(
                    contract_id="host.http",
                    version_range=">=1,<2",
                    operation_id="invoke",
                    payload={},
                    idempotency_key=f"packvm-{mode}",
                ),
                _context(harness),
                effect_scope=harness.scope.to_dict(),
            )
    finally:
        broker.close()
    assert not driver.invocations


def test_broker_end_to_end_uses_only_v4_authority_and_audit(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    frame = InvocationFrame(
        contract_id="host.http",
        version_range=">=1,<2",
        operation_id="invoke",
        payload={"message": "hello"},
        idempotency_key="request-1",
    )
    try:
        assert broker.invoke(
            frame,
            _context(harness),
            effect_scope=harness.scope.to_dict(),
        ) == {"ok": True}
    finally:
        broker.close()
    assert [event["event_state"] for event in harness.store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]


def test_ambiguous_provider_outcome_is_never_committed_or_retried(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    broker = _broker(
        harness,
        adapter,
        ProviderOutcome(None, disposition=EffectDisposition.UNKNOWN),
    )
    frame = InvocationFrame(
        contract_id="host.http",
        version_range=">=1,<2",
        operation_id="invoke",
        payload={"message": "hello"},
        idempotency_key="request-1",
    )
    try:
        with pytest.raises(AmbiguousEffectError):
            broker.invoke(
                frame,
                _context(harness),
                effect_scope=harness.scope.to_dict(),
            )
    finally:
        broker.close()
    assert harness.store.audit_events()[-1]["event_state"] == "ambiguous"


def test_stale_epoch_and_caller_identity_swap_fail_before_lease(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    stale = replace(_context(harness), security_epoch=2)
    static, _final = _queries(harness, stale, _digest("stale"))
    with pytest.raises(AuthorityDenied, match="stale"):
        adapter.check_static_path(static)

    swapped = replace(
        _context(harness),
        caller_principal=OpaqueAuthorityRef(harness.target.principal_id),
    )
    _static, final = _queries(harness, swapped, _digest("swap"))
    with pytest.raises(AuthorityDenied, match="caller identity"):
        adapter.authorize_and_issue_lease(final)
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)


@pytest.mark.parametrize("field", ["path", "max_bytes"])
def test_adapter_rejects_omitted_scope_bounds_before_lease(
    tmp_path: Path,
    field: str,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    static, final = _queries(harness, context, _digest(f"omitted-{field}"))
    unbounded = harness.scope.to_dict()
    if field == "path":
        unbounded["dimensions"].pop("path")
    else:
        unbounded["quotas"].pop("max_bytes")

    with pytest.raises(AuthorityDenied):
        adapter.check_static_path(replace(static, effect_scope=unbounded))
    with pytest.raises(AuthorityDenied):
        adapter.authorize_and_issue_lease(replace(final, effect_scope=unbounded))
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)


def test_captured_plan_and_runtime_evidence_swaps_fail_closed(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    changed_plan = replace(_context(harness), plan_digest=_digest("attacker-plan"))
    static, _final = _queries(harness, changed_plan, _digest("changed-plan"))
    with pytest.raises(AuthorityDenied, match="ResolvedPlan"):
        adapter.check_static_path(static)

    context = _context(harness)
    _static, final = _queries(harness, context, _digest("changed-evidence"))
    final = replace(
        final,
        evidence=replace(final.evidence, backend_digest=_digest("wrong-backend")),
    )
    with pytest.raises(AuthorityDenied, match="runtime evidence"):
        adapter.authorize_and_issue_lease(final)


def test_audit_failure_rolls_back_grant_use_and_lease(tmp_path: Path) -> None:
    fail = False

    def audit_fault() -> None:
        if fail:
            raise OSError("disk full")

    harness = _Harness(tmp_path, audit_fault=audit_fault)
    adapter = _adapter(harness)
    context = _context(harness)
    _static, final = _queries(harness, context, _digest("audit-failure"))
    fail = True
    with pytest.raises(AuditUnavailable, match="audit"):
        adapter.authorize_and_issue_lease(final)
    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)


def test_lease_replay_and_revoke_after_authorization_are_denied(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    _static, final = _queries(harness, context, _digest("replay"))
    lease = adapter.authorize_and_issue_lease(final)
    adapter.recheck_effect_boundary(context, OpaqueAuthorityRef(harness.target.principal_id), lease)
    with pytest.raises(AuthorityDenied, match="consumed"):
        adapter.recheck_effect_boundary(
            context, OpaqueAuthorityRef(harness.target.principal_id), lease
        )

    second_context = _context(harness, request_id="request-2")
    _static, second_final = _queries(harness, second_context, _digest("revoke"))
    second = adapter.authorize_and_issue_lease(second_final)
    harness.kernel.revoke(
        target_kind="provider_authority",
        target_id=harness.provider.record_id,
        reason="compromised",
    )
    with pytest.raises(AuthorityDenied):
        adapter.recheck_effect_boundary(
            second_context,
            OpaqueAuthorityRef(harness.target.principal_id),
            second,
        )


def test_request_fence_revokes_unused_lease_and_releases_reservation(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    _static, final = _queries(harness, context, _digest("cancelled"))
    lease = adapter.authorize_and_issue_lease(final)
    assert harness.store.grant_usage(harness.grant.grant_id) == (1, 0)

    adapter.fence_request(context.request_id)

    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)
    with pytest.raises(AuthorityDenied):
        adapter.recheck_effect_boundary(
            context,
            OpaqueAuthorityRef(harness.target.principal_id),
            lease,
        )


def test_security_epoch_advance_fences_previously_issued_adapter_lease(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    _static, final = _queries(harness, context, _digest("old-epoch"))
    lease = adapter.authorize_and_issue_lease(final)

    assert adapter.advance_security_epoch("emergency") == 2

    with pytest.raises(AuthorityDenied):
        adapter.recheck_effect_boundary(
            context,
            OpaqueAuthorityRef(harness.target.principal_id),
            lease,
        )


def test_revoke_after_dispatch_prevents_authoritative_commit(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    request_digest = _digest("revoke-after-dispatch")
    _static, final = _queries(harness, context, request_digest)
    lease = adapter.authorize_and_issue_lease(final)
    reservation = adapter.reserve_effect(context, _binding(harness), request_digest)
    adapter.recheck_effect_boundary(context, OpaqueAuthorityRef(harness.target.principal_id), lease)
    harness.kernel.revoke(
        target_kind="pack_artifact",
        target_id=harness.target.parent_artifact_digest,
        reason="compromised after dispatch",
    )
    with pytest.raises(AuthorityDenied, match="committed"):
        adapter.commit_effect(reservation, _digest("must-not-commit"))


class _TriggerResolver:
    def __init__(self, harness: _Harness) -> None:
        self.harness = harness

    def resolve_trigger_authority(
        self,
        *,
        registration_id: str,
        occurrence_id: str,
        target: FunctionPrincipal,
        security_epoch: int,
    ) -> TriggerAuthorityBinding:
        request_digest = authority_digest(
            {
                "registration_id": registration_id,
                "occurrence_id": occurrence_id,
                "target_principal_id": target.principal_id,
                "security_epoch": security_epoch,
            }
        )
        return TriggerAuthorityBinding(
            context=self.harness.context(
                request_id=f"trigger-{occurrence_id}",
                request_digest=request_digest,
                effect_digest=self.harness.scope.digest,
            ),
            scope=self.harness.scope,
        )


def test_trigger_lease_is_occurrence_bound_and_one_shot(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness, trigger_resolver=_TriggerResolver(harness))
    lease = adapter.issue_trigger_lease(
        "registration-1",
        "occurrence-1",
        OpaqueAuthorityRef(harness.target.principal_id),
        1,
    )
    token = lease.token.decode("ascii")
    durable = harness.kernel.dispatch(
        token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=authority_digest(
            {
                "registration_id": "registration-1",
                "occurrence_id": "occurrence-1",
                "target_principal_id": harness.target.principal_id,
                "security_epoch": 1,
            }
        ),
    )
    with pytest.raises(AuthorityDenied, match="already used"):
        harness.kernel.dispatch(
            token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=durable.request_digest,
        )


def test_restart_recovery_marks_dispatched_adapter_effect_ambiguous(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    context = _context(harness)
    request_digest = _digest("restart")
    _static, final = _queries(harness, context, request_digest)
    lease = adapter.authorize_and_issue_lease(final)
    reservation = adapter.reserve_effect(context, _binding(harness), request_digest)
    adapter.recheck_effect_boundary(context, OpaqueAuthorityRef(harness.target.principal_id), lease)

    restarted_kernel = AuthorityKernel(
        harness.store,
        harness.kernel._binding_resolver,
        clock=harness.clock,
    )
    restarted = AuthorityV4Adapter(
        restarted_kernel,
        _Principals(harness.caller, harness.target),
    )
    assert restarted.recover() == [reservation.value]
    recovered = harness.store.get_lease(reservation.value)
    assert recovered is not None and recovered[1] is LeaseState.AMBIGUOUS


def _binding(harness: _Harness):
    artifact = _artifact(harness)
    return OperationCatalog(
        (artifact,),
        (
            OperationRoute(
                contract_id="host.http",
                operation_id="invoke",
                artifact_digest=artifact.digest,
                function_id=harness.target.function_id,
                variant_id="provider.variant",
                execution_domain_profile="dedicated.provider",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(harness.target.principal_id),
            ),
        ),
    ).resolve("host.http", "invoke", ">=1,<2")


def _artifact(harness: _Harness) -> PackArtifact:
    operation = ContractOperation(
        contract_id="host.http",
        contract_version="1.0.0",
        revision_digest=harness.target.contract_revision_digest,
        operation_id="invoke",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effect_class=EffectClass.EXTERNAL_EFFECT,
        idempotency="keyed",
        reconcile_operation="status",
    )
    function = FunctionArtifact(
        function_id=harness.target.function_id,
        implementation_digest=harness.target.function_implementation_digest,
        variant_id="provider.variant",
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id="provider.variant",
        digest=_digest("variant"),
        execution_kind=ExecutionKind.WASM,
        os="macos",
        architecture="arm64",
        runtime_abi="component-v1",
        backend="provider-backend",
    )
    return PackArtifact(
        pack_id="provider.pack",
        version="1.0.0",
        digest=harness.target.parent_artifact_digest,
        publisher_lineage="publisher.target",
        package_kind=PackageKind.HOST_EXTENSION,
        functions=(function,),
        variants=(variant,),
    )
