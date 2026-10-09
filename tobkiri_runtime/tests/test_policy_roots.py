"""Root references always repeat actual proof and never resurrect revoked state."""

from types import SimpleNamespace as NS

import pytest

from core_runtime.saved_tool_policy_roots_v4 import SavedToolPolicyRootsV4 as CLASS


def test_restored_revoked_root_cannot_trigger_fresh_native_selection():
    context = NS(
        root=NS(
            caller_principal=NS(value="owner"),
            caller_session_id="session",
            request_id="saved-request",
        ),
        mode="full",
        capture_digest="digest",
        conversation_id="conversation",
        turn_id="turn",
        workspace_id="workspace",
        workspace_binding=NS(canonical_root="/owned"),
        assert_current=lambda: None,
    )

    def restore(invocation, ctx):
        raise PermissionError("native root revoked")

    def create(invocation, ctx):
        pytest.fail("revoked receipt cannot be replaced automatically")

    roots = CLASS(capture_context=lambda inv: context, create=create, restore=restore)
    with pytest.raises(PermissionError, match="root revoked"):
        roots.capture_current(object(), context.root)


def test_root_reference_repeats_current_committed_proof_each_operation():
    current = {"revoked": False}

    def guard():
        if current["revoked"]:
            raise PermissionError("native root revoked")

    context = NS(
        root=NS(
            caller_principal=NS(value="owner"),
            caller_session_id="session",
            request_id="saved-request",
        ),
        mode="agent",
        capture_digest="digest",
        conversation_id="conversation",
        turn_id="turn",
        workspace_id="workspace",
        workspace_binding=NS(canonical_root="/owned"),
        assert_current=lambda: None,
    )
    capture = NS(
        mode="agent",
        capture_digest="digest",
        turn_id="turn",
        workspace_root="/owned",
        assert_current=guard,
    )
    root = NS(capture_current=lambda inv, owner: capture)
    calls = []
    roots = CLASS(
        capture_context=lambda inv: context,
        create=lambda inv, ctx: calls.append(ctx) or root,
        restore=lambda inv, ctx: None,
    )
    assert roots.capture_current(object(), context.root) is capture
    assert roots.capture_current(object(), context.root) is capture
    assert len(calls) == 1
    current["revoked"] = True
    with pytest.raises(PermissionError, match="root revoked"):
        roots.capture_current(object(), context.root)
    assert len(calls) == 1
