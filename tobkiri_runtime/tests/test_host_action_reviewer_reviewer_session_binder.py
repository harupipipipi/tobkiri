"""Composition seam checks; real Store/Broker proof is tested separately."""

from types import SimpleNamespace as NS
import threading
import time
import pytest
import core_runtime.bootstrap.action_review_v4.reviewer_session_binder as binder
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.action_review_v4.canonical_reviewer_port import GENERATE_TARGET


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
            self.kwargs = kwargs

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
