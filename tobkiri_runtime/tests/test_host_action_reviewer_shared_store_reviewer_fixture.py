"""Actual root review method plus independent real generate Broker/Store fixture."""

from dataclasses import replace
import time
from types import SimpleNamespace
import pytest
from core_runtime.bootstrap.action_review_v4 import reviewer_session_binder as binder
from core_runtime.bootstrap.action_review_v4.canonical_reviewer_port import (
    CanonicalReviewerGeneratePort,
    GENERATE_TARGET,
)
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    HEALTH_CONTRACT,
    HEALTH_GENERATE_OPERATION,
)
from tests.test_authority_v4_lifecycle import _Harness, _digest
from core_runtime.host_contract import bind_host_contract
from tests.test_operation_cancellation import _envelope
from tests.test_interactive_approval_v4 import _CONTRACT
from core_runtime.authority.v4 import AuthorityScope, AuthorityDenied
from core_runtime.authority.policy_exact_grant import RetainedExactOperation, CapturedReviewEvidence
from tobkiri_host.policy_exact_authority_adapter import CommittedPolicyDerivationRoot
from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.operation_cancellation import OwnedCancellationHandles, nested_cancellation_proof_for
from tests.fixtures.host_action_reviewer_v4 import build_shared_store_reviewer


def test_actual_root_review_method_uses_separate_real_generate_lease(tmp_path):
    harness = _Harness(tmp_path)
    bundle = build_shared_store_reviewer(harness)
    scope = AuthorityScope(
        "operation.invoke",
        _digest("read-semantics"),
        dimensions={"contract": ("fixture.file",), "operation": ("file.read",)},
        exact_request_digest=_digest("request"),
    )
    context = harness.context(request_digest=_digest("request"), effect_digest=scope.digest)
    retained = RetainedExactOperation(
        {
            "contract_id": "fixture.file",
            "operation_id": "file.read",
            "normalized_payload": {"path": str(tmp_path / "hello.txt")},
            "request_digest": _digest("request"),
        },
        context,
        scope,
        "fixture.file",
        "file.read",
        "read",
        str(tmp_path),
        lambda: None,
    )
    with bind_host_contract(_CONTRACT):
        evidence = CommittedPolicyDerivationRoot.review_exact(
            SimpleNamespace(reviewer=bundle.reviewer), retained
        )
    assert isinstance(evidence, CapturedReviewEvidence)
    assert evidence.outcome == "safe"
    assert evidence.provenance["transport_request_digest"] != evidence.provenance["payload_digest"]
    stored = harness.store.get_lease(evidence.provenance["transport_proof"]["lease_id"])
    assert stored[1].value == "committed"
    assert stored[0].caller.principal_id != harness.caller.principal_id
    assert stored[0].target.principal_id != harness.target.principal_id
    assert stored[0].grant_id == "review-independent-grant"
    assert len(bundle.transport.transports) == 1
    bundle.reviewer.validate_review_evidence(evidence, retained)
    harness.store.revoke(
        target_kind="grant", target_id="review-independent-grant", reason="fixture revoke"
    )
    with pytest.raises(AuthorityDenied):
        bundle.reviewer.validate_review_evidence(evidence, retained)
    harness.store.close()


def test_reviewer_registration_survives_real_broker_dispatch_and_drains(tmp_path, monkeypatch):
    """Exercise the actual binder and Broker with fixture authority and an offline provider."""
    harness = _Harness(tmp_path)
    bundle = build_shared_store_reviewer(harness)
    capture = bundle.independent_capture
    caller = OpaqueAuthorityRef(capture["caller_principal_id"])
    grant = harness.store.get_grant(capture["grant_id"])
    binding = bundle.broker._catalog.resolve_pinned(*GENERATE_TARGET)
    parent_envelope = replace(
        _envelope(),
        context=bundle.bind_context("saved-parent-fixture", "saved-parent"),
        contract_id="conversation.saved-turn.v1",
        operation_id="saved_complete",
        deadline_monotonic=time.monotonic() + 30,
    )
    parent = CapturedInvocationScopeV4(parent_envelope, lambda: None)
    registry = binder.ReviewerDispatchRegistry()
    sessions = {}
    observed = []
    reviewer_health = []
    unavailable = False

    def bind_session(name, principal, _owner, *, parent_invocation):
        assert principal == caller.value and parent_invocation is parent
        sessions[name] = f"{name}.reviewer.activation"
        return sessions[name]

    def release_session(name, resolved):
        assert registry.lookup(resolved) is None
        assert sessions.pop(name) == resolved

    class CaptureFactory:
        def __init__(self, _dispatch, *, bind_generate, **_kwargs):
            self.bind_generate = bind_generate

        def capture(self):
            return self.bind_generate

    monkeypatch.setattr(binder, "CanonicalReviewerCaptureFactory", CaptureFactory)
    dispatch = bundle.broker._dispatch

    def observe(backend, envelope, *args, **kwargs):
        identity = registry.lookup(envelope.context.caller_session_id)
        assert identity is not None and identity.matches_gateway(envelope)
        assert identity.saved_parent is parent
        observed.append(envelope)
        return dispatch(backend, envelope, *args, **kwargs)

    monkeypatch.setattr(bundle.broker, "_dispatch", observe)

    def observe_provider():
        envelope = observed[-1]
        identity = registry.lookup(envelope.context.caller_session_id)
        assert identity is not None and identity.matches_gateway(envelope)

    bundle.transport.on_transport = observe_provider
    invoke = bundle.transport.invoke

    def health(contract, operation, payload, **kwargs):
        if contract == HEALTH_CONTRACT:
            observe_provider()
            assert operation == HEALTH_GENERATE_OPERATION and payload == {}
            reviewer_health.append(observed[-1])
            return {"providers": [{
                "provider_instance_id": "adapter-a",
                "status": "unavailable" if unavailable else "available",
                "observed_at": time.time(),
                "verified": True,
            }]}
        return invoke(contract, operation, payload, **kwargs)

    monkeypatch.setattr(bundle.transport, "invoke", health)
    cancellation = OwnedCancellationHandles().bind(
        group=("fixture", "saved"), role="execute", envelope=parent_envelope,
        owner_principal="owner", owner_session="owner-session", guard=parent.assert_current,
    )
    try:
        with cancellation.track("reviewer-parent"), bind_host_contract(_CONTRACT):
            bind = binder.build_reviewer_capture(
                SimpleNamespace(
                    envelope=parent_envelope,
                    presentation_owner_principal_id="owner",
                    presentation_owner_session_id="owner-session",
                ),
                None,
                broker=bundle.broker,
                authority=bundle.authority,
                authority_store=harness.store,
                catalog=bundle.broker._catalog,
                profile_id=capture["profile_id"],
                policy_caller_binding=SimpleNamespace(principal_ref=caller),
                generate_binding=binding,
                signed_generate_edge=SimpleNamespace(
                    authority_mode="profile_grant", caller=grant.caller, target=grant.target,
                    binding_key=("reviewer", *GENERATE_TARGET),
                ),
                bind_nested_session=bind_session,
                release_nested_session=release_session,
                context_for=lambda _contract, _op, name: bundle.bind_context(sessions[name], name),
                effect_scope_for=lambda *_args: grant.scope.to_dict(),
                capture_invocation_scope=lambda _envelope: parent,
                nested_cancellation_proof_for=nested_cancellation_proof_for,
                assert_current_capture=bundle.boundary.assert_current,
                local_configuration_revision=lambda: (bundle.facts["model_reference"], 1),
                local_route_is_current=lambda _route: True,
                capture_independent_authority=lambda _context, _edge: capture,
                dispatch_capture={**capture, "profile_revision": parent_envelope.context.profile_revision},
                parent_deadline_monotonic=parent_envelope.deadline_monotonic,
                parent_cancellation=parent_envelope.cancellation_requested,
                reviewer_dispatches=registry,
            )
            port = CanonicalReviewerGeneratePort(bundle.boundary, bind, clock=harness.clock)
            request = {
                "model_reference": bundle.facts["model_reference"], "tools": [],
                "messages": [{"role": "user", "content": "fixture"}],
                "request_id": "review-registered-request", "deadline": time.time() + 10,
            }
            assert port(request)["status"] == "ok"
            evidence = port.consume_transport_evidence(request["request_id"])
            proof = evidence["transport_proof"]
            lease, state = harness.store.get_lease(proof["lease_id"])
            assert state.value == "committed"
            assert lease.grant_id == grant.grant_id
            assert len(observed) == len(bundle.transport.transports) == 1
            assert lease.request_digest == observed[0].request_digest
            assert registry.lookup(observed[0].context.caller_session_id) is None
            assert not sessions

            unavailable = True
            with pytest.raises(ProviderExecutionError):
                port({**request, "request_id": "review-unavailable-provider"})
            assert reviewer_health == observed and len(observed) == 2
            assert len(bundle.transport.transports) == 1
            assert not sessions

            harness.store.revoke(target_kind="grant", target_id=grant.grant_id, reason="fixture revoke")
            with pytest.raises(AuthorityDenied):
                port({**request, "request_id": "review-revoked-request"})
            assert len(observed) == 2 and not sessions
    finally:
        bundle.broker.close()
        harness.store.close()
