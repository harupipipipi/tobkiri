from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from core_runtime.authority.debug_cli_operator import authority_snapshot  # noqa: E402
from core_runtime.authority.models import AuthorityRequest  # noqa: E402
from domain.safety import approval as runtime_approval  # noqa: E402
from domain.safety import debug_cli_operator as runtime_operator  # noqa: E402
from tobkiri import cli  # noqa: E402


class _SseResponse:
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, events):
        self._lines = io.BytesIO(
            b"".join(
                b"data: " + json.dumps(event).encode() + b"\n\n"
                for event in events
            )
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __iter__(self):
        return iter(self._lines)


def test_open_json_preserves_structured_http_error_message(monkeypatch):
    failure = urllib.error.HTTPError(
        "http://127.0.0.1:8767/api/test",
        400,
        "Bad Request",
        {},
        io.BytesIO(json.dumps({"error": {"message": "contract unavailable"}}).encode()),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(cli.CliError, match=r"HTTP 400: contract unavailable"):
        cli._open_json(urllib.request.Request("http://127.0.0.1:8767/api/test"), "Defaultspack")


def _authority_request(**overrides):
    values = {
        "request_id": "auth-1",
        "status": "pending",
        "principal_id": "profile:debug",
        "permission_id": "model.invoke",
        "resource": {"kind": "model", "model_id": "m1"},
        "reason": "debug",
        "risk_level": "high",
        "created_at": "2026-07-29T00:00:00Z",
        "expires_at": "2026-07-29T01:00:00Z",
        "conversation_id": "conversation-1",
        "profile_id": "debug",
        "node_id": "agent.ai",
        "graph_id": "default",
    }
    values.update(overrides)
    return AuthorityRequest(**values)


def test_authority_snapshot_digest_changes_with_exact_resource():
    original = authority_snapshot(_authority_request())
    changed = authority_snapshot(_authority_request(resource={"kind": "model", "model_id": "m2"}))

    assert len(original["digest"]) == 64
    assert len(original["target_digest"]) == 64
    assert original["digest"] != changed["digest"]
    assert original["target_digest"] != changed["target_digest"]


def test_runtime_operator_checks_digest_and_exact_provenance(monkeypatch):
    request = {
        "request_id": "apr-1",
        "status": "pending",
        "args_hash": "a" * 64,
        "operation": "computer.click",
        "expires_at": 2_000_000_000,
        "details": {
            "permission_id": "computer.control",
            "function_id": "computer.click",
            "conversation_id": "conversation-1",
        },
    }

    class Broker:
        def available(self):
            return True

        def verify_debug_cli_operator(self, _operator, *, expected_decision):
            assert expected_decision == "approve"
            return {"ok": True, "verified": True}

    class Store:
        def bind_debug_context(self, _request_id, binding):
            return all(binding.values())

    monkeypatch.setattr(runtime_operator, "get_approval_request", lambda _request_id: request)
    monkeypatch.setattr(
        runtime_operator.ViewerBrokerClient,
        "from_environment",
        classmethod(lambda _cls: Broker()),
    )
    monkeypatch.setattr(runtime_operator, "get_approval_store", lambda: Store())
    operator = {
        "kind": "debug_cli_operator",
        "origin": "launcher_debug_cli",
        "scope": "once",
        "decision": "approve",
        "version": 2,
        "session_id": "session-1",
        "run_id": "run-1",
        "workspace_digest": "b" * 64,
        "pack_id": "defaultspack",
        "profile_id": "debug",
        "lease_epoch": 1,
        "request_id": "apr-1",
        "canonical_arguments_digest": "a" * 64,
        "operation": "computer.click",
        "permission_id": "computer.control",
        "tool": "computer.click",
        "action": "computer.click",
        "conversation_id": "conversation-1",
        "operation_owner": "defaultspack",
        "expires_at": 1_900_000_000,
    }

    assert runtime_operator.verify_debug_cli_decision("apr-1", "a" * 64, operator) == request
    with pytest.raises(runtime_operator.DebugCliOperatorError, match="digest changed"):
        runtime_operator.verify_debug_cli_decision("apr-1", "b" * 64, operator)
    with pytest.raises(runtime_operator.DebugCliOperatorError, match="action mismatch"):
        runtime_operator.verify_debug_cli_decision(
            "apr-1", "a" * 64, {**operator, "action": "computer.type"}
        )


def test_cli_has_only_individual_approval_commands():
    parser = cli._parser()
    parser.parse_args(["debug", "approvals", "list"])
    parser.parse_args(["debug", "approvals", "show", "apr-1"])
    parser.parse_args(
        [
            "debug",
            "approvals",
            "approve",
            "apr-1",
            "--expected-digest",
            "a" * 64,
        ]
    )
    parser.parse_args(["debug", "approvals", "deny", "apr-1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["debug", "approvals", "approve-all"])


def test_cli_resume_returns_after_exact_replayed_tool_completes(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "token"
    token_file.write_text("secret", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setattr(
        cli,
        "_session",
        lambda: {
            "api_token_file": str(token_file),
            "defaultspack_url": "http://127.0.0.1:8767",
        },
    )
    response = _SseResponse(
        [
            {"type": "tool_call_started", "approval_replay": True},
            {
                "type": "tool_call_completed",
                "approval_replay": True,
                "is_error": False,
            },
            {"type": "message_delta", "text": "must not be awaited"},
        ]
    )
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    result = cli._api_resume("conversation-1", {"message": {}})

    assert result == {
        "resumed": True,
        "terminal_event": "tool_call_completed",
        "approval_requested": False,
    }


def test_server_owned_coding_resume_uses_stored_arguments_and_consumes_once(
    monkeypatch,
):
    from blocks.coding import approval_resume

    arguments = {
        "workspace_id": "workspace-1",
        "conversation_id": "conversation-1",
        "path": "proof.txt",
        "content": "proof",
    }
    request = {
        "request_id": "apr-1",
        "status": "approved",
        "operation": "file.write",
        "args_hash": runtime_approval.hash_arguments(arguments),
        "details": {
            "function_id": "coding_file_write",
            "tool_name": "coding_file_write",
            "conversation_id": "conversation-1",
            "arguments": arguments,
        },
    }
    monkeypatch.setattr(
        approval_resume.approval,
        "get_approval_request",
        lambda _request_id: request,
    )
    monkeypatch.setattr(
        approval_resume.approval,
        "resolve_debug_resume_handle",
        lambda _resume_id, _request_id: "one-shot-token",
    )
    monkeypatch.setattr(
        approval_resume.approval,
        "verify_execution_token",
        lambda *_args, **_kwargs: SimpleNamespace(
            valid=True,
            request_id="apr-1",
            message=None,
            code=None,
        ),
    )
    captured = {}

    def execute(function_id, payload, context):
        captured.update(function_id=function_id, payload=payload, context=context)
        request["status"] = "consumed"
        return {"status": "ok", "data": {"written": True}}

    monkeypatch.setattr(approval_resume, "run_defaultspack_function", execute)

    result = approval_resume.run(
        {
            "request_id": "apr-1",
            "resume_id": "resume-1",
            "conversation_id": "conversation-1",
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["resumed"] is True
    assert captured["function_id"] == "coding_file_write"
    assert captured["payload"] == {**arguments, "approval_token": "one-shot-token"}
    assert "one-shot-token" not in json.dumps(result)


def test_server_owned_resume_accepts_exact_pack_approval_tool(monkeypatch):
    from blocks.coding import approval_resume
    from blocks.coding import pack_approve

    arguments = {
        "target_pack_id": "test-pack",
        "snapshot_digest": "a" * 64,
    }
    request = {
        "request_id": "apr-pack",
        "status": "approved",
        "operation": "pack.approve",
        "args_hash": runtime_approval.hash_arguments(arguments),
        "details": {
            "function_id": "coding_pack_approve",
            "conversation_id": "debug-pack-approval",
            "arguments": arguments,
        },
    }
    monkeypatch.setattr(approval_resume.approval, "get_approval_request", lambda _id: request)
    monkeypatch.setattr(
        approval_resume.approval,
        "resolve_debug_resume_handle",
        lambda _resume_id, _request_id: "one-shot-token",
    )
    monkeypatch.setattr(
        approval_resume.approval,
        "verify_execution_token",
        lambda *_args, **_kwargs: SimpleNamespace(
            valid=True, request_id="apr-pack", message=None, code=None
        ),
    )

    def execute(payload, context):
        assert payload == {**arguments, "approval_token": "one-shot-token"}
        request["status"] = "consumed"
        return {"status": "ok", "data": {"approved": True, "verified": True}}

    monkeypatch.setattr(pack_approve, "run", execute)

    result = approval_resume.run(
        {
            "request_id": "apr-pack",
            "resume_id": "resume-pack",
            "conversation_id": "debug-pack-approval",
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["resumed"] is True


def test_cli_pack_request_requires_active_debug_and_calls_server(monkeypatch):
    monkeypatch.setattr(cli, "_debug_binding_query", lambda: {"debug_session_id": "dbg"})
    calls = []
    monkeypatch.setattr(
        cli,
        "_api_request",
        lambda method, path, payload=None, **_kwargs: calls.append((method, path, payload))
        or {"approval_required": True},
    )

    result = cli._pack_approval_request(argparse.Namespace(pack_id="test-pack"))

    assert result["approval_required"] is True
    assert calls == [
        (
            "POST",
            "/api/coding/packs/approval/request",
            {"pack_id": "test-pack"},
        )
    ]


def test_cli_pack_status_uses_path_free_status_route(monkeypatch):
    monkeypatch.setattr(cli, "_debug_binding_query", lambda: {"debug_session_id": "dbg"})
    calls = []
    monkeypatch.setattr(
        cli,
        "_api_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs))
        or {"status": "approved", "approved_and_verified": True},
    )

    result = cli._pack_status(argparse.Namespace(pack_id="test-pack"))

    assert result["approved_and_verified"] is True
    assert calls == [
        (
            "GET",
            "/api/coding/packs/status",
            {"query": {"pack_id": "test-pack"}},
        )
    ]


def test_pack_approve_consumes_token_and_verifies_exact_snapshot(monkeypatch):
    from blocks.coding import pack_approve
    from core_runtime import approval_manager

    observed = {}

    def verify(token, operation, args_hash, *, consume):
        observed.update(
            token=token,
            operation=operation,
            args_hash=args_hash,
            consume=consume,
        )
        return SimpleNamespace(valid=True, request_id="apr-pack", message=None, code=None)

    manager = SimpleNamespace(
        scan_packs=lambda: ["test-pack"],
        approve_if_snapshot=lambda pack_id, digest: SimpleNamespace(
            success=pack_id == "test-pack" and digest == "a" * 64,
            error=None,
        ),
        is_pack_approved_and_verified=lambda pack_id, **_kwargs: (pack_id == "test-pack", None),
    )
    monkeypatch.setattr(pack_approve.approval, "verify_execution_token", verify)
    monkeypatch.setattr(approval_manager, "get_approval_manager", lambda: manager)
    payload = {
        "target_pack_id": "test-pack",
        "snapshot_digest": "a" * 64,
        "approval_token": "one-shot-token",
    }

    result = pack_approve.run(payload, {})

    assert result["status"] == "ok"
    assert result["data"] == {"approved": True, "verified": True}
    assert observed == {
        "token": "one-shot-token",
        "operation": "pack.approve",
        "args_hash": runtime_approval.hash_arguments(payload),
        "consume": True,
    }


def test_cli_session_uses_launcher_owned_run_as_guardian(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token_file = tmp_path / ".desktop_api_token"
    token_file.write_text("test-token", encoding="utf-8")
    stored = []
    monkeypatch.setattr(cli, "_write_session", lambda value: stored.append(value))
    requests = []

    def broker(method, path, payload=None):
        requests.append((method, path, payload))
        if path.endswith("/guardian"):
            return {
                "guardian": {
                    "run_id": "launch-owned",
                    "workspace": str(workspace.resolve()),
                    "pack_id": "defaultspack",
                    "guardian_owned": True,
                    "http_port": 8766,
                    "api_token_file": str(token_file),
                }
            }
        if method == "GET":
            if len([item for item in requests if item[:2] == ("GET", path)]) == 1:
                return {"status": {"state": "disabled"}}
            return {"status": {"state": "armed", "session_id": payload_session_id()}}
        if path.endswith("/request"):
            return {"status": {"state": "pending"}}
        if path.endswith("/start"):
            return {
                "session_secret": "session-secret-" + ("x" * 40),
                "status": {
                    "state": "active",
                    "session_id": payload["session_id"],
                    "lease_epoch": 7,
                    "workspace_digest": "a" * 64,
                    "duration": "permanent",
                    "expires_at": None,
                },
            }
        raise AssertionError(path)

    def payload_session_id():
        request_call = next(item for item in requests if item[1].endswith("/request"))
        return request_call[2]["session_id"]

    monkeypatch.setattr(cli, "_broker_request", broker)
    result = cli._session_start(
        argparse.Namespace(
            workspace=str(workspace),
            run_id="launch-owned",
            pack_id="defaultspack",
            profile_id="debug",
        )
    )

    assert result["status"]["state"] == "active"
    request_payload = next(item[2] for item in requests if item[1].endswith("/request"))
    assert "process_id" not in request_payload
    assert "process_id" not in stored[-1]
    assert stored[-1]["duration"] == "permanent"


def test_cli_refuses_changed_digest_before_requesting_operator(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_request_by_id",
        lambda _request_id: {
            "_approval_source": "runtime",
            "request_id": "apr-1",
            "args_hash": "a" * 64,
        },
    )
    signed = False

    def sign(_request):
        nonlocal signed
        signed = True
        return {}, "a" * 64

    monkeypatch.setattr(cli, "_signed_operator", sign)
    args = argparse.Namespace(
        request_id="apr-1",
        expected_digest="b" * 64,
        decision="approve",
    )

    with pytest.raises(cli.CliError, match="expected digest"):
        cli._approval_decide(args)
    assert signed is False


def test_cli_approve_resumes_exact_conversation_without_returning_token_in_resume_result(
    monkeypatch,
):
    request = {
        "_approval_source": "runtime",
        "request_id": "apr-1",
        "operation": "computer.click",
        "args_hash": "a" * 64,
        "details": {
            "conversation_id": "conversation-1",
            "function_id": "computer_use",
            "action": "computer.click",
            "arguments": {"x": 12, "y": 34},
        },
    }
    monkeypatch.setattr(cli, "_request_by_id", lambda _request_id: request)
    monkeypatch.setattr(cli, "_signed_operator", lambda _request: ({"signed": True}, "a" * 64))
    monkeypatch.setattr(
        cli,
        "_api_request",
        lambda *_args, **_kwargs: {
            "approved": True,
            "request_id": "apr-1",
            "resume_id": "resume-opaque",
        },
    )
    events = []

    def broker(_method, _path, payload=None):
        events.append(("settle", payload["outcome"]))
        return {"ok": True, "settled": True}

    monkeypatch.setattr(cli, "_broker_request", broker)
    captured = {}

    def resume(conversation_id, payload):
        events.append(("resume", conversation_id))
        captured["conversation_id"] = conversation_id
        captured["payload"] = payload
        return {"resumed": True, "terminal_event": "done"}

    monkeypatch.setattr(cli, "_api_resume", resume)
    result = cli._approval_decide(
        argparse.Namespace(
            request_id="apr-1",
            expected_digest="a" * 64,
            decision="approve",
        )
    )

    assert result["resumed"] is True
    assert captured["conversation_id"] == "conversation-1"
    assert (
        captured["payload"]["message"]["metadata"]["approval_followup"]["resume_id"]
        == "resume-opaque"
    )
    assert "approval_token" not in json.dumps(captured["payload"], sort_keys=True)
    assert events == [("settle", "settled"), ("resume", "conversation-1")]


def test_cli_marks_resume_failed_without_settling_first(monkeypatch):
    request = {
        "_approval_source": "runtime",
        "request_id": "apr-1",
        "operation": "file.write",
        "args_hash": "a" * 64,
        "details": {
            "conversation_id": "conversation-1",
            "function_id": "coding_file_write",
            "action": "file.write",
            "arguments": {"path": "proof.txt", "content": "proof"},
        },
    }
    monkeypatch.setattr(cli, "_request_by_id", lambda _request_id: request)
    monkeypatch.setattr(cli, "_signed_operator", lambda _request: ({"signed": True}, "a" * 64))
    monkeypatch.setattr(
        cli,
        "_api_request",
        lambda *_args, **_kwargs: {
            "approved": True,
            "request_id": "apr-1",
            "resume_id": "resume-opaque",
        },
    )
    settlements = []
    monkeypatch.setattr(
        cli,
        "_broker_request",
        lambda _method, _path, payload=None: settlements.append(payload["outcome"])
        or {"ok": True},
    )
    monkeypatch.setattr(
        cli,
        "_api_resume",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cli.CliError("resume unavailable")),
    )

    with pytest.raises(cli.CliError, match="resume unavailable"):
        cli._approval_decide(
            argparse.Namespace(
                request_id="apr-1",
                expected_digest="a" * 64,
                decision="approve",
            )
        )

    assert settlements == ["settled", "resume_failed"]


def test_cli_preserves_resume_error_when_failure_settlement_is_rejected(monkeypatch):
    request = {
        "_approval_source": "runtime",
        "request_id": "apr-1",
        "operation": "file.write",
        "args_hash": "a" * 64,
        "details": {
            "conversation_id": "conversation-1",
            "function_id": "coding_file_write",
            "action": "file.write",
            "arguments": {"path": "proof.txt", "content": "proof"},
        },
    }
    monkeypatch.setattr(cli, "_request_by_id", lambda _request_id: request)
    monkeypatch.setattr(cli, "_signed_operator", lambda _request: ({"signed": True}, "a" * 64))
    monkeypatch.setattr(
        cli,
        "_api_request",
        lambda *_args, **_kwargs: {
            "approved": True,
            "request_id": "apr-1",
            "resume_id": "resume-opaque",
        },
    )
    settlements = []

    def broker(_method, _path, payload=None):
        outcome = payload["outcome"]
        settlements.append(outcome)
        if outcome == "resume_failed":
            raise cli.CliError("debug operator execution was already consumed")
        return {"ok": True}

    monkeypatch.setattr(cli, "_broker_request", broker)
    monkeypatch.setattr(
        cli,
        "_api_resume",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cli.CliError("host authority contract unavailable")
        ),
    )

    with pytest.raises(cli.CliError, match="host authority contract unavailable"):
        cli._approval_decide(
            argparse.Namespace(
                request_id="apr-1",
                expected_digest="a" * 64,
                decision="approve",
            )
        )

    assert settlements == ["settled", "resume_failed"]


def test_authority_accepts_launcher_verified_debug_operator_once(tmp_path, monkeypatch):
    from core_runtime.authority import service as service_module
    from core_runtime.authority.request_store import AuthorityRequestStore
    from core_runtime.authority.service import AuthorityService

    class HmacKey:
        def get_active_key(self):
            return "debug-authority-test-key-" + ("x" * 32)

    monkeypatch.setenv("RUMI_AUTHORITY_MODE", "enforce")
    store = AuthorityRequestStore(tmp_path / "authority", hmac_key_manager=HmacKey())
    service = AuthorityService(request_store=store)
    decision = service.check(
        principal_id="profile:debug",
        permission_id="model.invoke",
        resource={"kind": "model", "provider_id": "test", "model_id": "m1"},
        profile_id="debug",
        conversation_id="conversation-1",
    )
    monkeypatch.setattr(
        service_module,
        "verify_authority_debug_operator",
        lambda request, digest, operator: (
            request.request_id == decision.request_id
            and digest == "d" * 64
            and operator.get("signed") is True,
            "",
            {
                "decision_source": "delegated_debug_cli",
                "human_approved": False,
            },
        ),
    )

    approved = service.approve_request(
        decision.request_id,
        scope="once",
        debug_cli_operator={"signed": True, "decision": "approve"},
        expected_digest="d" * 64,
    )
    replay = service.approve_request(
        decision.request_id,
        scope="once",
        debug_cli_operator={"signed": True, "decision": "approve"},
        expected_digest="d" * 64,
    )

    assert approved["success"] is True
    assert approved["approved"] is True
    assert approved["scope"] == "once"
    assert replay["success"] is False
    assert replay["status_code"] == 409


def test_coding_approval_rejects_token_and_request_id_without_operator(
    tmp_path, monkeypatch
):
    from blocks.coding import approval_approve
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        approval_approve,
        "approve",
        lambda request_id, **_kwargs: {
            "request_id": request_id,
            "approved": True,
            "status": "approved",
            "token": "one-shot-secret",
        },
    )
    plain = approval_approve.run({"approval_request_id": "apr-1"}, {})

    ui_context = {"source": "defaultspack_local_ui"}
    mark_tool_server_approval_context(ui_context)
    forged_interactive = approval_approve.run(
        {"approval_request_id": "apr-1"},
        ui_context,
    )
    monkeypatch.setattr(approval_approve, "get_approval_request", lambda _request_id: {"args_hash": "a" * 64})
    monkeypatch.setattr(
        approval_approve,
        "verify_coding_ui_operator",
        lambda *_args, **_kwargs: {"verified": True},
    )
    interactive = approval_approve.run(
        {"approval_request_id": "apr-1", "ui_operator": {"signed": True}},
        ui_context,
    )

    monkeypatch.setattr(
        approval_approve,
        "verify_debug_cli_decision",
        lambda request_id, digest, operator: {
            "request_id": request_id,
            "args_hash": digest,
            "operator": operator,
        },
    )
    delegated = approval_approve.run(
        {
            "approval_request_id": "apr-2",
            "expected_digest": "a" * 64,
            "debug_cli_operator": {"signed": True, "decision": "approve"},
        },
        {},
    )

    assert plain["status"] == "error"
    assert plain["error"]["code"] == "APPROVAL_OPERATOR_REQUIRED"
    assert forged_interactive["status"] == "error"
    assert interactive["status"] == "ok"
    assert delegated["status"] == "ok"


def test_coding_interactive_ui_provenance_requires_browser_fetch_headers(
    monkeypatch,
):
    from transport.http import _local_ui_approval_route_authorized

    monkeypatch.setenv("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "local-test-token")
    bearer_only = {"Authorization": "Bearer local-test-token"}
    interactive = {
        **bearer_only,
        "Origin": "http://127.0.0.1:8766",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
    }

    assert (
        _local_ui_approval_route_authorized(
            "POST", "/api/coding/approvals/approve", bearer_only
        )
        is False
    )
    assert (
        _local_ui_approval_route_authorized(
            "POST", "/api/coding/approvals/approve", interactive
        )
        is False
    )


def test_native_coding_operator_is_digest_decision_and_replay_bound(monkeypatch):
    from domain.safety import coding_ui_operator
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signing_key = Ed25519PrivateKey.generate()
    public_key = base64.urlsafe_b64encode(
        signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode().rstrip("=")
    instance_nonce = "launcher-native-coding-test"
    monkeypatch.setattr(
        coding_ui_operator.ViewerBrokerClient,
        "from_environment",
        classmethod(
            lambda cls: cls(
                attestation_public_key=public_key,
                instance_nonce=instance_nonce,
            )
        ),
    )
    now = int(time.time())
    operator = {
        "version": 4,
        "kind": "coding_ui_operator",
        "origin": "tauri_webview_window",
        "instance_nonce": instance_nonce,
        "window_label": "defaultspack-main",
        "request_id": "apr-native-1",
        "expected_digest": "a" * 64,
        "decision": "approve",
        "issued_at": now,
        "expires_at": now + 60,
        "nonce": "native-once-nonce",
    }
    operator["signature"] = base64.urlsafe_b64encode(
        signing_key.sign(coding_ui_operator._message(operator))
    ).decode().rstrip("=")

    verified = coding_ui_operator.verify_coding_ui_operator(
        operator,
        request_id="apr-native-1",
        expected_digest="a" * 64,
        decision="approve",
    )
    assert verified["window_label"] == "defaultspack-main"
    with pytest.raises(coding_ui_operator.CodingUiOperatorError, match="already been used"):
        coding_ui_operator.verify_coding_ui_operator(
            operator,
            request_id="apr-native-1",
            expected_digest="a" * 64,
            decision="approve",
        )
    tampered = {**operator, "nonce": "different-nonce", "decision": "deny"}
    with pytest.raises(coding_ui_operator.CodingUiOperatorError, match="decision mismatch"):
        coding_ui_operator.verify_coding_ui_operator(
            tampered,
            request_id="apr-native-1",
            expected_digest="a" * 64,
            decision="approve",
        )


def test_approval_store_debug_binding_is_immutable(tmp_path):
    from domain.safety.approval_store import ApprovalStore

    store = ApprovalStore(tmp_path / "approval.sqlite3")
    store.save_request(
        {
            "request_id": "apr-bind-1",
            "operation": "computer.click",
            "risk_level": "high",
            "args_hash": "a" * 64,
            "details": {},
            "created_at": int(time.time()),
            "expires_at": int(time.time()) + 300,
            "status": "pending",
            "decision_at": None,
        }
    )
    binding = {
        "debug_session_id": "session-1",
        "lease_epoch": 7,
        "debug_run_id": "run-1",
        "workspace_identity_digest": "b" * 64,
        "pack_id": "defaultspack",
        "profile_id": "debug",
        "conversation_id": "conversation-1",
        "operation_owner": "defaultspack",
    }
    assert store.bind_debug_context("apr-bind-1", binding) is True
    assert store.bind_debug_context("apr-bind-1", binding) is True
    assert store.bind_debug_context(
        "apr-bind-1", {**binding, "lease_epoch": 8}
    ) is False
    stored = store.get_request("apr-bind-1")
    assert stored["lease_epoch"] == 7
    assert stored["debug_session_id"] == "session-1"


def test_request_creation_persists_active_debug_binding(tmp_path, monkeypatch):
    from domain.safety.approval_store import ApprovalStore

    store = ApprovalStore(tmp_path / "approval.sqlite3")

    class Broker:
        def available(self):
            return True

        def debug_approval_status(self):
            return {
                "status": {
                    "state": "active",
                    "session_id": "session-create",
                    "lease_epoch": 11,
                    "run_id": "run-create",
                    "workspace_digest": "c" * 64,
                    "pack_id": "defaultspack",
                    "profile_id": "debug",
                }
            }

    monkeypatch.setattr(
        runtime_approval.ViewerBrokerClient,
        "from_environment",
        classmethod(lambda _cls: Broker()),
    )
    monkeypatch.setattr(runtime_approval, "get_approval_store", lambda: store)
    monkeypatch.setattr(runtime_approval, "_refresh_approval_state_mirrors_from_store", lambda: None)

    created = runtime_approval.create_approval_request(
        "computer.click",
        "high",
        {"x": 1, "y": 2},
        details={
            "pack_id": "defaultspack",
            "profile_id": "debug",
            "conversation_id": "conversation-create",
            "operation_owner": "defaultspack",
        },
    )
    stored = store.get_request(created["request_id"])

    assert stored["debug_session_id"] == "session-create"
    assert stored["lease_epoch"] == 11
    assert stored["debug_run_id"] == "run-create"
    assert stored["workspace_identity_digest"] == "c" * 64
    assert stored["pack_id"] == "defaultspack"
    assert stored["profile_id"] == "debug"
    assert stored["conversation_id"] == "conversation-create"
    assert stored["operation_owner"] == "defaultspack"


def test_cli_approval_list_is_server_filtered_to_exact_active_binding(monkeypatch):
    session = {
        "session_id": "session-list",
        "lease_epoch": 13,
        "run_id": "run-list",
        "workspace_digest": "d" * 64,
        "pack_id": "defaultspack",
        "profile_id": "debug",
    }
    monkeypatch.setattr(cli, "_session", lambda: session)
    monkeypatch.setattr(
        cli,
        "_broker_request",
        lambda *_args, **_kwargs: {
            "status": {
                "state": "active",
                "session_id": "session-list",
                "lease_epoch": 13,
                "run_id": "run-list",
                "workspace_digest": "d" * 64,
                "pack_id": "defaultspack",
                "profile_id": "debug",
            }
        },
    )
    queries = []

    def api_request(_method, path, payload=None, query=None):
        del payload
        queries.append((path, query))
        return {"pending": []} if path == "/api/coding/approvals" else {"requests": []}

    monkeypatch.setattr(cli, "_api_request", api_request)

    assert cli._approvals_list(argparse.Namespace()) == {"pending": [], "count": 0}
    runtime_query = queries[0][1]
    assert runtime_query["debug_session_id"] == "session-list"
    assert runtime_query["lease_epoch"] == 13
    assert runtime_query["debug_run_id"] == "run-list"
    assert runtime_query["workspace_identity_digest"] == "d" * 64
    assert runtime_query["pack_id"] == "defaultspack"
    assert runtime_query["profile_id"] == "debug"
    authority_query = queries[1][1]
    assert authority_query["debug_session_id"] == "session-list"
    assert authority_query["lease_epoch"] == 13


def test_authority_request_debug_binding_is_part_of_snapshot_and_filter():
    from core_runtime.authority.service import AuthorityService

    request = _authority_request(
        debug_session_id="session-authority",
        lease_epoch=17,
        debug_run_id="run-authority",
        workspace_identity_digest="e" * 64,
        pack_id="defaultspack",
        debug_profile_id="debug",
        operation_owner="profile:debug",
    )
    original = authority_snapshot(request)["digest"]
    changed = authority_snapshot(
        _authority_request(
            debug_session_id="session-authority",
            lease_epoch=18,
            debug_run_id="run-authority",
            workspace_identity_digest="e" * 64,
            pack_id="defaultspack",
            debug_profile_id="debug",
            operation_owner="profile:debug",
        )
    )["digest"]
    binding = {
        "debug_session_id": "session-authority",
        "lease_epoch": 17,
        "debug_run_id": "run-authority",
        "workspace_identity_digest": "e" * 64,
        "pack_id": "defaultspack",
        "profile_id": "debug",
    }

    assert original != changed
    assert AuthorityService._matches_debug_binding(request, binding) is True
    assert AuthorityService._matches_debug_binding(
        request, {**binding, "lease_epoch": 18}
    ) is False
