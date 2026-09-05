from __future__ import annotations

import http.client
import json
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from tests.conformance_support.host_contract import host_contract


def _assert_v4_local_guard_boundary() -> None:
    """Bind local mutations to the v4 host authority contract."""
    from tempfile import TemporaryDirectory

    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("domain.function_runtime.bridge")
    assert_profile_resolver_requires_authority_snapshot()
    with TemporaryDirectory() as root:
        assert_payload_mutations_denied(harness(Path(root)))


def test_sensitive_coding_http_path_uses_local_guard():
    from transport.http import _RequestHandler, _is_sensitive_http_path

    for path in (
        "/api/coding/files",
        "/api/coding/files/read",
        "/api/coding/files/search",
        "/api/coding/files/diff",
        "/api/coding/files/write",
    ):
        assert _is_sensitive_http_path(path) is True

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "https://example.test"}
    handler.client_address = ("127.0.0.1", 54321)

    assert handler._sensitive_request_error("POST", "/api/coding/files/write") == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )
    assert handler._sensitive_request_error("POST", "/api/coding/files/read") == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )
    assert handler._sensitive_request_error("GET", "/api/coding/files") == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )


def test_sensitive_coding_http_path_requires_csrf_for_local_origin():
    from transport.http import _RequestHandler

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "http://localhost:8766"}
    handler.client_address = ("127.0.0.1", 54321)

    assert handler._sensitive_request_error("POST", "/api/coding/terminal/exec") == (
        403,
        "CSRF header required for sensitive local mutation",
        "CSRF_REQUIRED",
    )
    assert handler._sensitive_request_error("POST", "/api/authority/requests/auth_1/approve") == (
        403,
        "CSRF header required for sensitive local mutation",
        "CSRF_REQUIRED",
    )

    handler.headers = {"Origin": "http://localhost:8766", "X-Rumi-CSRF": "1"}
    assert handler._sensitive_request_error("POST", "/api/coding/terminal/exec") is None
    assert (
        handler._sensitive_request_error("POST", "/api/authority/requests/auth_1/approve") is None
    )

    handler.headers = {"origin": "http://localhost:8766", "x-rumi-csrf": "1"}
    assert handler._sensitive_request_error("POST", "/api/coding/terminal/exec") is None


def test_git_branch_post_is_guarded_but_get_remains_read_only():
    from transport.http import _RequestHandler, _is_sensitive_http_path

    assert _is_sensitive_http_path("/api/coding/git/branch") is True

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "http://localhost:8766"}
    handler.client_address = ("127.0.0.1", 54321)

    assert handler._sensitive_request_error("GET", "/api/coding/git/branch") is None
    assert handler._sensitive_request_error("POST", "/api/coding/git/branch") == (
        403,
        "CSRF header required for sensitive local mutation",
        "CSRF_REQUIRED",
    )


def test_cockpit_sensitive_reads_are_guarded():
    from transport.http import _RequestHandler, _is_sensitive_http_path

    sensitive_reads = [
        "/api/coding/approvals",
        "/api/authority/requests",
        "/api/authority/test/request",
        "/api/browser/artifacts",
        "/api/coding/agent/sessions/status",
        "/api/coding/agent/sessions/merge-report",
    ]
    for path in sensitive_reads:
        assert _is_sensitive_http_path(path) is True

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "https://example.test"}
    handler.client_address = ("127.0.0.1", 54321)

    assert handler._sensitive_request_error("GET", "/api/coding/approvals") == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )

    handler.headers = {"Origin": "http://localhost:8766"}
    assert handler._sensitive_request_error("GET", "/api/coding/approvals") is None


def test_parameterized_workspace_mutations_are_guarded():
    from domain.safety.local_guard import is_sensitive_coding_path

    assert is_sensitive_coding_path("/api/coding/workspaces/ws-1", "PUT") is True
    assert is_sensitive_coding_path("/api/coding/workspaces/ws-1/select", "POST") is True
    assert is_sensitive_coding_path("/api/coding/workspaces/ws-1/trust", "POST") is True
    assert is_sensitive_coding_path("/api/coding/workspaces/ws-1", "GET") is False


def test_dynamic_tool_post_routes_are_guarded():
    from domain.safety.local_guard import require_local_guard
    from transport.http import _is_sensitive_http_path

    assert _is_sensitive_http_path("/api/tools/example") is True
    assert require_local_guard(
        "/api/tools/example",
        "POST",
        {"Origin": "http://localhost:8766"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "CSRF header required for sensitive local mutation",
        "CSRF_REQUIRED",
    )


def test_browser_companion_session_get_is_local_guarded():
    from domain.safety.local_guard import require_local_guard
    from transport.http import _RequestHandler, _is_sensitive_http_path

    path = "/api/tools/browser-companion/session"

    assert _is_sensitive_http_path(path) is True
    assert require_local_guard(
        path,
        "GET",
        {"Origin": "https://example.test"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )
    assert require_local_guard(
        path,
        "GET",
        {"Origin": "http://localhost:8766"},
        ("203.0.113.10", 54321),
    ) == (
        403,
        "sensitive local route requires a loopback client",
        "LOCAL_ONLY_REQUIRED",
    )
    assert require_local_guard(
        path,
        "GET",
        {"Origin": "http://localhost:8766"},
        ("127.0.0.1", 54321),
    ) is None

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "https://example.test"}
    handler.client_address = ("127.0.0.1", 54321)
    assert handler._sensitive_request_error("GET", path) == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )


def test_non_loopback_websocket_upgrade_requires_local_auth(monkeypatch):
    from transport.http import _websocket_auth_error
    from core_runtime.host_contract import bind_host_contract
    from tests.conformance_support.host_contract import host_contract

    headers = {"Upgrade": "websocket", "Connection": "Upgrade"}

    assert _websocket_auth_error(headers, ("127.0.0.1", 54321)) is None
    assert _websocket_auth_error(headers, ("203.0.113.10", 54321)) == (
        403,
        "websocket auth token is not configured",
        "AUTH_REQUIRED",
    )

    with bind_host_contract(
        host_contract(
            profile_id="profile:test",
            values={"desktop_api_token": "local-ws-token"},
        )
    ):
        assert _websocket_auth_error(headers, ("203.0.113.10", 54321)) == (
            401,
            "websocket auth token required",
            "AUTH_REQUIRED",
        )
        assert _websocket_auth_error(
            {**headers, "Authorization": "Bearer local-ws-token"},
            ("203.0.113.10", 54321),
        ) is None


def test_route_metadata_sensitive_reads_server_route_table():
    from transport.http import _RequestHandler

    def handler(request_data, path_params):
        return {"ok": True, "request_data": request_data, "path_params": path_params}

    handler.__rumi_route_sensitive__ = True
    request_handler = _RequestHandler.__new__(_RequestHandler)
    request_handler.server_ref = SimpleNamespace(
        _routes=[
            (
                "POST",
                re.compile("^/api/template/sensitive$"),
                handler,
                "registry",
                {},
                "/api/template/sensitive",
            )
        ]
    )

    assert request_handler._route_metadata_sensitive("POST", "/api/template/sensitive") is True
    assert request_handler._route_metadata_sensitive("GET", "/api/template/sensitive") is False


def test_legacy_browser_qa_token_cannot_submit_pre_auth_event(monkeypatch):
    from transport.http import _RequestHandler
    from core_runtime.host_contract import bind_host_contract

    def handler(request_data, path_params):
        return {"ok": True, "request_data": request_data, "path_params": path_params}

    handler.__rumi_route_pre_auth__ = True
    request_handler = _RequestHandler.__new__(_RequestHandler)
    request_handler.client_address = ("127.0.0.1", 54321)
    request_handler.server_ref = SimpleNamespace(
        _routes=[
            (
                "POST",
                re.compile("^/api/ambient/events$"),
                handler,
                "registry",
                {},
                "/api/ambient/events",
            )
        ]
    )

    monkeypatch.setenv("RUMI_API_TOKEN", "local-secret")
    monkeypatch.setenv("RUMI_AUTHORITY_BROWSER_TEST_TOKEN", "browser-secret")

    with bind_host_contract(
        host_contract(
            profile_id="profile:test",
            values={"desktop_api_token": "local-secret"},
        )
    ):
        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "X-Rumi-CSRF": "1",
            "X-Rumi-Approval-Browser-Token": "browser-secret",
        }
        assert request_handler._sensitive_request_error("POST", "/api/ambient/events") == (
            401,
            "local auth token required",
            "AUTH_REQUIRED",
        )

        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "X-Rumi-CSRF": "1",
            "X-Rumi-Approval-Browser-Token": "wrong",
        }
        assert request_handler._sensitive_request_error("POST", "/api/ambient/events") == (
            401,
            "local auth token required",
            "AUTH_REQUIRED",
        )

        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "X-Rumi-Approval-Browser-Token": "browser-secret",
        }
        assert request_handler._sensitive_request_error("POST", "/api/ambient/events") == (
            401,
            "local auth token required",
            "AUTH_REQUIRED",
        )


def test_legacy_browser_qa_token_cannot_mint_authority_ui_operator(monkeypatch):
    from transport.http import _RequestHandler, _browser_qa_token_authorized
    from core_runtime.host_contract import bind_host_contract

    def handler(request_data, path_params):
        return {"ok": True, "request_data": request_data, "path_params": path_params}

    handler.__rumi_route_sensitive__ = True

    request_handler = _RequestHandler.__new__(_RequestHandler)
    request_handler.client_address = ("127.0.0.1", 54321)
    request_handler.server_ref = SimpleNamespace(
        _routes=[
            (
                "POST",
                re.compile("^/api/authority/browser-ui-operator$"),
                handler,
                "registry",
                {},
                "/api/authority/browser-ui-operator",
            )
        ]
    )

    monkeypatch.setenv("RUMI_API_TOKEN", "local-secret")
    monkeypatch.setenv("RUMI_AUTHORITY_BROWSER_TEST_TOKEN", "browser-secret")

    with bind_host_contract(
        host_contract(
            profile_id="profile:test",
            values={"desktop_api_token": "local-secret"},
        )
    ):
        request_handler.headers = {
            "Origin": "http://127.0.0.1:8766",
            "X-Rumi-CSRF": "1",
            "X-Rumi-Approval-Browser-Token": "browser-secret",
        }
        assert _browser_qa_token_authorized(
            "POST",
            "/api/authority/browser-ui-operator",
            request_handler.headers,
        ) is False
        assert request_handler._sensitive_request_error(
            "POST", "/api/authority/browser-ui-operator"
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        request_handler.headers = {
            "Origin": "http://127.0.0.1:8766",
            "X-Rumi-CSRF": "1",
            "X-Rumi-Approval-Browser-Token": "wrong",
        }
        assert _browser_qa_token_authorized(
            "POST",
            "/api/authority/browser-ui-operator",
            request_handler.headers,
        ) is False
        assert request_handler._sensitive_request_error(
            "POST", "/api/authority/browser-ui-operator"
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        request_handler.headers = {
            "Origin": "http://127.0.0.1:8766",
            "X-Rumi-CSRF": "1",
        }
        query_data = {"browser_approval_token": "browser-secret"}
        assert _browser_qa_token_authorized(
            "POST",
            "/api/authority/browser-ui-operator",
            request_handler.headers,
            query_data,
        ) is False
        assert request_handler._sensitive_request_error(
            "POST",
            "/api/authority/browser-ui-operator",
            query_data,
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        invalid_query_data = {"browser_approval_token": "wrong"}
        assert _browser_qa_token_authorized(
            "POST",
            "/api/authority/browser-ui-operator",
            request_handler.headers,
            invalid_query_data,
        ) is False
        assert request_handler._sensitive_request_error(
            "POST",
            "/api/authority/browser-ui-operator",
            invalid_query_data,
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        request_handler.headers = {
            "Origin": "http://127.0.0.1:8766",
            "X-Rumi-Approval-Browser-Token": "browser-secret",
        }
        assert request_handler._sensitive_request_error(
            "POST", "/api/authority/browser-ui-operator"
        ) == (401, "local auth token required", "AUTH_REQUIRED")


def test_ambient_browser_qa_context_flag_becomes_tool_server_approval():
    from transport.http import _AMBIENT_BROWSER_QA_CONTEXT_FLAG, _apply_ambient_browser_qa_context

    context = {}
    payload = {_AMBIENT_BROWSER_QA_CONTEXT_FLAG: True, "input_text": "hello"}

    _apply_ambient_browser_qa_context(context, payload)

    assert payload == {"input_text": "hello"}
    assert context["_tool_server_approved"] is True
    assert context["source"] == "ambient_browser_qa"
    assert context["approval_id"] == "ambient_browser_qa"


def test_ambient_browser_qa_context_does_not_bypass_v4_dispatch_boundary():
    from transport.http import DefaultsHttpServer, _AMBIENT_BROWSER_QA_CONTEXT_FLAG

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)

    result = server._invoke_function_route(
        "ambient_event_submit",
        {_AMBIENT_BROWSER_QA_CONTEXT_FLAG: True, "input_text": "hello"},
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "V4_OPERATION_UNAVAILABLE"
    assert "captured v4 catalog" in result["error"]["message"]


def test_ambient_browser_qa_context_reaches_function_routes():
    _assert_v4_local_guard_boundary()


def test_composer_transcription_requires_exact_loopback_same_origin_without_bearer_auth():
    from transport.http import _composer_transcription_request_error

    same_origin_headers = {
        "Host": "127.0.0.1:8766",
        "Origin": "http://127.0.0.1:8766",
    }
    assert _composer_transcription_request_error(
        same_origin_headers,
        ("127.0.0.1", 54321),
    ) is None

    assert _composer_transcription_request_error(
        {**same_origin_headers, "Origin": "http://127.0.0.1:8767"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "composer transcription origin does not match the local server",
        "ORIGIN_DENIED",
    )
    assert _composer_transcription_request_error(
        {"Host": "example.test:8766", "Origin": "http://example.test:8766"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "composer transcription requires a valid loopback same-origin request",
        "ORIGIN_DENIED",
    )
    assert _composer_transcription_request_error(
        same_origin_headers,
        ("203.0.113.7", 54321),
    ) == (
        403,
        "composer transcription requires a loopback client",
        "LOCAL_ONLY_REQUIRED",
    )
    assert _composer_transcription_request_error(
        {"Host": "127.0.0.1:8766"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "composer transcription requires a valid loopback same-origin request",
        "ORIGIN_DENIED",
    )


def test_composer_transcription_http_guard_rejects_cross_port_and_oversize_body_early(monkeypatch):
    from transport.http import (
        DefaultsHttpServer,
        _COMPOSER_TRANSCRIPTION_MAX_REQUEST_BYTES,
    )

    for key in ("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "RUMI_API_TOKEN", "RUMI_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEFAULTS_HTTP_PORT", "0")
    server = DefaultsHttpServer(None)
    server.start()
    try:
        port = server._server.server_address[1]
        body = json.dumps({"audio_data_url": "not-a-data-url"})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/ambient/transcriptions",
            body=body,
            headers={
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
            },
        )
        direct_response = connection.getresponse()
        direct_payload = json.loads(direct_response.read().decode("utf-8"))
        connection.close()
        assert direct_response.status == 404
        assert direct_payload["error"]["code"] == "ERROR"
        assert "not found" in direct_payload["error"]["message"]

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/ambient/transcriptions",
            body=body,
            headers={
                "Origin": f"http://127.0.0.1:{port + 1}",
                "Content-Type": "application/json",
            },
        )
        cross_port_response = connection.getresponse()
        cross_port_payload = json.loads(cross_port_response.read().decode("utf-8"))
        connection.close()
        assert cross_port_response.status == 403
        assert cross_port_payload["error"]["code"] == "ORIGIN_DENIED"

        raw_request = (
            b"POST /api/ambient/transcriptions HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode("ascii")
            + f"Origin: http://127.0.0.1:{port}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {_COMPOSER_TRANSCRIPTION_MAX_REQUEST_BYTES + 1}\r\n".encode(
                "ascii"
            )
            + b"Connection: close\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(raw_request)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks).decode("utf-8", errors="replace")
        assert "HTTP/1.1 413" in response
        assert "AUDIO_PAYLOAD_TOO_LARGE" in response
    finally:
        server.stop()


def test_ambient_monitor_start_requires_local_auth_and_rejects_unbound_v4_route(monkeypatch):
    from transport.http import (
        DefaultsHttpServer,
        _LOCAL_UI_APPROVAL_CONTEXT_FLAG,
        _RequestHandler,
        _browser_qa_token_authorized,
        _local_ui_approval_route_authorized,
    )
    from core_runtime.host_contract import bind_host_contract

    request_handler = _RequestHandler.__new__(_RequestHandler)
    request_handler.client_address = ("127.0.0.1", 54321)
    request_handler.server_ref = SimpleNamespace(_routes=[])
    monkeypatch.setenv("RUMI_AUTHORITY_BROWSER_TEST_TOKEN", "browser-secret")

    with bind_host_contract(
        host_contract(
            profile_id="profile:test",
            values={"desktop_api_token": "local-secret"},
        )
    ):
        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "X-Rumi-CSRF": "1",
        }
        assert request_handler._sensitive_request_error(
            "POST", "/api/ambient/monitor/start"
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "X-Rumi-CSRF": "1",
            "X-Rumi-Approval-Browser-Token": "browser-secret",
        }
        assert _browser_qa_token_authorized(
            "POST",
            "/api/ambient/monitor/start",
            request_handler.headers,
        ) is False
        assert request_handler._sensitive_request_error(
            "POST", "/api/ambient/monitor/start"
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "Authorization": "Bearer local-secret",
            "X-Rumi-CSRF": "1",
        }
        assert request_handler._sensitive_request_error(
            "POST", "/api/ambient/monitor/start"
        ) is None
        assert _local_ui_approval_route_authorized(
            "POST",
            "/api/ambient/monitor/start",
            request_handler.headers,
        ) is True

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)

    result = server._invoke_function_route(
        "ambient_monitor_start",
        {_LOCAL_UI_APPROVAL_CONTEXT_FLAG: True, "action": "start"},
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "V4_OPERATION_UNAVAILABLE"


def test_runtime_and_desktop_mutations_require_local_ui_auth_and_reject_unbound_v4_route():
    from transport.http import (
        DefaultsHttpServer,
        _LOCAL_UI_APPROVAL_CONTEXT_FLAG,
        _local_ui_approval_route_authorized,
    )
    from core_runtime.host_contract import bind_host_contract

    headers = {
        "Origin": "http://localhost:8766",
        "Authorization": "Bearer local-secret",
        "X-Rumi-CSRF": "1",
    }

    with bind_host_contract(
        host_contract(
            profile_id="profile:test",
            values={"desktop_api_token": "local-secret"},
        )
    ):
        for method, path in (
            ("POST", "/api/runtime/ensure"),
            ("POST", "/api/runtime/update"),
            ("POST", "/api/runtime/uninstall"),
            ("POST", "/api/runtime/operations/op-1/cancel"),
            ("POST", "/api/desktops"),
            ("POST", "/api/desktops/seat-1/start"),
            ("POST", "/api/desktops/seat-1/stop"),
            ("POST", "/api/desktops/seat-1/restart"),
            ("POST", "/api/desktops/seat-1/input"),
            ("POST", "/api/desktops/seat-1/ai-input"),
            ("POST", "/api/desktops/seat-1/rules"),
            ("POST", "/api/desktops/seat-1/control/acquire"),
            ("POST", "/api/desktops/seat-1/control/renew"),
            ("POST", "/api/desktops/seat-1/control/release"),
            ("POST", "/api/desktops/seat-1/access-requests/request-1/grant"),
            ("DELETE", "/api/desktops/seat-1"),
        ):
            assert _local_ui_approval_route_authorized(method, path, headers) is True

        assert _local_ui_approval_route_authorized("GET", "/api/desktops", headers) is False
        assert _local_ui_approval_route_authorized(
            "POST", "/api/runtime/ensure", {"X-Rumi-CSRF": "1"}
        ) is False

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)

    result = server._invoke_function_route(
        "managed_runtime_ensure",
        {_LOCAL_UI_APPROVAL_CONTEXT_FLAG: True, "provider_id": "windows_wsl"},
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "V4_OPERATION_UNAVAILABLE"


def test_provider_key_save_requires_local_auth_and_rejects_unbound_v4_route():
    from transport.http import (
        DefaultsHttpServer,
        _LOCAL_UI_APPROVAL_CONTEXT_FLAG,
        _RequestHandler,
        _local_ui_approval_route_authorized,
    )
    from core_runtime.host_contract import bind_host_contract

    request_handler = _RequestHandler.__new__(_RequestHandler)
    request_handler.client_address = ("127.0.0.1", 54321)
    request_handler.server_ref = SimpleNamespace(_routes=[])

    with bind_host_contract(
        host_contract(
            profile_id="profile:test",
            values={"desktop_api_token": "local-secret"},
        )
    ):
        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "X-Rumi-CSRF": "1",
        }
        assert request_handler._sensitive_request_error(
            "POST", "/api/ai/provider-key"
        ) == (401, "local auth token required", "AUTH_REQUIRED")

        request_handler.headers = {
            "Origin": "http://localhost:8766",
            "Authorization": "Bearer local-secret",
            "X-Rumi-CSRF": "1",
        }
        assert request_handler._sensitive_request_error(
            "POST", "/api/ai/provider-key"
        ) is None
        assert _local_ui_approval_route_authorized(
            "POST",
            "/api/ai/provider-key",
            request_handler.headers,
        ) is True

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)

    result = server._invoke_function_route(
        "ai_set_provider_key",
        {
            _LOCAL_UI_APPROVAL_CONTEXT_FLAG: True,
            "provider_id": "opencode-go",
            "value": "secret",
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "V4_OPERATION_UNAVAILABLE"


def test_ambient_monitor_start_requires_local_auth_and_marks_local_ui_context():
    _assert_v4_local_guard_boundary()


def test_runtime_and_desktop_mutations_can_use_local_ui_approval_context():
    _assert_v4_local_guard_boundary()


def test_provider_key_save_requires_local_auth_and_marks_local_ui_context():
    _assert_v4_local_guard_boundary()


def test_provider_key_save_accepts_viewer_persisted_token_when_launch_token_differs(
    monkeypatch, tmp_path
):
    from transport.http import (
        _RequestHandler,
        _local_ui_approval_route_authorized,
    )

    app_dir = tmp_path / "rumi-app"
    user_data = app_dir / "user_data"
    app_dir.mkdir()
    user_data.mkdir()
    (app_dir / ".desktop_api_token").write_text("viewer-local-token", encoding="utf-8")
    monkeypatch.setenv("RUMI_APP_DIR", str(app_dir))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_API_TOKEN", "launch-issued-token")
    monkeypatch.setenv("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "launch-issued-token")

    request_handler = _RequestHandler.__new__(_RequestHandler)
    request_handler.client_address = ("127.0.0.1", 54321)
    request_handler.server_ref = SimpleNamespace(_routes=[])
    request_handler.headers = {
        "Origin": "http://localhost:8766",
        "Authorization": "Bearer viewer-local-token",
        "X-Rumi-CSRF": "1",
    }

    assert request_handler._sensitive_request_error("POST", "/api/ai/provider-key") is None
    assert _local_ui_approval_route_authorized(
        "POST",
        "/api/ai/provider-key",
        request_handler.headers,
    ) is True


def test_self_improvement_routes_are_guarded_as_sensitive_local_routes():
    from domain.safety.local_guard import require_local_guard
    from transport.http import _RequestHandler, _is_sensitive_http_path

    sensitive_paths = [
        "/api/agent/self-improvement/status",
        "/api/agent/self-improvement/run",
        "/api/agent/self-improvement/report",
    ]
    for path in sensitive_paths:
        assert _is_sensitive_http_path(path) is True
        assert require_local_guard(
            path,
            "POST",
            {"Origin": "https://example.test"},
            ("127.0.0.1", 54321),
        ) == (
            403,
            "origin not allowed for sensitive local route",
            "ORIGIN_DENIED",
        )

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "http://localhost:8766"}
    handler.client_address = ("127.0.0.1", 54321)

    assert handler._sensitive_request_error("POST", "/api/agent/self-improvement/status") == (
        403,
        "CSRF header required for sensitive local mutation",
        "CSRF_REQUIRED",
    )

    handler.headers = {"Origin": "http://localhost:8766", "X-Rumi-CSRF": "1"}
    assert handler._sensitive_request_error("POST", "/api/agent/self-improvement/status") is None


def test_memory_memo_routes_are_guarded_from_cross_origin_access():
    from domain.safety.local_guard import require_local_guard
    from transport.http import (
        _RequestHandler,
        _is_sensitive_http_path,
        _requires_sensitive_http_auth,
    )

    memo_paths = [
        "/api/memory/memo/folders",
        "/api/memory/memo/folders/personalization",
        "/api/memory/memo/notes",
        "/api/memory/memo/notes/note-1",
    ]
    for path in memo_paths:
        assert _is_sensitive_http_path(path) is True
        assert _requires_sensitive_http_auth("GET", path) is False

    assert require_local_guard(
        "/api/memory/memo/notes",
        "GET",
        {"Origin": "https://example.test"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "origin not allowed for sensitive local route",
        "ORIGIN_DENIED",
    )
    assert require_local_guard(
        "/api/memory/memo/notes",
        "POST",
        {"Origin": "http://localhost:8766"},
        ("127.0.0.1", 54321),
    ) == (
        403,
        "CSRF header required for sensitive local mutation",
        "CSRF_REQUIRED",
    )

    handler = _RequestHandler.__new__(_RequestHandler)
    sent_headers = []
    handler.path = "/api/memory/memo/notes"
    handler.headers = {"Origin": "https://example.test"}
    handler.send_header = lambda name, value: sent_headers.append((name, value))

    handler._send_cors_headers()

    assert "Access-Control-Allow-Origin" not in dict(sent_headers)


def test_non_sensitive_cors_allows_generated_csrf_header():
    from transport.http import _RequestHandler

    handler = _RequestHandler.__new__(_RequestHandler)
    sent_headers = []
    handler.path = "/api/health"
    handler.headers = {}
    handler.send_header = lambda name, value: sent_headers.append((name, value))

    handler._send_cors_headers()

    allowed_headers = dict(sent_headers)["Access-Control-Allow-Headers"]
    assert "X-Rumi-CSRF" in allowed_headers


def test_audit_redacts_secrets(tmp_path, monkeypatch):
    from domain.safety.audit import audit_path, record_attempt

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    record_attempt("test.secret", "high", {"api_key": "secret", "path": "ok.txt"})

    line = audit_path().read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["arguments"]["api_key"] == "***"
    assert payload["arguments"]["path"] == "ok.txt"


def test_viewer_local_auth_context_reaches_direct_registry_handlers():
    from domain.tool_policy.internal_context import tool_server_approval_context_is_internal
    from transport.http import DefaultsHttpServer, _LOCAL_UI_APPROVAL_CONTEXT_FLAG

    captured = {}

    def handler(args, context):
        captured["args"] = dict(args)
        captured["context"] = dict(context)
        return {"status": "ok"}

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    server.facade = object()
    result = server._invoke_registry_handler(
        handler,
        {
            _LOCAL_UI_APPROVAL_CONTEXT_FLAG: True,
            "input_text": "hello",
        },
        {},
    )

    assert result == {"status": "ok"}
    assert captured["args"] == {"input_text": "hello"}
    assert captured["context"]["_tool_server_approved"] is True
    assert tool_server_approval_context_is_internal(captured["context"])
    assert captured["context"]["source"] == "defaultspack_local_ui"
    assert captured["context"]["approval_id"] == "defaultspack_local_ui"
