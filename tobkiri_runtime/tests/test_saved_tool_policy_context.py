"""Verify source capture of saved request and native workspace identity."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


def load_capture(monkeypatch, root):
    from core_runtime import saved_tool_policy_context_v4 as module

    monkeypatch.setattr(module, "SAVED_TOOL_CAPTURE_FIELDS", ("profile_id", "activation_id"))
    monkeypatch.setattr(
        module, "saved_tool_owner_and_request_scope",
        lambda invocation: (root, invocation.parent_invocation),
    )
    return module.capture_saved_tool_policy_context


def test_saved_capture_rechecks_original_request_and_owned_mount(monkeypatch):
    root = NS(
        profile_id="profile",
        activation_id="activation",
        caller_principal="owner",
        caller_session_id="session",
    )
    capture = load_capture(monkeypatch, root)
    request = {"action_approval_mode": "full", "conversation_id": "conversation", "turn_id": "turn"}
    saved = NS(
        assert_current=lambda: None,
        parent=None,
        envelope=NS(
            contract_id="conversation.saved-turn.v1",
            operation_id="saved_complete",
            payload={"request": request},
        ),
    )
    invocation = NS(assert_current=lambda: None, parent_invocation=saved)
    binding = NS(
        profile_id="profile",
        workspace_id="workspace",
        canonical_root=Path("/owned"),
        mount_revision="revision",
        root_st_dev=1,
        root_st_ino=2,
    )
    selected = {
        "workspace_id": "workspace",
        "canonical_root": "/owned",
        "mount_revision": "revision",
        "root_st_dev": 1,
        "root_st_ino": 2,
    }
    captured = capture(
        invocation,
        profile_id="profile",
        read_conversation=lambda inv, cid: {"id": cid, "metadata": {"workspace_id": "workspace"}},
        resolve_workspace=lambda profile, wid: binding,
        selected_workspace=lambda: selected,
    )
    assert captured.mode == "full" and captured.workspace_binding is binding
    request["action_approval_mode"] = "agent"
    with pytest.raises(PermissionError, match="original capture changed"):
        captured.assert_current()
    request["action_approval_mode"] = "full"
    selected["root_st_ino"] = 3
    with pytest.raises(PermissionError, match="not Host selected"):
        captured.assert_current()


@pytest.mark.parametrize(
    "message",
    [
        "grant secret-token",
        "reviewer missing secret",
        "workspace /private/path",
        "native denied private-proof",
        "root revoked secret",
    ],
)
def test_policy_stop_has_existing_error_shape_and_never_leaks_details(message):
    from core_runtime.saved_tool_stop_v4 import saved_tool_policy_stop_result as stop

    result = stop(PermissionError(message))
    assert set(result) == {"result", "is_error", "widget"}
    assert result["is_error"] is True and result["widget"] is None
    public = json.loads(result["result"])["error"]
    assert public["code"].startswith("ACTION_APPROVAL_")
    assert message not in result["result"]
    assert "private" not in result["result"] and "secret" not in result["result"]


def test_only_exact_authenticated_review_error_can_present_danger_reason(monkeypatch):
    from tobkiri_host.policy_review_errors import AuthenticatedPolicyReviewDenied as error_type
    from core_runtime.saved_tool_stop_v4 import saved_tool_policy_stop_result as stop

    public = json.loads(stop(error_type("既存ファイルを削除する操作です。"))["result"])["error"]
    assert public["code"] == "ACTION_APPROVAL_AGENT_REVIEW_DENIED"
    assert "既存ファイルを削除" in public["message"]
    wrapped = RuntimeError("provider execution failed")
    wrapped.__cause__ = error_type("削除する操作です。")
    assert (
        json.loads(stop(wrapped)["result"])["error"]["code"]
        == "ACTION_APPROVAL_AGENT_REVIEW_DENIED"
    )
    forged = PermissionError("agent review denied private raw payload")
    forged.safe_public_reason = "FORGED REASON"
    assert "FORGED REASON" not in stop(forged)["result"]


def test_native_boundary_rejects_new_mount_at_same_path_without_broker_io(monkeypatch):
    root = NS(
        profile_id="profile",
        activation_id="activation",
        caller_principal=NS(value="owner"),
        caller_session_id="session",
    )
    capture_fn = load_capture(monkeypatch, root)
    namespace = capture_fn.__globals__
    guard = namespace["assert_saved_native_policy_boundary"]
    saved = NS(
        assert_current=lambda: None,
        parent=None,
        envelope=NS(
            contract_id="conversation.saved-turn.v1",
            operation_id="saved_complete",
            payload={
                "request": {
                    "conversation_id": "conversation",
                    "turn_id": "turn",
                    "action_approval_mode": "full",
                }
            },
        ),
    )
    invocation = NS(assert_current=lambda: None, parent_invocation=saved)
    binding = NS(
        workspace_id="workspace",
        canonical_root=Path("/owned"),
        mount_revision="revision",
        root_st_dev=1,
        root_st_ino=2,
    )
    receipt = {
        "workspace_id": "workspace",
        "canonical_root": "/owned",
        "mount_revision": "revision",
        "root_st_dev": 1,
        "root_st_ino": 2,
    }
    native = {
        "owner_principal": "owner",
        "owner_session": "session",
        "context": {"profile_id": "profile", "activation_id": "activation"},
        "conversation": "conversation",
        "turn": "turn",
        "mode": "full",
        "workspace": "/owned",
        "delegation_boundary": {"routes": [{"workspace_receipt": dict(receipt)}]},
    }
    guard(
        invocation,
        root,
        native,
        workspace_id="workspace",
        resolve_workspace=lambda profile, wid: binding,
        selected_workspace=lambda: receipt,
    )
    binding.root_st_ino = 3
    receipt["root_st_ino"] = 3
    with pytest.raises(PermissionError, match="original workspace receipt changed"):
        guard(
            invocation,
            root,
            native,
            workspace_id="workspace",
            resolve_workspace=lambda profile, wid: binding,
            selected_workspace=lambda: receipt,
        )
