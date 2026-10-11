"""Real saved HTTP/native-consent effects publish captured tool progress.

Only guest process transport, model responses, and native decision transport
are adapters. HTTP authentication, saved owner/binding, Broker, approvals,
effects, progress reservation/publication, and progress resource reads are real.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Mapping

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import interactive_confirmation_digest
from core_runtime.bootstrap.saved_bridge import READINESS
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_tool_broker_pack.runtime import broker
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore
from tests.test_production_frontend_contract_http import (
    _SavedToolPackVmBackend,
    _authenticate,
    _captured_production_server,
    _contract,
    _request,
)
from tobkiri_host.authority_approval_window import AuthorityApprovalWindowController
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.credential_store import host_credential_store_factory
from tobkiri_host.ports import InteractiveApprovalDecisionCommand
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_host.saved_tool_consent import SavedToolConsentExecution
from tobkiri_protocol.turn_progress_v1 import AI_STREAM, payload_digest


@pytest.mark.parametrize(
    "tool_id,arguments,expected",
    [
        ("calculator", {"expression": "2+2"}, "4"),
        ("coding_file_read", {"path": "hello.txt"}, "owned file content"),
    ],
)
def test_real_captured_tool_progress_and_result_use_original_default_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
    arguments: dict,
    expected: str,
) -> None:
    """Obtain real saved ancestry and native admission before either effect."""
    native_commands = {}
    native_decisions = []
    tool_results = []
    tool_payloads = []
    pages = []
    elapsed = []
    broker_budgets = []
    broker_deadlines = []
    tool_finished = []
    ai_calls = []
    saved_envelopes = []
    rejected = []
    original_window = (
        AuthorityApprovalWindowController.open_authority_approval_window
    )

    def observe_window(self, command):
        assert command.request_id not in native_commands
        native_commands[command.request_id] = command
        return original_window(self, command)

    def decide_native(request_id: str) -> Mapping[str, object]:
        command = native_commands[request_id]
        pending = authority.get_interactive_approval_request(request_id)
        assert pending is not None
        assert pending.target.operation_id == "host_consent.admit"
        assert authority.interactive_approval_state(request_id) == "pending"
        confirmation = ""
        if pending.typed_confirmation_digest is not None:
            confirmation = pending.redacted_metadata["confirmation_phrase"]
            assert interactive_confirmation_digest(confirmation) == (
                pending.typed_confirmation_digest
            )
        proof = sign_ui_operator(
            request_id,
            nonce=str(uuid.uuid4()),
            decision="approve",
            request_snapshot_digest=pending.digest.removeprefix("sha256:"),
            typed_confirmation_digest=(
                pending.typed_confirmation_digest.removeprefix("sha256:")
                if pending.typed_confirmation_digest is not None else None
            ),
        )
        decision = session.authority_control.approve_interactive_approval(
            InteractiveApprovalDecisionCommand(
                context=command.context,
                request_id=request_id,
                actor_id="test-native-user",
                confirmation_text=confirmation,
                ui_operator=proof,
            )
        )
        assert decision.state == "approved"
        native_decisions.append(request_id)
        return {"opened": True}

    monkeypatch.setattr(
        AuthorityApprovalWindowController,
        "open_authority_approval_window",
        observe_window,
    )
    original_provider = ExactHostProviderBackendV4.invoke
    original_consent = SavedToolConsentExecution.__call__

    def observe_consent(self, invocation, execution, payload):
        try:
            return original_consent(self, invocation, execution, payload)
        except Exception as error:
            frames = []
            trace = error.__traceback__
            while trace is not None:
                frames.append({
                    "file": Path(trace.tb_frame.f_code.co_filename).name,
                    "line": trace.tb_lineno,
                })
                trace = trace.tb_next
            rejected.append({
                "phase": "consent", "type": type(error).__name__,
                "frames": frames[-8:]
            })
            raise

    monkeypatch.setattr(SavedToolConsentExecution, "__call__", observe_consent)

    def observe_provider(self, request):
        if (request.contract_id, request.operation_id) == (
            broker.CONTRACT, broker.OPERATION
        ):
            broker_budgets.append(request.deadline_monotonic - time.monotonic())
            broker_deadlines.append(request.deadline_monotonic)
        return original_provider(self, request)

    monkeypatch.setattr(ExactHostProviderBackendV4, "invoke", observe_provider)
    original = V4DispatchSession.invoke

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        target = (contract_id, operation_id)
        if target == READINESS:
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if target in {AI_STREAM, TARGETS[2]}:
            assert target == AI_STREAM
            ai_calls.append(payload)
            assert tool_id in {
                item["function"]["name"] for item in payload["tools"]
            }
            if len(ai_calls) == 1:
                return {
                    "status": "ok",
                    "output": "",
                    "tool_intents": [{
                        "operation": tool_id,
                        "intent_id": "captured-call",
                        "arguments": arguments,
                    }],
                }
            assert len(ai_calls) == 2
            assert expected in payload["messages"][-1]["content"]
            return {"status": "ok", "output": "done"}
        started = time.monotonic()
        try:
            result = original(self, contract_id, operation_id, payload, **kwargs)
        except Exception as error:
            frames = []
            trace = error.__traceback__
            while trace is not None:
                frames.append({
                    "file": Path(trace.tb_frame.f_code.co_filename).name,
                    "line": trace.tb_lineno,
                })
                trace = trace.tb_next
            rejected.append({
                "target": target, "type": type(error).__name__, "frames": frames[-8:]
            })
            raise
        if target == (broker.CONTRACT, broker.OPERATION):
            tool_finished.append(time.monotonic())
            elapsed.append(tool_finished[-1] - started)
            tool_payloads.append(dict(payload))
            tool_results.append(result)
            assert len(native_decisions) == 1
            # Read while the acknowledged user branch is still current and
            # before the next AI stage reserves its separate progress identity.
            status, page, _ = _request(
                server,
                "GET",
                _contract("GET", "/api/chat/turn/progress")
                + "?turn_id=progress-turn&conversation_id=progress-conversation"
                + f"&cursor=0&progress_id={payload['progress_id']}",
                headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
            )
            assert status == 200, page
            pages.append(page["data"])
        return result

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    saved_backend = _SavedToolPackVmBackend()
    original_saved = saved_backend.invoke

    def observe_saved(request):
        saved_envelopes.append(request)
        return original_saved(request)

    monkeypatch.setattr(saved_backend, "invoke", observe_saved)
    servers = _captured_production_server(
        tmp_path,
        monkeypatch,
        packvm_backends=BackendRegistry((saved_backend,)),
        credential_store_factory=host_credential_store_factory,
        authority_approval_window_open=decide_native,
    )
    server, session, authority = next(servers)
    try:
        user_data = tmp_path / "user-data"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("owned file content", encoding="utf-8")
        mounts = WorkspaceMountStore("defaults", user_data_root=user_data)
        mounts.mount("owned", str(workspace), expected_revision=0)
        mounts.select("owned", expected_revision=1)
        conversation = {
            "id": "progress-conversation",
            "model_reference": "model-profile-1",
        }
        if tool_id.startswith("coding_"):
            conversation.update(
                conversation_kind="coding",
                metadata={"mode": "coding", "workspace_id": "owned"},
            )
        ConversationStore("defaults", user_data_root=user_data).create(
            conversation, expected_revision=0
        )
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
        body = {"request": {
            "turn_id": "progress-turn",
            "conversation_id": "progress-conversation",
            "conversation_revision": 1,
            "content": "owned fixture",
            "tool_selection": {
                "mode": "manual", "include": [tool_id], "exclude": [],
                "scope": "turn", "must_use": True,
            },
        }}
        status, completed, _ = _request(
            server, "POST", _contract("POST", "/api/chat/turn"),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
            timeout_seconds=330,
        )
        assert status == 200, completed
        assert completed["data"]["status"] == "completed", json.dumps({
            "rejected": rejected,
            "ai_calls": len(ai_calls),
            "native_decisions": len(native_decisions),
            "tool_results": tool_results,
            "pages": pages,
        }, ensure_ascii=False)
        assert len(ai_calls) == 2
        assert len(native_decisions) == len(tool_results) == len(pages) == 1
        result = tool_results[0]
        assert result["status"] == "success" and result["is_error"] is False
        assert expected in json.dumps(result, ensure_ascii=False)
        page = pages[0]
        payload = {key: tool_payloads[0][key] for key in (
            "tool_id", "tool_call_id", "arguments"
        )}
        assert page["progress_id"] == tool_payloads[0]["progress_id"]
        assert page["canonical_turn_status"] == "running"
        assert page["provider_complete"] is True
        assert page["events"] == [
            {"cursor": 1, "event": {"type": "tool_started", **payload}},
            {"cursor": 2, "event": {
                "type": "tool_completed",
                "tool_id": tool_id,
                "tool_call_id": "captured-call",
                "status": result["status"],
                "content": json.dumps(
                    {key: result.get(key) for key in ("status", "result", "error")},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False,
                ),
            }},
        ]
        turn = completed["data"]["turn"]
        assert page["binding"]["input_digest"] == turn["input_digest"]
        assert len(saved_envelopes) == 1
        assert page["binding"]["request_id"] == (
            saved_envelopes[0].context.request_id
        )
        original_payload = dict(tool_payloads[0])
        original_payload.pop("progress_id")
        original_payload.pop("_session_id", None)
        assert page["binding"]["ai_input_digest"] == payload_digest(original_payload)
        assert len(broker_budgets) == 1 and 0 < broker_budgets[0] <= 300
        assert broker_deadlines[0] <= saved_envelopes[0].deadline_monotonic
        assert tool_finished[0] < broker_deadlines[0]
        assert elapsed[0] < 300, "effect exceeded its inherited captured budget"
    finally:
        servers.close()
