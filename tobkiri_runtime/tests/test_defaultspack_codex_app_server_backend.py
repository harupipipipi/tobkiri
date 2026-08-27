from __future__ import annotations

from collections import deque
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class _Transport:
    def __init__(self, incoming=()):
        self.incoming = deque(incoming)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def receive(self, timeout):
        del timeout
        if not self.incoming:
            from blocks.coding.codex_app_server import RequestTimeout

            raise RequestTimeout("empty fake transport")
        return self.incoming.popleft()

    def close(self):
        self.closed = True


def _initialized_client(incoming=(), **kwargs):
    from blocks.coding.codex_app_server import CodexAppServerClient

    transport = _Transport([{"id": 1, "result": {"platformFamily": "windows"}}, *incoming])
    client = CodexAppServerClient(transport, **kwargs)
    client.initialize(version="test")
    return client, transport


def test_codex_app_server_is_coding_backend_not_provider():
    from domain.ai_client.providers import get_provider_catalog_map
    from domain.components.registry import DomainComponentRegistry, build_domain_component_roots

    registry = DomainComponentRegistry(build_domain_component_roots(DEFAULTSPACK_ROOT))
    backend = registry.get("coding_backends", "codex-app-server")

    assert backend is not None
    assert backend.kind == "coding_backend"
    assert backend.as_dict()["version"] == "2"
    assert backend.as_dict()["official_source"] == (
        "https://developers.openai.com/codex/app-server/"
    )
    assert backend.as_dict()["policy"]["do_not_treat_as_llm_provider"] is True
    assert backend.as_dict()["policy"]["read_only_by_default"] is True
    assert "codex-app-server" not in get_provider_catalog_map()


def test_codex_app_server_schema_exposes_safe_dynamic_configuration():
    import json

    schema_path = (
        DEFAULTSPACK_ROOT / "domain" / "coding_backends" / "codex-app-server" / "schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    properties = schema["properties"]
    assert "model" in properties
    assert "include_hidden_models" in properties
    assert "permission_profile" in properties
    assert "required_mcp_servers" in properties
    assert "models" not in properties
    assert schema["allOf"][0]["not"]["required"] == [
        "permission_profile",
        "legacy_sandbox",
    ]


def test_codex_app_server_workspace_boundary_and_server_approval(tmp_path):
    from blocks.coding.codex_app_server import (
        CodexAppServerBackend,
        ServerApprovalRequiredError,
        WorkspaceBoundaryError,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "notes.txt"
    inside.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")

    backend = CodexAppServerBackend()
    session = backend.create_session(str(workspace), profile={"name": "test"})

    with pytest.raises(ServerApprovalRequiredError):
        backend.validate_action(
            session,
            "file.write",
            target_path=inside,
            client_supplied_approved=True,
        )

    backend.validate_action(
        session,
        "file.write",
        target_path=inside,
        context={"server_approvals": {"file.write": True}},
        client_supplied_approved=False,
    )

    with pytest.raises(WorkspaceBoundaryError):
        backend.validate_action(
            session,
            "file.write",
            target_path=outside,
            context={"server_approvals": {"file.write": True}},
        )

    with pytest.raises(WorkspaceBoundaryError, match="not a directory"):
        backend.create_session(str(tmp_path / "missing"))


def test_initialize_handshake_precedes_all_other_calls():
    from blocks.coding.codex_app_server import CodexAppServerClient, ProtocolError

    transport = _Transport([{"id": 1, "result": {}}])
    client = CodexAppServerClient(transport)
    with pytest.raises(ProtocolError, match="initialize"):
        client.list_models()
    client.initialize()
    assert [message["method"] for message in transport.sent] == ["initialize", "initialized"]
    assert transport.sent[0]["params"]["capabilities"] == {
        "experimentalApi": False,
        "requestAttestation": False,
    }


def test_model_inventory_consumes_every_cursor_and_preserves_metadata():
    client, transport = _initialized_client(
        [
            {
                "id": 2,
                "result": {
                    "data": [
                        {
                            "id": "exact/model:preview",
                            "displayName": "Exact",
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                            "supportsPersonality": True,
                            "isDefault": True,
                            "upgrade": "exact/model:next",
                        }
                    ],
                    "nextCursor": "page-2",
                },
            },
            {
                "id": 3,
                "result": {
                    "data": [{"id": "hidden/model", "hidden": True, "inputModalities": ["text"]}],
                    "nextCursor": None,
                },
            },
        ]
    )
    models = client.list_models(include_hidden=True)
    assert [model["id"] for model in models] == ["exact/model:preview", "hidden/model"]
    assert models[0]["upgrade"] == "exact/model:next"
    assert models[0]["input_modalities"] == ["text", "image"]
    assert transport.sent[-1]["params"]["cursor"] == "page-2"


def test_thread_start_is_read_only_and_preserves_session_tree_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client, transport = _initialized_client(
        [
            {"id": 2, "result": {"data": [], "nextCursor": None}},
            {
                "id": 3,
                "result": {"thread": {"id": "thr_child", "sessionId": "thr_root"}},
            },
        ]
    )
    session = client.start_thread(workspace)
    assert session.thread_id == "thr_child"
    assert session.session_id == "thr_root"
    request = transport.sent[-1]
    assert request["method"] == "thread/start"
    assert request["params"]["sandbox"] == "read-only"
    assert request["params"]["approvalPolicy"] == "untrusted"


def test_permission_profile_and_legacy_sandbox_are_mutually_exclusive(tmp_path):
    client, _transport = _initialized_client(experimental_api=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        client._thread_config(
            tmp_path,
            permissions="workspace-write",
            sandbox="readOnly",
        )


def test_required_mcp_failure_stops_thread_start(tmp_path):
    from blocks.coding.codex_app_server import RequiredMcpServerError

    client, transport = _initialized_client(
        [
            {
                "id": 2,
                "result": {
                    "data": [{"name": "required-tools", "required": True, "status": "failed"}],
                    "nextCursor": None,
                },
            }
        ]
    )
    with pytest.raises(RequiredMcpServerError, match="required-tools"):
        client.start_thread(tmp_path)
    assert not any(message.get("method") == "thread/start" for message in transport.sent)


def test_server_approval_defaults_to_decline_and_uses_authority_when_present():
    requests = [
        {
            "method": "item/commandExecution/requestApproval",
            "id": "approval-1",
            "params": {"threadId": "thr", "turnId": "turn", "command": "echo ok"},
        },
        {"id": 2, "result": {"account": None}},
    ]
    client, transport = _initialized_client(
        requests, authority=lambda operation, _params: operation == "terminal.exec"
    )
    client.read_account()
    response = next(message for message in transport.sent if message.get("id") == "approval-1")
    assert response["result"] == {"decision": "accept"}

    denied, denied_transport = _initialized_client()
    denied._dispatch_incoming(requests[0])
    assert denied_transport.sent[-1]["result"] == {"decision": "decline"}


def test_permission_grant_cannot_exceed_requested_subset():
    client, transport = _initialized_client(
        authority=lambda _operation, _params: {
            "permissions": {
                "network": {"enabled": True},
                "fileSystem": {"read": ["/outside"]},
            },
            "scope": "session",
        }
    )
    client._dispatch_incoming(
        {
            "method": "item/permissions/requestApproval",
            "id": "permissions-1",
            "params": {"permissions": {"network": {"enabled": True}}},
        }
    )
    assert transport.sent[-1]["result"] == {"permissions": {}, "scope": "session"}


def test_permission_grant_preserves_a_structural_subset():
    client, transport = _initialized_client(
        authority=lambda _operation, _params: {
            "permissions": {"network": {"hosts": ["api.example.com"]}},
            "scope": "turn",
        }
    )
    client._dispatch_incoming(
        {
            "method": "item/permissions/requestApproval",
            "id": "permissions-1",
            "params": {
                "permissions": {
                    "network": {
                        "hosts": ["api.example.com", "docs.example.com"],
                    },
                    "fileSystem": None,
                }
            },
        }
    )
    assert transport.sent[-1]["result"] == {
        "permissions": {"network": {"hosts": ["api.example.com"]}},
        "scope": "turn",
    }


def test_auth_and_usage_notifications_are_redacted():
    client, _transport = _initialized_client()
    client._dispatch_incoming(
        {
            "method": "account/updated",
            "params": {
                "authMode": "chatgpt",
                "accessToken": "secret",
                "chatgptAccountId": "account-secret",
                "planType": "pro",
            },
        }
    )
    assert client.account["accessToken"] == "[REDACTED]"
    assert client.account["chatgptAccountId"] == "[REDACTED]"
    assert "secret" not in str(client.events)


def test_account_login_modes_and_usage_reads_do_not_retain_secrets():
    client, transport = _initialized_client(
        [
            {"id": 2, "result": {"type": "apiKey"}},
            {"id": 3, "result": {"rateLimits": {"primary": {"usedPercent": 10}}}},
            {"id": 4, "result": {"summary": {"totalTokens": 42}}},
        ]
    )
    assert client.start_account_login({"type": "apiKey", "apiKey": "sk-secret"}) == {
        "type": "apiKey"
    }
    assert client.read_rate_limits()["rateLimits"]["primary"]["usedPercent"] == 10
    assert client.read_usage()["summary"]["totalTokens"] == 42
    assert "sk-secret" not in str(client.events)
    assert "sk-secret" not in str(client.account)
    assert transport.sent[2]["method"] == "account/login/start"


def test_external_auth_tokens_require_explicit_experimental_capability():
    from blocks.coding.codex_app_server import ProtocolError

    client, transport = _initialized_client()
    with pytest.raises(ProtocolError, match="experimentalApi"):
        client.start_account_login(
            {
                "type": "chatgptAuthTokens",
                "accessToken": "secret",
                "chatgptAccountId": "account-secret",
            }
        )
    assert not any(message.get("method") == "account/login/start" for message in transport.sent)


def test_json_rpc_correlation_caches_an_early_response():
    client, _transport = _initialized_client(
        [
            {"id": 3, "result": {"rateLimits": {"primary": {"usedPercent": 20}}}},
            {"id": 2, "result": {"account": {"type": "chatgpt"}}},
        ]
    )
    assert client.read_account()["account"]["type"] == "chatgpt"
    assert client.read_rate_limits()["rateLimits"]["primary"]["usedPercent"] == 20


def test_protocol_errors_do_not_reflect_secret_server_messages():
    from blocks.coding.codex_app_server import CodexAppServerError

    client, _transport = _initialized_client(
        [{"id": 2, "error": {"code": -32000, "message": "token sk-secret"}}]
    )
    with pytest.raises(CodexAppServerError, match=r"account/read failed \(code -32000\)") as exc:
        client.read_account()
    assert "sk-secret" not in str(exc.value)


def test_request_timeout_and_unclaimed_response_backpressure_fail_closed():
    from blocks.coding import codex_app_server

    timed_out, _transport = _initialized_client()
    timed_out.timeout = 0.001
    with pytest.raises(codex_app_server.RequestTimeout):
        timed_out.read_account()

    original_limit = codex_app_server._MAX_CACHED_RESPONSES
    codex_app_server._MAX_CACHED_RESPONSES = 1
    try:
        overloaded, _transport = _initialized_client(
            [
                {"id": 100, "result": {}},
                {"id": 101, "result": {}},
            ]
        )
        with pytest.raises(codex_app_server.ProtocolError, match="too many unclaimed"):
            overloaded.read_account()
    finally:
        codex_app_server._MAX_CACHED_RESPONSES = original_limit


def test_thread_options_cannot_override_workspace_or_approval_policy(tmp_path):
    client, _transport = _initialized_client()
    with pytest.raises(ValueError, match="unsupported thread option"):
        client._thread_config(tmp_path, cwd="/outside")
    with pytest.raises(ValueError, match="unsupported thread option"):
        client._thread_config(tmp_path, approvalPolicy="never")


def test_permission_profiles_require_experimental_api(tmp_path):
    from blocks.coding.codex_app_server import ProtocolError

    client, _transport = _initialized_client()
    with pytest.raises(ProtocolError, match="experimentalApi"):
        client._thread_config(tmp_path, permissions="workspace-write")


def test_required_configured_mcp_server_must_be_present(tmp_path):
    from blocks.coding.codex_app_server import RequiredMcpServerError

    client, transport = _initialized_client(
        [{"id": 2, "result": {"data": [], "nextCursor": None}}],
        required_mcp_servers=("company-tools",),
    )
    with pytest.raises(RequiredMcpServerError, match="company-tools"):
        client.start_thread(tmp_path)
    assert not any(message.get("method") == "thread/start" for message in transport.sent)


def test_resume_and_fork_preserve_distinct_thread_and_session_ids(tmp_path):
    client, transport = _initialized_client(
        [
            {"id": 2, "result": {"data": [], "nextCursor": None}},
            {"id": 3, "result": {"thread": {"id": "thread-a", "sessionId": "tree"}}},
            {"id": 4, "result": {"data": [], "nextCursor": None}},
            {"id": 5, "result": {"thread": {"id": "thread-b", "sessionId": "tree"}}},
        ]
    )
    resumed = client.resume_thread("thread-a", tmp_path)
    forked = client.fork_thread("thread-a", tmp_path, last_turn_id="turn-1")
    assert resumed.thread_id == "thread-a"
    assert forked.thread_id == "thread-b"
    assert resumed.session_id == forked.session_id == "tree"
    fork_request = transport.sent[-1]
    assert fork_request["params"] == {
        "threadId": "thread-a",
        "lastTurnId": "turn-1",
    }


def test_turn_interrupt_waits_for_terminal_server_event(tmp_path):
    from blocks.coding.codex_app_server import CodingSession

    client, transport = _initialized_client(
        [
            {"id": 2, "result": {}},
            {
                "method": "turn/completed",
                "emittedAtMs": 123,
                "params": {
                    "threadId": "thread-a",
                    "turn": {"id": "turn-a", "status": "interrupted"},
                },
            },
        ]
    )
    session = CodingSession("tree", str(tmp_path), thread_id="thread-a")
    interrupted = client.interrupt_turn(session, "turn-a", timeout=1)
    assert interrupted["turn"]["status"] == "interrupted"
    assert client.events[-1]["emitted_at_ms"] == 123
    assert transport.sent[-1]["method"] == "turn/interrupt"


def test_reconnect_rehydrates_known_threads_from_the_server(tmp_path):
    client, old_transport = _initialized_client(
        [
            {"id": 2, "result": {"data": [], "nextCursor": None}},
            {"id": 3, "result": {"thread": {"id": "thread-a", "sessionId": "tree"}}},
        ]
    )
    client.start_thread(tmp_path)
    new_transport = _Transport(
        [
            {"id": 1, "result": {"platformFamily": "unix"}},
            {
                "id": 2,
                "result": {
                    "thread": {
                        "id": "thread-a",
                        "sessionId": "tree",
                        "turns": [{"id": "turn-a", "status": "inProgress"}],
                    }
                },
            },
        ]
    )
    restored = client.reconnect(new_transport)
    assert old_transport.closed is True
    assert restored["threads"]["thread-a"]["thread"]["turns"][0]["id"] == "turn-a"
    assert [message["method"] for message in new_transport.sent] == [
        "initialize",
        "initialized",
        "thread/read",
    ]


def test_unsupported_server_request_returns_json_rpc_method_error():
    client, transport = _initialized_client()
    client._dispatch_incoming({"id": "server-1", "method": "unknown/request", "params": {}})
    assert transport.sent[-1] == {
        "id": "server-1",
        "error": {"code": -32601, "message": "unsupported server request"},
    }


def test_malformed_message_fails_closed():
    from blocks.coding.codex_app_server import ProtocolError

    client, _transport = _initialized_client()
    with pytest.raises(ProtocolError):
        client._dispatch_incoming({"unexpected": True})


def test_stdio_transport_process_lifecycle_is_cross_platform(tmp_path):
    from blocks.coding.codex_app_server import CodexAppServerClient, StdioJsonRpcTransport

    script = (
        "import json,sys; "
        "m=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'id':m['id'],'result':{'platformFamily':sys.platform}}),flush=True); "
        "sys.stdin.readline()"
    )
    transport = StdioJsonRpcTransport(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
    )
    client = CodexAppServerClient(transport, timeout=3)
    result = client.initialize()
    assert result["platformFamily"] == sys.platform
    assert transport.pid > 0
    client.close()


def test_stdio_transport_rejects_malformed_json_frames(tmp_path):
    from blocks.coding.codex_app_server import (
        CodexAppServerClient,
        ProtocolError,
        StdioJsonRpcTransport,
    )

    script = "import sys; print('not-json', flush=True); sys.stdin.readline()"
    transport = StdioJsonRpcTransport(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
    )
    client = CodexAppServerClient(transport, timeout=3)
    with pytest.raises(ProtocolError, match="malformed"):
        client.initialize()
    client.close()
