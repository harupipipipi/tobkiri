"""The smallest Defaults-independent Profile v4 execution proof."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from tobkiri_host.admission import AdmissionEstimate, QueueScope, ResourceReservation
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import AdmissionTicket, RequestBroker
from tobkiri_host.composition import HostV4Composition
from tests.conformance_support import (
    MINIMAL_CONTRACT_ID,
    MINIMAL_OPERATION_ID,
    MinimalConformanceBackend,
    minimal_context,
    minimal_profile,
)
from tobkiri_host.contracts import AdapterPlanner, ResolvedOperationBinding
from tobkiri_host.effects import InMemoryReconciliationStore
from tobkiri_host.errors import BackendUnavailableError, ResolutionError
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import InvocationFrame, OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    FinalAuthorizationQuery,
    OpaqueAuditReservation,
    OpaqueInvocationLease,
    StaticAuthorityQuery,
)
from tobkiri_host.runtime import ProductionRuntimeV4
from tobkiri_protocol.canonical import canonical_digest


class NoAdapters:
    """The minimal operation has no structural adapter hop."""

    def execute(self, adapter: object, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise AssertionError(f"unexpected adapter execution: {adapter!r}")


@dataclass
class MinimalAdmission:
    events: list[str] = field(default_factory=list)

    def estimate(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        payload: Mapping[str, Any],
    ) -> AdmissionEstimate:
        del context, binding, payload
        self.events.append("admission.estimate")
        return AdmissionEstimate(1, 1, 1, 1, 1)

    def acquire(
        self,
        scope: QueueScope,
        estimate: AdmissionEstimate,
        wait_timeout_seconds: float,
    ) -> AdmissionTicket:
        del wait_timeout_seconds
        self.events.append(f"admission.acquire:{scope.profile_id}")
        return AdmissionTicket(
            ResourceReservation("minimal-reservation", scope.profile_id, estimate.charge())
        )

    def release(self, ticket: AdmissionTicket) -> None:
        self.events.append(f"admission.release:{ticket.reservation.reservation_id}")


@dataclass
class MinimalAuthority:
    events: list[str] = field(default_factory=list)

    def check_static_path(self, query: StaticAuthorityQuery) -> None:
        del query
        self.events.append("authority.static")

    def authorize_and_issue_lease(
        self,
        query: FinalAuthorizationQuery,
    ) -> OpaqueInvocationLease:
        del query
        self.events.append("authority.final")
        return OpaqueInvocationLease(b"minimal-lease")

    def recheck_effect_boundary(
        self,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        lease: OpaqueInvocationLease,
    ) -> None:
        del context, target, lease
        self.events.append("authority.effect")

    def fence_request(self, request_id: str) -> None:
        self.events.append(f"authority.fence:{request_id}")

    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        del registration_id, occurrence_id, target, security_epoch
        return OpaqueInvocationLease(b"minimal-trigger-lease")


@dataclass
class MinimalAudit:
    events: list[str] = field(default_factory=list)

    def reserve_effect(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        request_digest: str,
    ) -> OpaqueAuditReservation:
        del context, binding, request_digest
        self.events.append("audit.reserve")
        return OpaqueAuditReservation("minimal-audit")

    def mark_dispatched(self, reservation: OpaqueAuditReservation) -> None:
        del reservation
        self.events.append("audit.dispatched")

    def commit_effect(self, reservation: OpaqueAuditReservation, outcome_digest: str) -> None:
        del reservation, outcome_digest
        self.events.append("audit.commit")

    def fail_effect(
        self,
        reservation: OpaqueAuditReservation,
        stable_code: str,
        ambiguous: bool,
    ) -> None:
        del reservation, stable_code, ambiguous
        self.events.append("audit.fail")


def _frame(message: object = "hello") -> InvocationFrame:
    return InvocationFrame(
        contract_id=MINIMAL_CONTRACT_ID,
        version_range=">=1,<2",
        operation_id=MINIMAL_OPERATION_ID,
        payload={"message": message},
    )


def _broker(
    composition: HostV4Composition,
    backend: MinimalConformanceBackend,
    *,
    production: bool,
) -> tuple[RequestBroker, list[str]]:
    events: list[str] = []
    authority = MinimalAuthority(events)
    audit = MinimalAudit(events)
    admission = MinimalAdmission(events)
    broker = RequestBroker(
        catalog=composition.catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=admission,
        authority=authority,
        audit=audit,
        reconciliation=InMemoryReconciliationStore(),
        production=production,
    )
    return broker, events


def test_minimal_profile_captures_all_records_and_executes_one_operation() -> None:
    """Prove the Profile-to-Broker path without loading Defaults Profile."""

    profile = minimal_profile()
    runtime = ProductionRuntimeV4.capture(
        profile=profile.profile,
        lock=profile.lock,
        plan=profile.plan,
        activation=profile.activation,
        pack_roots={profile.pack.pack_id: profile.pack_root},
        supporting_artifacts=(profile.base, profile.shell),
        verified_effective_artifacts=profile.artifact_inventory,
        authority_ceilings=profile.authority_ceilings,
    )
    composition = runtime.composition
    backend = MinimalConformanceBackend()
    broker, events = _broker(composition, backend, production=False)
    try:
        assert broker.invoke(
            _frame(),
            minimal_context(backend),
            effect_scope={
                "capability": "operation.invoke",
                "contract": MINIMAL_CONTRACT_ID,
                "operation": MINIMAL_OPERATION_ID,
            },
        ) == {"message": "hello"}
    finally:
        broker.close()

    assert composition.profile["profile_id"] == "conformance.minimal"
    assert composition.lock["effective_set"] == [
        {"role": "base", "identity": profile.base.pack_id, "artifact_digest": profile.base.digest},
        {
            "role": "shell",
            "identity": profile.shell.pack_id,
            "artifact_digest": profile.shell.digest,
        },
        {"role": "pack", "identity": profile.pack.pack_id, "artifact_digest": profile.pack.digest},
    ]
    assert len(composition.plan["bindings"]) == 1
    assert backend.materializations == 1
    assert backend.invocations == 1
    assert events == [
        "authority.static",
        "admission.estimate",
        "admission.acquire:conformance.minimal",
        "authority.final",
        "audit.reserve",
        "authority.effect",
        "audit.dispatched",
        "audit.commit",
        "admission.release:minimal-reservation",
    ]


def test_minimal_conformance_backend_is_unreachable_in_production() -> None:
    """A conformance helper must never become a production backend fallback."""

    profile = minimal_profile()
    backend = MinimalConformanceBackend()
    broker, _events = _broker(profile.composition(), backend, production=True)
    try:
        with pytest.raises(BackendUnavailableError, match="feature-disabled"):
            broker.invoke(_frame(), minimal_context(backend), effect_scope={})
    finally:
        broker.close()

    assert backend.materializations == 0
    assert backend.invocations == 0


def test_minimal_contract_rejects_invalid_payload_before_materialization() -> None:
    """Contract validation must precede admission, materialization, and execution."""

    profile = minimal_profile()
    backend = MinimalConformanceBackend()
    broker, _events = _broker(profile.composition(), backend, production=False)
    try:
        with pytest.raises(ResolutionError, match="input schema"):
            broker.invoke(_frame(message=42), minimal_context(backend), effect_scope={})
    finally:
        broker.close()

    assert backend.materializations == 0
    assert backend.invocations == 0


def test_minimal_profile_rejects_stale_activation_record() -> None:
    """The small fixture keeps ActivationRecord binding checks executable."""

    profile = minimal_profile()
    stale_activation = {**profile.activation, "plan_digest": "sha256:" + "0" * 64}
    with pytest.raises(ResolutionError, match="ActivationRecord"):
        HostV4Composition.capture(
            profile=profile.profile,
            lock=profile.lock,
            plan=profile.plan,
            activation=stale_activation,
            artifacts=profile.artifacts,
            routes=(profile.route,),
            authority_ceilings=profile.authority_ceilings,
            effective_artifacts=profile.artifact_inventory,
        )


def test_minimal_profile_rejects_lock_variant_pin_mismatch() -> None:
    """A re-sealed Lock cannot replace the Plan's exact executable variant."""

    profile = minimal_profile()
    lock = {
        **profile.lock,
        "variant_pins": [
            {**profile.lock["variant_pins"][0], "backend": "tobkiri.remote-pack-v4"}
        ],
    }
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    with pytest.raises(ResolutionError, match="variant pins"):
        HostV4Composition.capture(
            profile=profile.profile,
            lock=lock,
            plan=profile.plan,
            activation=profile.activation,
            artifacts=profile.artifacts,
            routes=(profile.route,),
            authority_ceilings=profile.authority_ceilings,
            effective_artifacts=profile.artifact_inventory,
        )
