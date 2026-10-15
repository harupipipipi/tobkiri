from __future__ import annotations

import time

import pytest

from tobkiri_host.chat_approval_continuation import (
    ChatApprovalContinuationController,
)
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import ChatApprovalContinuationCommand


def _context(*, session: str = "session-1") -> RequestContext:
    digest = "sha256:" + "a" * 64
    return RequestContext(
        request_id="outer-request",
        trace_id="trace-1",
        caller_principal=OpaqueAuthorityRef("shell.tauri.default"),
        profile_id="defaults",
        activation_id="activation-1",
        activation_digest=digest,
        plan_digest=digest,
        security_epoch=1,
        caller_session_id=session,
        caller_domain_id="domain-shell",
        caller_boot_epoch=1,
        target_domain_id="domain-continuation",
        target_boot_epoch=1,
        target_backend_digest=digest,
        profile_authority_digest=digest,
        fencing_token=1,
        handle_namespace="handles",
        profile_revision=digest,
    )


def _command(**patch) -> ChatApprovalContinuationCommand:
    values = {
        "context": _context(),
        "request_id": "apr-1",
        "conversation_id": "conversation-1",
        "turn_id": "turn-1",
        "presentation_owner_principal_id": "shell.tauri.default",
        "presentation_owner_session_id": "session-1",
        "ui_operator": {"signed": True},
    }
    values.update(patch)
    return ChatApprovalContinuationCommand(**values)


def _binding(turn_id: str = "") -> dict[str, str]:
    return {
        "request_id": "apr-1",
        "conversation_id": "conversation-1",
        "turn_id": turn_id,
        "operation": "computer.click",
        "args_hash": "b" * 64,
        "tool_name": "computer_use",
        "tool_call_id": "call-1",
        "profile_id": "defaults",
    }


def _packet(**patch) -> dict[str, object]:
    packet = {
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "operation_id": "turn-1",
        "request_id": "apr-1",
    }
    packet.update(patch)
    return packet


def test_host_continuation_retains_token_and_claims_once():
    resumes = []
    controller = ChatApprovalContinuationController(
        approve=lambda *_args: {
            "request_id": "apr-1",
            "approved": True,
            "status": "approved",
            "token": "host-secret",
            "expires_at": int(time.time()) + 60,
            "binding": _binding(),
        },
        resume=lambda binding, token, conversation: resumes.append(
            (binding, token, conversation)
        ) or {"resumed": True, "tool": "computer_use",
              "terminal_event": "tool_call_completed"},
    )
    approved = controller.approve_chat_continuation(_command())
    assert "token" not in approved
    assert approved["turn_id"] == "turn-1"
    assert approved["operation_id"] == "turn-1"
    assert approved["terminal"] is None
    resume = _command(ui_operator=None, resume_id=approved["resume_id"])

    assert controller.resume_chat_continuation(resume) == {
        "resumed": True,
        "tool": "computer_use",
        "terminal_event": "tool_call_completed",
        **_packet(),
        "terminal": {
            **_packet(),
            "status": "completed",
            "result_reference": {
                "tool": "computer_use",
                "terminal_event": "tool_call_completed",
            },
            "error": None,
        },
    }
    assert resumes == [(_binding(), "host-secret", "conversation-1")]
    with pytest.raises(PermissionError):
        controller.resume_chat_continuation(resume)


@pytest.mark.parametrize(
    "patch",
    [
        {"conversation_id": "foreign-conversation"},
        {"turn_id": "foreign-turn"},
        {"turn_id": ""},
        {"context": _context(session="foreign-session")},
        {"presentation_owner_session_id": "foreign-session"},
    ],
)
def test_host_continuation_rejects_foreign_binding(patch):
    controller = ChatApprovalContinuationController(
        approve=lambda *_args: {
            "request_id": "apr-1",
            "approved": True,
            "status": "approved",
            "token": "host-secret",
            "expires_at": int(time.time()) + 60,
            "binding": _binding(),
        },
        resume=lambda *_args: pytest.fail("foreign continuation executed"),
    )
    approved = controller.approve_chat_continuation(_command())
    with pytest.raises(PermissionError):
        controller.resume_chat_continuation(
            _command(ui_operator=None, resume_id=approved["resume_id"], **patch)
        )


def test_host_continuation_binds_saved_turn_recorded_at_approve():
    controller = ChatApprovalContinuationController(
        approve=lambda *_args: {
            "request_id": "apr-1",
            "approved": True,
            "status": "approved",
            "token": "host-secret",
            "expires_at": int(time.time()) + 60,
            "binding": _binding(turn_id="turn-1"),
        },
        resume=lambda *_args: {"resumed": True},
    )

    approved = controller.approve_chat_continuation(_command())
    assert approved["turn_id"] == "turn-1"
    with pytest.raises(PermissionError):
        controller.resume_chat_continuation(
            _command(ui_operator=None, resume_id=approved["resume_id"],
                     turn_id="foreign-turn")
        )

    with pytest.raises(PermissionError):
        controller.approve_chat_continuation(_command(turn_id="foreign-turn"))


def test_host_continuation_rejects_stale_approval():
    controller = ChatApprovalContinuationController(
        approve=lambda *_args: {
            "request_id": "apr-1",
            "approved": True,
            "status": "approved",
            "token": "host-secret",
            "expires_at": int(time.time()) - 1,
            "binding": _binding(),
        },
        resume=lambda *_args: pytest.fail("stale continuation executed"),
    )
    with pytest.raises(PermissionError):
        controller.approve_chat_continuation(_command())
