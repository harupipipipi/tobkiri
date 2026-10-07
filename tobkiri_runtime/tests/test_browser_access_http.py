"""Real HTTP regressions for local browser admission and native decisions."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Iterator

import pytest

from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from tests.test_panel_auth_capture_fencing import _CapturedDispatch, _binding

PREFIX = "/api/panel/browser-access/"
SECRET = "test-native-secret"


@pytest.fixture
def browser_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[PackAPIServer]:
    binding = _binding(activation_id="activation:browser-http", security_epoch=3)
    web_root = tmp_path / "ui"
    web_root.mkdir()
    (web_root / "shell.html").write_text("authenticated Defaults", encoding="utf-8")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret=SECRET),
        dispatch_session=_CapturedDispatch(binding, [binding]),
        web_mounts=(
            {
                "path_prefix": "/p",
                "web_root": web_root,
                "spa_fallback": True,
                "index_file": "shell.html",
                "auth_required": True,
                "auth_bootstrap": True,
            },
        ),
    )
    monkeypatch.setattr(
        "core_runtime.api.browser_access.open_browser_access_window", lambda *args: None
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _post(
    server: PackAPIServer,
    action: str,
    body: object,
    *,
    cookie: str = "",
    native: bool = False,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict, list[tuple[str, str]]]:
    raw = json.dumps(body).encode()
    path = PREFIX + action
    request_headers = {"Content-Type": "application/json"}
    if native:
        timestamp, nonce = str(int(time.time())), secrets.token_hex(32)
        message = (
            f"tobkiri.browser-access.request.v1\n{timestamp}\n{nonce}\nPOST\n"
            f"{path}\n{hashlib.sha256(raw).hexdigest()}"
        )
        proof = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
        request_headers.update(
            {
                "X-Tobkiri-Browser-Access-Timestamp": timestamp,
                "X-Tobkiri-Browser-Access-Nonce": nonce,
                "X-Tobkiri-Browser-Access-Proof": proof,
            }
        )
    else:
        request_headers["Origin"] = f"http://127.0.0.1:{server.port}"
    if cookie:
        request_headers["Cookie"] = cookie
    request_headers.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.request("POST", path, body=raw, headers=request_headers)
    response = connection.getresponse()
    status, response_headers, response_raw = (
        response.status,
        response.getheaders(),
        response.read(),
    )
    connection.close()
    payload = json.loads(response_raw)
    if native and status != 401:
        actual = next(
            value
            for key, value in response_headers
            if key.lower() == "x-tobkiri-browser-access-response-proof"
        )
        message = (
            f"tobkiri.browser-access.response.v1\n{timestamp}\n{nonce}\nPOST\n"
            f"{path}\n{status}\n{hashlib.sha256(response_raw).hexdigest()}"
        )
        expected = hmac.new(
            SECRET.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(actual, expected)
    return status, payload, response_headers


def _create(server: PackAPIServer) -> tuple[str, str]:
    status, payload, headers = _post(server, "request", {"target": "/p/defaults/chat"})
    assert status == 200, payload
    cookie = next(value for key, value in headers if key.lower() == "set-cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Path=/api/panel/browser-access" in cookie
    assert "proof" not in payload["data"]
    return payload["data"]["request_id"], cookie.split(";", 1)[0]


def test_request_native_approval_claim_and_authenticated_mount(
    browser_server: PackAPIServer,
) -> None:
    server = browser_server
    request_id, cookie = _create(server)
    assert server._panel_auth_manager._active_sessions == {}
    assert _post(server, "claim", {"request_id": request_id}, cookie=cookie)[0] == 409
    status, context, _ = _post(
        server, "context", {"request_id": request_id}, native=True
    )
    assert status == 200
    assert context["data"]["origin"] == f"http://127.0.0.1:{server.port}"
    assert context["data"]["profile_id"] == "defaults"
    assert "proof" not in context["data"]
    assert (
        _post(
            server,
            "decision",
            {"request_id": request_id, "decision": "approved"},
            native=True,
        )[0]
        == 200
    )
    assert (
        _post(server, "status", {"request_id": request_id}, cookie=cookie)[1]["data"][
            "status"
        ]
        == "approved"
    )
    status, login, headers = _post(
        server, "claim", {"request_id": request_id}, cookie=cookie
    )
    assert status == 200
    assert login["data"]["csrf_token"] and login["data"]["journal_scope"]
    assert "session_id" not in login["data"]
    assert login["data"]["target"] == "/p/defaults/chat"
    panel_cookie = next(
        value.split(";", 1)[0]
        for key, value in headers
        if key.lower() == "set-cookie" and value.startswith("rumi_panel_session=")
    )
    session = server._panel_auth_manager.verify_session(
        panel_cookie.split("=", 1)[1],
        server.handler_class._current_panel_auth_binding(),
    )
    assert session is not None and not session.get("request_scope")
    assert _post(server, "claim", {"request_id": request_id}, cookie=cookie)[0] == 409
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.request("GET", "/p/defaults/chat", headers={"Cookie": panel_cookie})
    response = connection.getresponse()
    assert response.status == 200
    assert response.read() == b"authenticated Defaults"
    connection.close()


@pytest.mark.parametrize(
    "target",
    [
        [],
        {},
        None,
        "https://example.com",
        "//evil",
        "/p/other/chat",
        "/approval",
        "/p/defaults/chat?x=1",
        "/p/defaults/../chat",
    ],
)
def test_browser_cannot_select_foreign_or_invalid_target(
    browser_server: PackAPIServer, target: object
) -> None:
    assert _post(browser_server, "request", {"target": target})[0] == 409


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": ""},
        {"Host": "evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_browser_origin_host_and_fetch_site_are_required(
    browser_server: PackAPIServer, headers: dict[str, str]
) -> None:
    assert (
        _post(
            browser_server, "request", {"target": "/p/defaults/chat"}, headers=headers
        )[0]
        == 403
    )


def test_browser_cannot_approve_or_steal_another_claim(
    browser_server: PackAPIServer,
) -> None:
    request_id, cookie = _create(browser_server)
    assert (
        _post(
            browser_server,
            "decision",
            {"request_id": request_id, "decision": "approved"},
            cookie=cookie,
        )[0]
        == 401
    )
    assert _post(browser_server, "status", {"request_id": request_id})[0] == 409
    assert _post(browser_server, "claim", {"request_id": request_id})[0] == 409
    assert (
        _post(
            browser_server, "request", {"target": "/p/defaults/chat", "approved": True}
        )[0]
        == 400
    )
    assert (
        _post(
            browser_server,
            "context",
            {"request_id": request_id},
            native=True,
            headers={"X-Tobkiri-Browser-Access-Proof": "0" * 64},
        )[0]
        == 401
    )


def test_denial_idempotence_and_explicit_retry(browser_server: PackAPIServer) -> None:
    request_id, cookie = _create(browser_server)
    status, repeated, _ = _post(
        browser_server, "request", {"target": "/p/defaults/chat"}, cookie=cookie
    )
    assert status == 200 and repeated["data"]["request_id"] == request_id
    assert (
        _post(
            browser_server,
            "decision",
            {"request_id": request_id, "decision": "denied"},
            native=True,
        )[0]
        == 200
    )
    assert (
        _post(browser_server, "claim", {"request_id": request_id}, cookie=cookie)[0]
        == 409
    )
    assert (
        _post(browser_server, "status", {"request_id": request_id}, cookie=cookie)[1][
            "data"
        ]["status"]
        == "denied"
    )
    assert (
        _post(browser_server, "request", {"target": "/p/defaults/chat"}, cookie=cookie)[
            1
        ]["data"]["request_id"]
        != request_id
    )


def test_capture_switch_invalidates_pending_browser_and_native_requests(
    browser_server: PackAPIServer,
) -> None:
    request_id, cookie = _create(browser_server)
    dispatch = browser_server.handler_class._dispatch_session
    dispatch.current[0] = replace(dispatch.binding, security_epoch=4)
    assert (
        _post(browser_server, "status", {"request_id": request_id}, cookie=cookie)[0]
        == 409
    )
    assert (
        _post(
            browser_server,
            "decision",
            {"request_id": request_id, "decision": "approved"},
            native=True,
        )[0]
        == 409
    )
    assert browser_server._panel_auth_manager._active_sessions == {}


def test_launcher_unavailable_does_not_issue_any_session(
    browser_server: PackAPIServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object) -> None:
        raise RuntimeError("sanitized failure")

    monkeypatch.setattr(
        "core_runtime.api.browser_access.open_browser_access_window", unavailable
    )
    status, payload, headers = _post(
        browser_server, "request", {"target": "/p/defaults/chat"}
    )
    assert status == 503 and payload["data"]["code"] == "launcher_unavailable"
    assert not any(key.lower() == "set-cookie" for key, _ in headers)
    assert browser_server._panel_auth_manager._active_sessions == {}


def test_bootstrap_page_has_request_button_csp_and_no_credential(
    browser_server: PackAPIServer,
) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", browser_server.port, timeout=5)
    connection.request("GET", "/p/defaults/chat")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
    document = response.read().decode()
    assert (
        "アクセスをリクエスト" in document and "const requestAllowed=true" in document
    )
    assert SECRET not in document and "X-Rumi-Desktop-Bootstrap" not in document
    assert "/api/panel/auth/exchange" in document
    connection.close()
