"""Privacy and delivery checks for fixed Shell HTTP failure diagnostics."""

from __future__ import annotations

import io
import logging
from urllib.parse import quote

import pytest

from core_runtime.api.api_response import APIResponse
from core_runtime.api.http_response import ResponseWriterMixin
from core_runtime.http_failure_diagnostic import defer_ui_read_failure


def _route(target: str) -> str:
    return "/api/contracts/defaultspack/" + quote("GET " + target, safe="")


class _ClosedResponse:
    def finish(self) -> None:
        self.closed = True

    def log_request(self, status: int, length: int) -> None:
        pass


class _Writer(ResponseWriterMixin, _ClosedResponse):
    def __init__(self, target: str) -> None:
        self.command = "GET"
        self.path = _route(target)
        self.wfile = io.BytesIO()
        self.headers = {}
        self._panel_session_cookie = None
        self._CLIENT_DISCONNECT_EXCEPTIONS = (BrokenPipeError,)
        self.closed = False
        self.sent_status = None

    def send_response_only(self, status: int) -> None:
        self.sent_status = status

    def send_header(self, name: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def version_string(self) -> str:
        return "test"

    def date_time_string(self) -> str:
        return "test"

    def _get_cors_origin(self, origin: str) -> None:
        return None


@pytest.mark.parametrize(
    ("target", "label"),
    [
        ("/api/ui/full-catalog", "catalog"),
        ("/api/tools/catalog", "catalog"),
        ("/api/ai/strategies", "catalog"),
        ("/api/ui/settings?full=true&private=secret", "settings"),
        ("/api/command-protocol/v1/catalog", "commands"),
    ],
)
def test_actual_response_delivers_unchanged_before_fixed_log(
    target: str, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    writer = _Writer(target)
    response = APIResponse(
        False,
        data={"code": "TIMEOUT", "private": "payload-secret"},
        error="private-error",
    )
    with caplog.at_level(logging.WARNING):
        writer._send_response(response, 504)
        assert writer.sent_status == 504
        assert writer.wfile.getvalue() == response.to_json().encode()
        assert not caplog.records
        queued = writer._completed_diagnostic_logs
        assert len(queued) == 1
        assert queued[0][3] == (label, 504, "TIMEOUT")
        assert queued[0][4] is None
        writer.finish()
    assert writer.closed
    assert [record.getMessage() for record in caplog.records] == [
        f"Shell UI read failed interface={label} http_status=504 code=TIMEOUT"
    ]


@pytest.mark.parametrize(
    "data", [None, {}, {"code": "cookie-secret"}, {"code": ["TIMEOUT"]}, {"code": 123}]
)
def test_unknown_or_missing_code_is_never_reflected(
    data: object, caplog: pytest.LogCaptureFixture
) -> None:
    writer = _Writer("/api/ui/settings?cookie=private")
    with caplog.at_level(logging.WARNING):
        writer._send_response(APIResponse(False, data=data, error="private"), 401)
        writer.finish()
    assert [record.getMessage() for record in caplog.records] == [
        "Shell UI read failed interface=settings http_status=401 code=unknown"
    ]


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", _route("/api/ui/settings"), 500),
        ("GET", _route("/api/chat/conversation?conversation_id=private"), 500),
        ("GET", "/api/ui/settings", 500),
        ("GET", _route("/api/ui/settings").replace("defaultspack", "private"), 500),
        ("GET", _route("/api/ui/settings"), 200),
        ("GET", _route("/api/ui/settings"), True),
        ("GET", _route("https://private/api/ui/settings"), 500),
        ("GET", _route("/api/ui/settings#private"), 500),
        ("GET", "/api/contracts/defaultspack/" + "private" * 2000, 500),
    ],
)
def test_unrelated_malformed_and_success_responses_are_silent(
    method: object, path: object, status: object
) -> None:
    writer = _Writer("/api/ui/settings")
    defer_ui_read_failure(
        writer,
        method=method,
        request_path=path,
        status=status,
        response_data={"code": "TIMEOUT"},
    )
    assert not getattr(writer, "_completed_diagnostic_logs", [])


def test_logging_failure_preserves_close_and_existing_access_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime import http_failure_diagnostic

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private sink error")

    monkeypatch.setattr(http_failure_diagnostic._LOGGER, "warning", broken)
    writer = _Writer("/api/ui/settings")
    response = APIResponse(False, data={"code": "API_FAILURE"})
    writer._send_response(response, 503)
    writer.finish()
    assert writer.closed
    assert writer.wfile.getvalue() == response.to_json().encode()
    assert writer._completed_access_logs == []


def test_disconnect_does_not_claim_a_delivered_failure() -> None:
    class Disconnected(io.BytesIO):
        def write(self, value: bytes) -> int:
            raise BrokenPipeError

    writer = _Writer("/api/ui/settings")
    writer.wfile = Disconnected()
    writer._send_response(APIResponse(False), 503)
    assert writer.close_connection
    assert not getattr(writer, "_completed_diagnostic_logs", [])
