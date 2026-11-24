from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace


DEFAULTSPACK_ROOT = (
    Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
)
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.coding import approval_approve, approval_resume  # noqa: E402
from domain.safety import approval  # noqa: E402


def _approved_request(**patch):
    arguments = {"action": "click", "x": 10, "y": 20}
    request = {
        "request_id": "apr-native-1",
        "status": "approved",
        "operation": "computer.click",
        "args_hash": approval.hash_arguments(arguments),
        "expires_at": int(time.time()) + 300,
        "profile_id": "defaults",
        "details": {
            "tool_name": "computer_use",
            "function_id": "computer.click",
            "conversation_id": "conversation-1",
            "tool_call_id": "tool-call-1",
            "profile_id": "defaults",
            "arguments": arguments,
        },
    }
    request.update(patch)
    return request


def test_native_resume_handle_is_exact_bound_and_claimed_once():
    request = _approved_request()
    handle = approval.register_native_resume_handle(request, "secret-token")

    assert approval.claim_native_resume_handle(handle, request) == "secret-token"
    assert approval.claim_native_resume_handle(handle, request) == ""


def test_native_resume_handle_rejects_changed_request_binding():
    request = _approved_request(request_id="apr-native-binding")
    handle = approval.register_native_resume_handle(request, "secret-token")
    changed = {
        **request,
        "details": {**request["details"], "conversation_id": "conversation-2"},
    }

    assert approval.claim_native_resume_handle(handle, changed) == ""
    assert approval.claim_native_resume_handle(handle, request) == ""


def test_native_approval_hides_token_and_returns_bound_resume_handle(monkeypatch):
    request = _approved_request(request_id="apr-native-approve")
    monkeypatch.setattr(approval_approve, "get_approval_request", lambda _id: request)
    monkeypatch.setattr(approval_approve, "verify_coding_ui_operator", lambda *_a, **_k: None)
    monkeypatch.setattr(
        approval_approve,
        "approve",
        lambda *_a, **_k: {
            "request_id": request["request_id"],
            "status": "approved",
            "approved": True,
            "token": "server-secret",
        },
    )
    monkeypatch.setattr(approval_approve, "record_approval", lambda *_a, **_k: None)

    result = approval_approve.run(
        {
            "approval_request_id": request["request_id"],
            "continuation_conversation_id": "conversation-1",
            "ui_operator": {"signed": True},
        }
    )

    assert result["status"] == "ok"
    assert result["data"]["resume_id"].startswith("native_resume_")
    assert "token" not in result["data"]


def test_native_approval_rejects_foreign_conversation(monkeypatch):
    request = _approved_request(request_id="apr-native-foreign")
    monkeypatch.setattr(approval_approve, "get_approval_request", lambda _id: request)
    monkeypatch.setattr(approval_approve, "verify_coding_ui_operator", lambda *_a, **_k: None)
    approve_calls = []
    monkeypatch.setattr(
        approval_approve,
        "approve",
        lambda *_a, **_k: approve_calls.append(True),
    )
    monkeypatch.setattr(approval_approve, "record_approval", lambda *_a, **_k: None)

    result = approval_approve.run(
        {
            "approval_request_id": request["request_id"],
            "continuation_conversation_id": "conversation-2",
            "ui_operator": {"signed": True},
        }
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "APPROVAL_RESUME_MISMATCH"
    assert approve_calls == []


def test_native_browser_resume_uses_only_stored_tool_and_arguments(monkeypatch):
    request = _approved_request(request_id="apr-native-browser")
    handle = approval.register_native_resume_handle(request, "one-shot-token")
    monkeypatch.setattr(approval_resume.approval, "get_approval_request", lambda _id: request)
    monkeypatch.setattr(
        approval_resume.approval,
        "verify_execution_token",
        lambda *_a, **_k: SimpleNamespace(
            valid=True,
            request_id=request["request_id"],
            message="",
            code="",
        ),
    )
    captured = {}

    def execute(function_id, payload, context):
        captured.update(function_id=function_id, payload=payload, context=context)
        request["status"] = "consumed"
        return {"status": "ok", "data": {"clicked": True}}

    monkeypatch.setattr(approval_resume, "run_defaultspack_function", execute)
    result = approval_resume.run(
        {
            "request_id": request["request_id"],
            "resume_id": handle,
            "conversation_id": "conversation-1",
        },
        {},
    )

    assert result["status"] == "ok"
    assert captured["function_id"] == "computer_use"
    assert captured["payload"] == {
        **request["details"]["arguments"],
        "approval_token": "one-shot-token",
    }
    assert approval.claim_native_resume_handle(handle, request) == ""
