"""Verify the alternate physical Wasm route through Broker and v4 Authority."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Mapping

import pytest

from core_runtime.authority.v4 import AuthorityDenied, DomainBoundary
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_tobkiri_host_authority_v4_adapter import (
    _Admission,
    _Backend,
    _NoAdapters,
    _adapter,
    _binding,
    _context,
    _digest,
    _packvm_artifact,
    _queries,
)
from tobkiri_host.admission import AdmissionEstimate, QueueScope
from tobkiri_host.backends import BackendRegistry, NativeWasmBinding
from tobkiri_host.broker import AdmissionTicket, RequestBroker
from tobkiri_host.contracts import (
    AdapterPlanner,
    OperationCatalog,
    ResolvedOperationBinding,
)
from tobkiri_host.effects import InMemoryReconciliationStore, ProviderOutcome
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import (
    InvocationFrame,
    OpaqueAuthorityRef,
    RequestContext,
    RuntimeEvidence,
)


class _WorkerBackend(_Backend):
    memory_reservation_bytes = 100

    def __init__(self, harness: _Harness, binding: ResolvedOperationBinding) -> None:
        super().__init__(harness, ProviderOutcome({"ok": True}))
        self.status = replace(self.status, backend_digest=_digest("native-worker"))
        self.evidence = replace(
            self.evidence,
            domain_ref=OpaqueAuthorityRef("domain.native-worker"),
            executable_digest=_digest("native-executable"),
            backend_digest=self.status.backend_digest,
            guest_artifact_identity=_digest("native-executable"),
            guest_execution_boundary="wasm-component",
        )
        self.binding = binding
        self.calls: list[str] = []

    def materialize(
        self, binding: ResolvedOperationBinding, reservation_id: str
    ) -> RuntimeEvidence:
        assert binding == self.binding
        self.calls.append("materialize")
        return replace(self.evidence, resource_reservation_id=reservation_id)

    def invoke(self, request: object) -> ProviderOutcome:
        self.calls.append("invoke")
        return super().invoke(request)

    def terminate(self, domain_id: str) -> None:
        self.calls.append("terminate")

    def release_materialization(self, reservation_id: str) -> None:
        self.calls.append("release")


class _DetachedAdmission(_Admission):
    def estimate(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        payload: Mapping[str, object],
    ) -> AdmissionEstimate:
        return replace(super().estimate(context, binding, payload), detached=True)

    def acquire(
        self,
        scope: QueueScope,
        estimate: AdmissionEstimate,
        wait_timeout_seconds: float,
    ) -> AdmissionTicket:
        assert estimate.detached is False
        assert estimate.backend_overhead_bytes == _WorkerBackend.memory_reservation_bytes
        return super().acquire(scope, estimate, wait_timeout_seconds)


@pytest.mark.parametrize("resume", [False, True])
def test_broker_uses_exact_wasm_route_and_commits_audit(
    tmp_path: Path, resume: bool
) -> None:
    harness = _Harness(tmp_path)
    artifact = _packvm_artifact(harness)
    catalog = OperationCatalog((artifact,), (_binding(harness).route,))
    binding = catalog.resolve("host.http", "invoke", None)
    backend = _WorkerBackend(harness, binding)
    context = replace(
        _context(harness),
        target_domain_id=backend.evidence.domain_ref.value,
        target_backend_digest=backend.status.backend_digest,
    )
    native = NativeWasmBinding(
        binding,
        backend,
        context.caller_principal,
        context.target_domain_id,
        backend.evidence.executable_digest,
    )
    adapter = _adapter(harness)
    adapter.install_native_wasm_binding_resolver(lambda context, target: native)
    domain = replace(
        harness.target_domain,
        domain_id=native.domain_id,
        boundary=DomainBoundary.WASM_COMPONENT,
    )
    adapter.register_execution_domain(
        domain,
        session_id="native-session",
        channel_digest=domain.authenticated_channel_digest,
        principal_ref=binding.principal_ref,
    )
    adapter.commit_provider_authority_bundle(
        provider_authorities=(
            replace(
                harness.provider,
                record_id="native-provider-authority",
                execution_domain_id=domain.domain_id,
                execution_domain_identity_digest=domain.identity_digest,
            ),
        ),
    )
    _, final = _queries(harness, context, _digest("request"))
    audit_before_denials = harness.store.audit_events()
    for evidence in (
        replace(backend.evidence, executable_digest=final.evidence.executable_digest),
        replace(backend.evidence, guest_artifact_identity=_digest("other-manifest")),
        replace(backend.evidence, guest_execution_boundary="packvm"),
    ):
        with pytest.raises(AuthorityDenied, match="runtime evidence"):
            adapter.authorize_and_issue_lease(replace(final, evidence=evidence))
    assert harness.store.audit_events() == audit_before_denials
    with RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry(()),
        materialization=MaterializationCoordinator(),
        admission=_DetachedAdmission(),
        authority=adapter,
        audit=adapter,
        reconciliation=InMemoryReconciliationStore(),
        native_wasm_binding_for=lambda context, binding: native,
    ) as broker:
        frame = InvocationFrame("host.http", None, "invoke", {}, idempotency_key="key")
        snapshot = broker.prepare(frame, context).to_snapshot()
        with pytest.raises(AuthorizationError, match="execution binding"):
            broker.invoke(
                frame,
                replace(context, target_backend_digest=_digest("different-backend")),
                effect_scope=harness.scope.to_dict(),
            )
        assert not backend.calls
        evidence = backend.evidence
        backend.evidence = replace(
            evidence,
            executable_digest=binding.function.implementation_digest,
        )
        with pytest.raises(AuthorizationError, match="runtime evidence mismatch"):
            broker.invoke(frame, context, effect_scope=harness.scope.to_dict())
        assert backend.calls == ["materialize", "terminate", "release"]
        assert harness.store.audit_events() == audit_before_denials
        backend.evidence = evidence
        backend.calls.clear()
        if resume:
            captured = native
            native = replace(captured, executable_digest=_digest("other-manifest"))
            with pytest.raises(AuthorizationError, match="binding changed"):
                broker.validate_prepared_snapshot(snapshot, context)
            native = captured
            result = broker.invoke_prepared(
                snapshot,
                context,
                harness.scope.to_dict(),
                execute_not_after_wall=time.time() + 10,
            )
        else:
            result = broker.invoke(frame, context, effect_scope=harness.scope.to_dict())
    assert result == {"ok": True}
    assert backend.calls == ["materialize", "invoke", "release"]
    assert [entry["event_state"] for entry in harness.store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]


def test_final_authority_requires_genuine_wasm_domain(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    binding = _binding(harness)
    backend = _WorkerBackend(harness, binding)
    context = replace(
        _context(harness),
        target_backend_digest=backend.status.backend_digest,
    )
    native = NativeWasmBinding(
        binding,
        backend,
        context.caller_principal,
        context.target_domain_id,
        backend.evidence.executable_digest,
    )
    adapter = _adapter(harness)
    adapter.install_native_wasm_binding_resolver(lambda context, target: native)
    _, final = _queries(harness, context, _digest("request"))
    audit_before_denials = harness.store.audit_events()
    with pytest.raises(AuthorityDenied, match="target identity"):
        adapter.authorize_and_issue_lease(
            replace(
                final,
                evidence=replace(backend.evidence, domain_ref=final.evidence.domain_ref),
            )
        )
    assert harness.store.audit_events() == audit_before_denials
