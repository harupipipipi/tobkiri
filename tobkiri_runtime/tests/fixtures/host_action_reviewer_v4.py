"""Offline fixture: actual independent Store/Broker/gateway on the gate's store.

Fixture source principals and native user approval remain test evidence. This
helper never installs a production route, settings, keys or network transport.
"""

from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import threading
import time

from core_runtime.authority.v4 import (
    AuthorityKernel,
    AuthorityScope,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
    AuthorityMode,
    authority_digest,
    AuthorityDenied,
)
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from core_runtime.invocation_scope_v4 import assert_dispatched_invocation
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import HOST_PROVIDER_FACTORY
from tests.test_authority_v4_lifecycle import _principal, _domain, _Resolver, _digest
from tests.test_tobkiri_host_authority_v4_adapter import (
    _artifact,
    _context,
    _adapter,
    _Admission,
    _NoAdapters,
)
from tobkiri_host.models import EffectClass, ExecutionKind, OpaqueAuthorityRef, InvocationFrame
from tobkiri_host.contracts import OperationCatalog, OperationRoute, AdapterPlanner
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import RequestBroker
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.effects import InMemoryReconciliationStore
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_protocol.canonical import canonical_digest
from core_runtime.bootstrap.action_review_v4.reviewer_session_binder import (
    review_transport_snapshot,
)
from core_runtime.bootstrap.action_review_v4.canonical_reviewer_port import (
    CapturedReviewerBoundary,
    CanonicalReviewerGeneratePort,
    ReviewerGenerateLease,
    GENERATE_TARGET,
)
from core_runtime.bootstrap.action_review_v4.exact_operation_review_adapter import (
    ExactOperationReviewAdapter,
)
from core_runtime.bootstrap.action_review_v4.reviewer_transport_authentication import (
    CommittedReviewerTransportAuthenticator,
)
from core_runtime.authority.policy_exact_grant import CapturedReviewEvidence
from tests.test_host_action_reviewer_canonical_port import RegisteredFixtureTransport


def build_shared_store_reviewer(
    harness, *, turn_id="turn-1", lease_ttl_seconds=30, evidence_validity_seconds=None
):
    """Register independent fixture authority before native mode approval."""
    caller = replace(
        _principal("review-caller"),
        function_id="rumi_host_authority_bridge_pack.host-authority.approval-policy",
    )
    target = replace(
        _principal("review-gateway", operation=GENERATE_TARGET[1]), function_id=GENERATE_TARGET[1]
    )
    caller_domain, target_domain = (
        _domain("review-caller", caller),
        _domain("review-gateway", target),
    )
    activation_digest = canonical_digest({"activation_id": "activation-1"})
    scope = AuthorityScope(
        "operation.invoke",
        target.contract_revision_digest,
        dimensions={
            "contract": (GENERATE_TARGET[0],),
            "operation": (GENERATE_TARGET[1],),
            "request_surface": ("approval-review",),
            "tool_calling": ("false",),
            "allow_failover": ("false",),
        },
    )

    class Resolver(_Resolver):
        def resolve_authority_binding(self, **kwargs):
            return replace(
                super().resolve_authority_binding(**kwargs), activation_digest=activation_digest
            )

    kernel = AuthorityKernel(
        harness.store, Resolver(scope), clock=harness.clock, lease_ttl_seconds=lease_ttl_seconds
    )
    for domain, principal, session in (
        (caller_domain, caller, "session-review-caller"),
        (target_domain, target, "session-review-gateway"),
    ):
        kernel.register_execution_domain(
            domain,
            session_id=session,
            channel_digest=domain.authenticated_channel_digest,
            principal=principal,
        )
    trust = HostExtensionTrustRecord(
        "review-gateway-trust",
        target.parent_artifact_digest,
        "publisher.target",
        (target.principal_id,),
        _digest("review-gateway-trust"),
        1,
        harness.clock(),
    )
    provider = ProviderAuthorityRecord(
        "review-gateway-provider",
        target,
        target_domain.domain_id,
        target_domain.identity_digest,
        scope,
        AuthorityMode.LEASE_ONLY,
        1,
        trust.trust_provenance_digest,
        "publisher.target",
        trust.trust_id,
        harness.clock(),
        host_broker_binding="broker.review-gateway",
    )
    approval = replace(
        harness.approval, approval_id="review-independent-approval", caller=caller, target=target
    )
    grant = replace(
        harness.grant,
        grant_id="review-independent-grant",
        caller=caller,
        target=target,
        scope=scope,
        approval_id=approval.approval_id,
        max_uses=None,
    )
    kernel.commit_approval_bundle(
        approval, host_extension_trust=trust, provider_authorities=(provider,), grants=(grant,)
    )
    # The gate fixtures use fence7. Use actual journal transitions, never edit
    # the ledger or bypass its monotonic reservation path.
    if harness.store.active_activation_reservation("activation-1") is None:
        for n in range(1, 8):
            ident = "activation-1" if n == 7 else "review-aborted-" + str(n)
            reservation, fence = harness.store.reserve_activation(
                activation_id=ident,
                profile_id="profile-1",
                plan_digest=_digest("plan"),
                profile_authority_digest=_digest("4"),
                security_epoch=1,
            )
            if n < 7:
                harness.store.transition_activation(
                    reservation, expected_state="prepared", new_state="aborted"
                )
            else:
                assert fence == 7
                for a, b in (
                    ("prepared", "ready_without_authority"),
                    ("ready_without_authority", "committing"),
                    ("committing", "active"),
                ):
                    harness.store.transition_activation(reservation, expected_state=a, new_state=b)
    proxy = SimpleNamespace(**vars(harness))
    proxy.caller, proxy.target = caller, target
    proxy.caller_domain, proxy.target_domain, proxy.kernel, proxy.scope = (
        caller_domain,
        target_domain,
        kernel,
        scope,
    )
    artifact = _artifact(proxy)
    operation = replace(
        artifact.functions[0].operations[0],
        contract_id=GENERATE_TARGET[0],
        operation_id=GENERATE_TARGET[1],
        effect_class=EffectClass.EXTERNAL_EFFECT,
        idempotency="none",
        reconcile_operation=None,
    )
    artifact = replace(
        artifact,
        functions=(replace(artifact.functions[0], operations=(operation,)),),
        variants=(
            replace(
                artifact.variants[0],
                execution_kind=ExecutionKind.HOST_EXTENSION,
                backend="tobkiri.python-host-v4",
            ),
        ),
    )
    route = OperationRoute(
        GENERATE_TARGET[0],
        GENERATE_TARGET[1],
        artifact.digest,
        target.function_id,
        "provider.variant",
        "dedicated.provider",
        "on_demand",
        OpaqueAuthorityRef(target.principal_id),
    )
    catalog = OperationCatalog((artifact,), (route,))
    binding = catalog.resolve_pinned(*GENERATE_TARGET)
    captured = HOST_PROVIDER_FACTORY[GENERATE_TARGET[1]].capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={(*GENERATE_TARGET, target.principal_id): target_domain.domain_id},
        )
    )
    transport = RegisteredFixtureTransport()
    backend = ExactHostProviderBackendV4(
        captured.contributions,
        backend_id="tobkiri.python-host-v4",
        profile_id="profile-1",
        plan_digest=_digest("plan"),
        security_epoch=1,
        invocation_context=lambda envelope: SimpleNamespace(
            contract_client=lambda **kwargs: transport,
            assert_current=lambda: assert_dispatched_invocation(envelope, harness.store),
        ),
    )
    authority = _adapter(proxy)
    broker = RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=_Admission(),
        authority=authority,
        audit=authority,
        reconciliation=InMemoryReconciliationStore(),
    )
    independent = {
        "caller_principal_id": caller.principal_id,
        "target_principal_id": target.principal_id,
        "caller_domain_id": caller_domain.domain_id,
        "caller_boot_epoch": caller_domain.boot_epoch,
        "target_domain_id": target_domain.domain_id,
        "target_boot_epoch": target_domain.boot_epoch,
        "profile_id": "profile-1",
        "activation_id": "activation-1",
        "activation_digest": activation_digest,
        "plan_digest": _digest("plan"),
        "profile_authority_digest": _digest("4"),
        "fencing_token": 7,
        "security_epoch": 1,
        "provider_authority_id": provider.record_id,
        "provider_authority_digest": provider.digest,
        "grant_id": grant.grant_id,
    }
    facts = {
        "profile_id": "profile-1",
        "model_reference": "saved-default",
        "caller_principal_id": caller.principal_id,
        "authority_capture_digest": authority_digest(independent),
        "route_binding": {
            "model_id": "model-a",
            "provider_instance_id": "adapter-a",
            "catalog_provider_instance_id": "catalog-main",
            "catalog_revision": "catalog-r1",
            "pricing_revision": "catalog-r1",
            "pricing": {"input": "1", "output": "2", "currency": "USD"},
        },
    }

    def guard():
        if harness.store.is_revoked("grant", grant.grant_id):
            raise AuthorityDenied("fixture independent reviewer revoked")

    boundary = CapturedReviewerBoundary(facts, authority_digest(facts), guard)

    def bind(payload, boundary_digest, payload_digest):
        session = "session.approval-review." + payload["request_id"]
        harness.store.bind_authenticated_session(
            session_id=session,
            domain=caller_domain,
            channel_digest=caller_domain.authenticated_channel_digest,
            principal_id=caller.principal_id,
        )
        context = replace(
            _context(proxy, request_id=payload["request_id"]),
            activation_digest=activation_digest,
            caller_session_id=session,
            target_backend_digest=backend.status.backend_digest,
        )
        prepared = broker.prepare(
            InvocationFrame(
                contract_id=GENERATE_TARGET[0],
                version_range="==1.0.0",
                operation_id=GENERATE_TARGET[1],
                payload=payload,
            ),
            context,
        )
        cancel = threading.Event()
        reserved = []
        dispatch = V4DispatchSession(
            broker=broker,
            context_for=lambda c, o, s: context,
            effect_scope_for=lambda c, o, p, ctx: scope.to_dict(),
            providers={},
            profile_id="profile-1",
            plan_digest=_digest("plan"),
            profile_revision="fixture",
            activation_id="activation-1",
            security_epoch=1,
            authority_control=authority,
            current_capture_check=guard,
        )

        def before():
            reserved.append(
                authority.reserve_effect(context, prepared.binding, prepared.request_digest)
            )

        def proof():
            stored = harness.store.get_lease(reserved[0].value)
            lease, state = stored
            return {
                "lease_id": lease.lease_id,
                "state": state.value,
                "request_digest": lease.request_digest,
                "payload_digest": payload_digest,
                "boundary_digest": boundary_digest,
                "session_id": session,
                "expires_at": lease.expires_at,
                "reviewer_prepared_snapshot": review_transport_snapshot(prepared),
            }

        return ReviewerGenerateLease(
            dispatch,
            session,
            caller.principal_id,
            boundary_digest,
            payload_digest,
            prepared.deadline_monotonic,
            cancel,
            None,
            guard,
            before,
            guard,
            proof,
            lambda: None,
        )

    port = CanonicalReviewerGeneratePort(
        boundary, bind, clock=harness.clock, evidence_validity_seconds=evidence_validity_seconds
    )
    verify = CommittedReviewerTransportAuthenticator(
        harness.store, boundary, independent, guard, clock=harness.clock
    )
    reviewer = ExactOperationReviewAdapter(
        port,
        boundary,
        turn_id=turn_id,
        evidence_factory=CapturedReviewEvidence,
        authenticate_transport=verify,
        clock=harness.clock,
    )
    return SimpleNamespace(
        facts=facts,
        boundary=boundary,
        reviewer=reviewer,
        verifier=verify,
        port=port,
        transport=transport,
        broker=broker,
        kernel=kernel,
        independent_capture=independent,
    )
