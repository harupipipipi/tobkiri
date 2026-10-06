"""Verify private selected-root inheritance and owned file live binding."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core_runtime import policy_invocation_v4

MODULE = vars(policy_invocation_v4)
from core_runtime import owned_file_approval_v4

OWNED = vars(owned_file_approval_v4)


def fixtures():
    registry = MODULE["PolicyInvocationRegistry"]()
    state = {"revoked": False}

    def proof():
        if state["revoked"]:
            raise PermissionError("native root revoked")

    policy = NS(mode="full", assert_current=proof)
    context = NS(request_id="outer", caller_session_id="session")
    outer = NS(context=context, request_digest="exact", lease=object())
    scope = NS(envelope=outer, parent=None, assert_current=proof)
    child = NS(
        assert_current=proof,
        parent_invocation=scope,
        envelope=NS(context=NS(request_id="child"), request_digest="child-digest", lease=object()),
    )
    return registry, policy, context, child, state


def test_authentic_private_policy_inherits_with_actual_child_parent_opaque_lease():
    registry, policy, context, child, state = fixtures()
    with registry.register(context, "exact", policy, policy.assert_current):
        inherited = registry.capture_current(child)
        assert inherited.policy is policy
        assert inherited.parent_lease is child.envelope.lease
        assert not hasattr(inherited, "approved")
        inherited.assert_current()
        state["revoked"] = True
        with pytest.raises(PermissionError, match="root revoked"):
            inherited.assert_current()


def test_policy_inheritance_rejects_changed_ancestry_digest_and_late_worker():
    registry, policy, context, child, state = fixtures()
    with registry.register(context, "exact", policy, policy.assert_current):
        inherited = registry.capture_current(child)
        child.parent_invocation.envelope.request_digest = "rebound"
        with pytest.raises(PermissionError, match="capture changed"):
            registry.capture_current(child)
        child.parent_invocation.envelope.request_digest = "exact"
    with pytest.raises(PermissionError, match="drained"):
        inherited.assert_current()
    assert registry.capture_current(child) is None


def test_policy_inheritance_has_no_wire_selection_id_fallback_or_cycles():
    registry, policy, context, child, state = fixtures()
    child.envelope.payload = {"selection_id": "fake", "approved": True, "mode": "full"}
    assert registry.capture_current(child) is None
    child.parent_invocation.parent = child.parent_invocation
    with pytest.raises(PermissionError, match="ancestry is invalid"):
        registry.capture_current(child)


def test_owned_file_registration_privately_binds_selected_root_and_rechecks_proof(monkeypatch):
    registry, policy, context, child, state = fixtures()
    # The original helper's actual owner validation is exercised by canonical
    # owned-file tests; here only that call is replaced to isolate inheritance.
    register = OWNED["register_file_tool_request"]
    namespace = register.__globals__
    monkeypatch.setitem(namespace, "authenticated_file_tool_owner", lambda invocation: None)
    monkeypatch.setitem(namespace, "_capture_identity", lambda context: ("actual-capture",))
    with registry.register(context, "exact", policy, policy.assert_current):
        inherited = registry.capture_current(child)
        request = register(
            {"path": "note.txt", "content": "hello"}, child, policy_inheritance=inherited
        )
        assert "policy" not in request and "selection_id" not in request
        found = OWNED["file_request_policy"](request, context)
        assert found is inherited
        state["revoked"] = True
        with pytest.raises(PermissionError, match="root revoked"):
            OWNED["assert_file_request_live"](request, context)
    namespace["_live_requests"].clear()
