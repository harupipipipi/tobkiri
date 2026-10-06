"""Exercise the exact finite native resource and Host presentation projection."""

from pathlib import Path
from types import SimpleNamespace as NS
import json

import pytest

from core_runtime import approval_policy_capabilities_v4

HOST = vars(approval_policy_capabilities_v4)
from ecosystem.rumi_host_authority_bridge_pack.runtime import approval_policy_capabilities

FACTORY = vars(approval_policy_capabilities)


def invoke_projection(projection=None, workspace=lambda profile, workspace: NS(native=True)):
    guard_calls = []
    port = HOST["HostApprovalPolicyCapabilitiesV4"](
        profile_id="profile",
        activation_id="activation",
        capture={"actual": "capture"},
        assert_current_capture=lambda: guard_calls.append("capture"),
        workspace_binding=workspace,
        policy_projection=projection,
        conversation_membership=lambda invocation, conversation, workspace: (
            conversation == "conversation" and workspace == "project"
        ),
        clock=lambda: 100.0,
    )
    invocation = NS(
        assert_current=lambda: guard_calls.append("invocation"),
        envelope=NS(context=NS(profile_id="profile", activation_id="activation")),
    )
    return (
        FACTORY["_bind"](NS(action_approval_policy_capabilities_port=port)),
        invocation,
        guard_calls,
    )


def test_capabilities_resource_missing_policy_keeps_only_ask_without_preferences():
    invoke, invocation, guards = invoke_projection()
    result = invoke({"conversation_id": "conversation", "workspace_id": "project"}, invocation)
    assert set(result) == {
        "available_modes",
        "active_mode",
        "reason",
        "profile_id",
        "activation_id",
        "workspace_id",
        "conversation_id",
        "capture_digest",
        "expires_at",
    }
    assert result["available_modes"] == ["ask"]
    assert result["active_mode"] == "ask"
    assert result["expires_at"] == 160.0
    assert result["capture_digest"].startswith("sha256:")
    assert len(guards) >= 4
    assert "native" not in json.dumps(result)


def test_capabilities_resource_uses_actual_projection_and_native_workspace_binding():
    seen = []

    def actual_projection(invocation, conversation, workspace, binding):
        seen.append((conversation, workspace, binding.native))
        return {
            "available_modes": ["ask", "agent", "full"],
            "active_mode": "full",
            "reason": "captured",
        }

    invoke, invocation, _ = invoke_projection(actual_projection)
    result = invoke({"conversation_id": "conversation", "workspace_id": "project"}, invocation)
    assert seen == [("conversation", "project", True)]
    assert result["available_modes"] == ["ask", "agent", "full"]
    assert result["active_mode"] == "full"


def test_capabilities_resource_workspace_failure_disables_elevation():
    def unavailable(profile, workspace):
        raise PermissionError("native mount retired")

    invoke, invocation, _ = invoke_projection(
        lambda *args: pytest.fail("no native workspace"), unavailable
    )
    result = invoke({"conversation_id": "conversation", "workspace_id": "project"}, invocation)
    assert result["available_modes"] == ["ask"]
    assert result["reason"] == "workspace-unavailable"


@pytest.mark.parametrize(
    "projection",
    [
        {"available_modes": ["full"], "active_mode": "full", "reason": "bad"},
        {"available_modes": ["ask", "unknown"], "active_mode": "ask", "reason": "bad"},
        {"available_modes": ["ask", "ask"], "active_mode": "ask", "reason": "bad"},
        {"available_modes": ["ask"], "active_mode": "ask", "reason": "bad", "approved": True},
    ],
)
def test_capabilities_rejects_bad_or_authority_looking_projection(projection):
    invoke, invocation, _ = invoke_projection(lambda *args: projection)
    with pytest.raises(PermissionError, match="projection is invalid"):
        invoke({"conversation_id": "conversation", "workspace_id": "project"}, invocation)


def test_capabilities_resource_denies_rebound_activation_or_payload_flags():
    invoke, invocation, _ = invoke_projection()
    invocation.envelope.context.activation_id = "retired"
    with pytest.raises(PermissionError, match="capture changed"):
        invoke({"conversation_id": "conversation", "workspace_id": "project"}, invocation)
    with pytest.raises(ValueError):
        invoke(
            {"conversation_id": "conversation", "workspace_id": "project", "approved": True},
            invocation,
        )


def test_new_draft_nullable_conversation_reports_support_but_active_ask():
    invoke, invocation, _ = invoke_projection(
        lambda *args: {
            "available_modes": ["ask", "full"],
            "active_mode": "full",
            "reason": "captured",
        }
    )
    result = invoke({"conversation_id": None, "workspace_id": "project"}, invocation)
    assert result["conversation_id"] is None
    assert result["available_modes"] == ["ask", "full"]
    assert result["active_mode"] == "ask"


@pytest.mark.parametrize(
    "payload",
    [
        {"workspace_id": "project"},
        {"conversation_id": "", "workspace_id": "project"},
        {"conversation_id": "forged", "workspace_id": "project"},
        {"conversation_id": None, "workspace_id": None},
    ],
)
def test_capabilities_rejects_missing_empty_unowned_or_fabricated_applicability(payload):
    invoke, invocation, _ = invoke_projection()
    with pytest.raises((ValueError, PermissionError)):
        invoke(payload, invocation)
