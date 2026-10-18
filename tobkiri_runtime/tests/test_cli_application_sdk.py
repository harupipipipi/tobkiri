"""CLI transport/stdio against an explicitly isolated HTTP test endpoint, not live Host."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import threading

import pytest

from core_runtime.pack_authoring import PackAuthoringError
from tests.test_pack_authoring import _build
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.models import PackageKind
from tobkiri_protocol.cli_application import (
    CliApplicationError,
    PanelContractSession,
    run_application_stdio,
)
from tobkiri_protocol.validation import validate_document


@pytest.fixture
def endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, dict(self.headers)))
            if self.path == "/api/panel/auth/exchange":
                if body != {"code": "explicit-test-bootstrap"}:
                    self.send_error(401)
                    return
                payload = {
                    "success": True,
                    "data": {"csrf_token": "test-csrf", "journal_scope": "test-scope"},
                }
                self.send_response(200)
                self.send_header("Set-Cookie", "panel=test; Path=/")
            else:
                assert self.headers["X-Rumi-CSRF"] == "test-csrf"
                assert self.headers["Cookie"] == "panel=test"
                assert self.headers["X-Tobkiri-Request-ID"]
                assert self.path == "/api/contracts/example/POST%20%2Fapi%2Frender"
                payload = {
                    "success": True,
                    "data": {
                        "status": "ok",
                        "value": {
                            "stdout": body["text"],
                            "stderr": "",
                            "exit_status": 0,
                            "stream": "complete",
                        },
                    },
                }
                self.send_response(200)
            raw = json.dumps(payload).encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    thread.join()
    server.server_close()


def _frame(**changes):
    value = {
        "protocol": "io.tobkiri.cli.io.v1",
        "type": "command",
        "request_id": "cli:req:frontend001",
        "command": "application.invoke",
        "arguments": {"command_id": "render", "input": {"text": "hello"}},
        "stdin": None,
        "tty": False,
        "output_limit": 256,
    }
    value.update(changes)
    return value


def _declarations():
    return {
        "render": {
            "namespace": "example",
            "path": "/api/render",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    }


def test_stdio_uses_finite_authenticated_http_without_importing_renderer(endpoint):
    url, calls = endpoint
    session = PanelContractSession(url, "explicit-test-bootstrap")
    output = StringIO()
    run_application_stdio(session, _declarations(), StringIO(json.dumps(_frame()) + "\n"), output)
    result = validate_document(output.getvalue(), "cli_io")
    assert result["stdout"] == "hello" and result["type"] == "result"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"profile_id": "foreign"},
        {"shell_provider_id": "foreign"},
        {"arguments": {"command_id": "render", "input": {}, "approved": True}},
        {"arguments": {"command_id": "unselected", "input": {"text": "hello"}}},
        {"arguments": {"command_id": "render", "input": {"text": "hello", "approved": True}}},
    ],
)
def test_identity_approval_unknown_command_and_closed_input_fail_before_http(endpoint, change):
    url, calls = endpoint
    session = PanelContractSession(url, "explicit-test-bootstrap")
    output = StringIO()
    run_application_stdio(
        session, _declarations(), StringIO(json.dumps(_frame(**change)) + "\n"), output
    )
    assert validate_document(output.getvalue(), "cli_io")["type"] == "error"
    assert len(calls) == 1


def test_cancel_does_not_invoke_renderer(endpoint):
    url, calls = endpoint
    session = PanelContractSession(url, "explicit-test-bootstrap")
    output = StringIO()
    run_application_stdio(
        session, _declarations(), StringIO(json.dumps(_frame(cancel=True)) + "\n"), output
    )
    assert validate_document(output.getvalue(), "cli_io")["stream"] == "cancelled"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8080",
        "http://127.0.0.1:8000/private",
        "http://secret@127.0.0.1:8000",
    ],
)
def test_cli_cannot_use_remote_endpoint_credentials_or_ambient_fallback(url):
    with pytest.raises(CliApplicationError, match="loopback"):
        PanelContractSession(url, "explicit-test-bootstrap")


def test_denied_bootstrap_does_not_fallback(endpoint):
    url, calls = endpoint
    with pytest.raises(CliApplicationError, match="denied"):
        PanelContractSession(url, "denied")
    assert len(calls) == 1


def test_application_source_uses_actual_compiler_and_normal_boundaries(tmp_path: Path):
    root = _build(tmp_path / "application", application=True)
    compiled = compile_pack_root(root)
    # Application is the composition role; it remains a normal sandbox artifact.
    assert compiled.artifact.package_kind is PackageKind.NORMAL
    assert json.loads((root / "pack.v4.json").read_text())["pack"]["kind"] == "application"
    manifest = json.loads((root / "pack.v4.json").read_text())
    assert manifest["requirements"]["capabilities"] == []
    assert manifest["requirements"]["execution_boundary"] == "sandbox"


def test_application_flag_cannot_be_a_client_truthy_approval(tmp_path: Path):
    with pytest.raises(PackAuthoringError, match="one pure Function"):
        _build(tmp_path / "application", application="approved")


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_remote_schema_reference_is_denied_before_contract_http(endpoint, keyword):
    base, calls = endpoint
    session = PanelContractSession(base, "explicit-test-bootstrap")
    declaration = _declarations()["render"]
    declaration["input_schema"] = {keyword: "https://example.invalid/schema"}
    with pytest.raises(CliApplicationError, match="must be local"):
        session.invoke(declaration, {"text": "hello"})
    assert len(calls) == 1  # bootstrap only, no schema fetch or Contract call
