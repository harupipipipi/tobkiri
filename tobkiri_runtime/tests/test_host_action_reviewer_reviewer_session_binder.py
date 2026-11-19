"""Composition seam checks; real Store/Broker proof is tested separately."""

from dataclasses import replace
from types import SimpleNamespace as NS
import threading
import time
import pytest
import core_runtime.bootstrap.action_review_v4.reviewer_session_binder as binder
from core_runtime.authority.v4 import AuthorityDenied, LeaseState
from core_runtime.bootstrap.action_review_v4.canonical_reviewer_port import GENERATE_TARGET
from core_runtime.bootstrap.action_review_v4.reviewer_capture_factory import (
    QUOTE_TARGET,
    SETTINGS_TARGET,
)
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _envelope
from tobkiri_protocol.data_codec import digest_payload


def inputs(monkeypatch):
    calls = []
    caller = NS(principal_ref=NS(value="policy-caller"))
    target = NS(principal_ref=NS(value="gateway-target"))
    edge = NS(
        authority_mode="profile_grant",
        caller=NS(principal_id="policy-caller"),
        target=NS(principal_id="gateway-target"),
        binding_key=("policy-fn", *GENERATE_TARGET),
    )
    context = NS()

    class Capture:
        def __init__(self, dispatch, **kwargs):
            self.kwargs = {**kwargs, "read_dispatch": dispatch}

        def capture(self):
            return self.kwargs

    monkeypatch.setattr(binder, "CanonicalReviewerCaptureFactory", Capture)
    monkeypatch.setattr(binder, "V4DispatchSession", lambda **kwargs: NS(**kwargs))
    params = dict(
        invocation=NS(
            presentation_owner_principal_id="owner",
            presentation_owner_session_id="owner-session",
            envelope=NS(),
        ),
        saved_context=NS(),
        broker=NS(
            prepare=lambda frame, ctx: NS(
                binding=NS(),
                request_digest="broker-digest",
                deadline_monotonic=time.monotonic() + 50,
            )
        ),
        authority=NS(),
        authority_store=NS(),
        catalog=NS(),
        profile_id="profile",
        policy_caller_binding=caller,
        generate_binding=target,
        signed_generate_edge=edge,
        bind_nested_session=lambda *args, **kwargs: calls.append(("bind", args)) or "resolved",
        release_nested_session=lambda *args: calls.append(("release", args)),
        context_for=lambda *args: calls.append(("context", args)) or context,
        effect_scope_for=lambda *args: {"exact": "scope"},
        capture_invocation_scope=lambda envelope: NS(assert_current=lambda: None),
        nested_cancellation_proof_for=lambda *args: object(),
        assert_current_capture=lambda: None,
        local_configuration_revision=lambda: ("model", 1),
        local_route_is_current=lambda route: True,
        capture_independent_authority=lambda ctx, edge: {
            "caller_principal_id": "policy-caller",
            "target_principal_id": "gateway-target",
            "profile_id": "profile",
        },
        dispatch_capture={
            "plan_digest": "plan",
            "profile_revision": "revision",
            "activation_id": "activation",
            "security_epoch": 1,
        },
        parent_deadline_monotonic=time.monotonic() + 60,
        parent_cancellation=threading.Event(),
    )
    return params, calls, context


def test_fixed_request_context_and_fresh_session_release(monkeypatch):
    params, calls, context = inputs(monkeypatch)
    capture = binder.build_reviewer_capture(**params)
    lease = capture["bind_generate"]({"request_id": "request"}, "boundary", "payload-digest")
    assert lease.dispatch.context_for("contract", "op", "session") is context
    assert lease.dispatch.effect_scope_for("contract", "op", {}, context) == {"exact": "scope"}
    assert len([c for c in calls if c[0] == "context"]) == 2
    lease.release()
    assert len([c for c in calls if c[0] == "release"]) == 2
    assert lease.session_id.startswith("session.approval-review.")
    assert lease.payload_digest == "payload-digest"


def test_missing_nested_cancellation_proof_releases_session(monkeypatch):
    params, calls, _ = inputs(monkeypatch)
    params["nested_cancellation_proof_for"] = lambda *args: None
    capture = binder.build_reviewer_capture(**params)
    with pytest.raises(AuthorityDenied, match="parent cancellation"):
        capture["bind_generate"]({}, "boundary", "payload-digest")
    assert len([c for c in calls if c[0] == "release"]) == 2


def test_cancelled_parent_does_not_bind_or_prepare(monkeypatch):
    params, calls, _ = inputs(monkeypatch)
    params["parent_cancellation"].set()
    with pytest.raises(AuthorityDenied, match="cancelled"):
        binder.build_reviewer_capture(**params)
    assert not calls


def test_wrong_independent_signed_edge_fails_before_session(monkeypatch):
    params, calls, _ = inputs(monkeypatch)
    params["signed_generate_edge"].authority_mode = "interactive_only"
    with pytest.raises(AuthorityDenied, match="signed reviewer"):
        binder.build_reviewer_capture(**params)
    assert not calls


def test_reviewer_dispatch_registration_binds_live_gateway_request_and_saved_parent(
    monkeypatch,
):
    params, _, _ = inputs(monkeypatch)
    registry = binder.ReviewerDispatchRegistry()
    parent = CapturedInvocationScopeV4(_envelope(), lambda: None)
    sessions = {}
    observed = []
    prepared = []
    cancel_parent_during_dispatch = False

    def bind_session(name, caller, owner, *, parent_invocation):
        assert caller == "policy-caller" and owner == ("owner", "owner-session")
        assert parent_invocation is parent
        resolved = f"{name}.policy-caller.activation"
        assert registry.lookup(resolved) is None
        sessions[name] = resolved
        return resolved

    def release_session(name, resolved):
        assert registry.lookup(resolved) is None
        assert sessions.pop(name) == resolved

    def context_for(_contract, _operation, name):
        return replace(parent.envelope.context, caller_session_id=sessions[name])

    def prepare(frame, context):
        assert registry.lookup(context.caller_session_id) is None
        result = NS(
            binding=NS(),
            request_digest=f"request-{len(prepared)}",
            deadline_monotonic=time.monotonic() + 10,
            normalized_payload=dict(frame.payload),
        )
        prepared.append(result)
        return result

    class Dispatch:
        def __init__(self, **kwargs):
            self.context_for = kwargs["context_for"]

        def invoke(self, contract, operation, payload, **kwargs):
            pair = contract, operation
            context = self.context_for(contract, operation, payload["_session_id"])
            resolved = context.caller_session_id
            assert resolved == sessions[payload["_session_id"]]
            assert registry.lookup(resolved + ".unregistered") is None
            identity = registry.lookup(resolved)
            if pair == SETTINGS_TARGET:
                assert identity is None
            else:
                assert type(identity) is binder.ReviewerDispatchIdentity
                assert identity.context is context
                assert identity.saved_parent is parent
                assert identity.resolved_session_id == resolved
                assert identity.target_pair == pair
                assert identity.payload_digest == digest_payload(prepared[-1].normalized_payload)
                assert identity.request_digest == prepared[-1].request_digest
            observed.append((pair, resolved))
            if cancel_parent_during_dispatch:
                params["parent_cancellation"].set()
            kwargs["execution_guard"]()
            kwargs["before_dispatch"]()
            return {"kind": "verified-reviewer-reply"}

    monkeypatch.setattr(binder, "V4DispatchSession", Dispatch)
    params.update(
        reviewer_dispatches=registry,
        invocation=NS(
            envelope=parent.envelope,
            presentation_owner_principal_id="owner",
            presentation_owner_session_id="owner-session",
        ),
        bind_nested_session=bind_session,
        release_nested_session=release_session,
        capture_invocation_scope=lambda envelope: parent,
        context_for=context_for,
        broker=NS(prepare=prepare),
        authority=NS(reserve_effect=lambda *args: NS(value=prepared[-1].request_digest)),
        authority_store=NS(get_lease=lambda key: (
            NS(request_digest=key, caller=NS(principal_id="policy-caller"),
               profile_id="profile", lease_id=key, expires_at=time.time() + 10),
            LeaseState.COMMITTED,
        )),
    )
    monkeypatch.setattr(binder, "review_transport_snapshot", lambda prepared: {})
    capture = binder.build_reviewer_capture(**params)
    assert not sessions

    for pair, data in (
        (SETTINGS_TARGET, {"profile_id": "profile"}),
        (QUOTE_TARGET, {"delivery_mode": "buffered"}),
        (QUOTE_TARGET, {"delivery_mode": "buffered"}),
    ):
        value = capture["read_dispatch"].invoke(*pair, {**data, "_session_id": "external"})
        capture["authenticate_read"](pair, value)
        assert registry.lookup(observed[-1][1]) is None
        assert not sessions

    lease = capture["bind_generate"]({"request_id": "fixture"}, "boundary", "digest")
    resolved = sessions[lease.session_id]
    assert registry.lookup(resolved) is None
    expected_payload = {"request_id": "fixture", "_session_id": lease.session_id}
    cancel_parent_during_dispatch = True
    try:
        for pair, payload in (
            (QUOTE_TARGET, expected_payload),
            (GENERATE_TARGET, {**expected_payload, "_session_id": lease.session_id + ".forged"}),
            (GENERATE_TARGET, {**expected_payload, "request_id": "rebound"}),
        ):
            with pytest.raises(AuthorityDenied, match="prepared dispatch changed"):
                lease.dispatch.invoke(*pair, payload)
            assert registry.lookup(resolved) is None
        with pytest.raises(AuthorityDenied, match="parent expired or cancelled"):
            lease.dispatch.invoke(
                *GENERATE_TARGET, expected_payload,
                execution_guard=lease.execution_guard, before_dispatch=lease.before_dispatch,
            )
        assert registry.lookup(resolved) is None
    finally:
        lease.release()
    assert not sessions
    assert [pair for pair, _ in observed] == [
        SETTINGS_TARGET, QUOTE_TARGET, QUOTE_TARGET, GENERATE_TARGET,
    ]
    assert len({session for _, session in observed}) == len(observed)


@pytest.mark.parametrize("pair,sibling", [
    (QUOTE_TARGET, GENERATE_TARGET), (GENERATE_TARGET, QUOTE_TARGET),
])
def test_reviewer_dispatch_identity_rejects_lookalike_gateway_envelopes(pair, sibling):
    registry = binder.ReviewerDispatchRegistry()
    saved_parent = CapturedInvocationScopeV4(_envelope(), lambda: None)
    fixture = _envelope()
    resolved_session = "session.approval-review.fixture.policy.activation"
    gateway = replace(
        fixture,
        context=replace(fixture.context, caller_session_id=resolved_session),
        contract_id=pair[0],
        operation_id=pair[1],
        payload={"requirements": {"request_surface": "approval-review", "tool_calling": False}},
    )
    identity = binder.ReviewerDispatchIdentity(
        gateway.context, pair, digest_payload(dict(gateway.payload)),
        gateway.request_digest, resolved_session, saved_parent,
    )

    assert registry.lookup(resolved_session) is None
    with registry.retain(identity):
        assert registry.lookup(resolved_session) is identity
        assert identity.matches_gateway(gateway)
        assert not identity.matches_gateway(replace(gateway, context=replace(gateway.context)))
        assert not identity.matches_gateway(replace(
            gateway, contract_id=sibling[0], operation_id=sibling[1],
        ))
        assert not identity.matches_gateway(replace(gateway, request_digest="rebound"))
        assert not identity.matches_gateway(replace(
            gateway, payload={**gateway.payload, "tools": []},
        ))
        assert registry.lookup("session.approval-review.lookalike.policy.activation") is None
        with pytest.raises(AuthorityDenied, match="already active"), registry.retain(identity):
            pytest.fail("duplicate reviewer registration was accepted")
    assert registry.lookup(resolved_session) is None
