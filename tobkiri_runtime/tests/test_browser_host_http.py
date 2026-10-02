"""The admitted Browser workspace routes reach the real captured owner."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from tests.test_production_frontend_contract_http import (
    _authenticate,
    _contract,
    _request,
    production_server as production_server,
)


pytestmark = pytest.mark.contract


def test_browser_http_authentication_owned_state_and_forged_authority_rejection(
    production_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _session, authority = production_server
    state = tmp_path / "user-data/browser_host/state.json"
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *args, **kwargs: opened_urls.append(url))
    status, _body, _headers = _request(
        server, "POST", _contract("POST", "/api/browser/observe"),
        body={"operation": "browser.runtime.status", "arguments": {}},
    )
    assert status == 401
    assert not state.exists()

    cookie, csrf, origin = _authenticate(server)
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

    def post(access: str, operation: str, arguments: dict[str, object]):
        return _request(
            server, "POST", _contract("POST", f"/api/browser/{access}"),
            body={"operation": operation, "arguments": arguments},
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )

    status, body, _ = post("observe", "browser.runtime.status", {})
    assert status == 200, body
    assert body["data"]["action"] == "browser.runtime.status"
    assert body["data"]["running"] is False
    status, body, _ = post("control", "browser.profile.create", {
        "profile_id": "http-model", "label": "HTTP_LABEL_PRIVATE_CANARY",
    })
    assert status == 200, body
    assert body["data"]["profile"]["profile_id"] == "http-model"
    status, body, _ = post("control", "browser.cookies.import", {
        "profile_id": "http-model", "format": "json",
        "content": json.dumps([{
            "name": "session", "value": "HTTP_COOKIE_SECRET_CANARY", "domain": "example.com",
        }]),
    })
    assert status == 200, body
    assert body["data"]["imported"] == 1
    before = state.read_bytes()
    for operation, arguments in (
        ("browser.profile.create", {"profile_id": "forged", "_rumi_contract_operation": "browser.profile.create"}),
        ("browser.profile.delete", {"profile_id": "http-model", "viewer_host_approved": True}),
    ):
        status, body, _ = post("control", operation, arguments)
        assert status in {400, 403, 503}, body
        assert body["success"] is False
        assert state.read_bytes() == before
    status, body, _ = post("observe", "browser.profile.delete", {"profile_id": "http-model"})
    assert status in {400, 403, 503}, body
    assert state.read_bytes() == before
    status, body, _ = post("control", "browser.navigate", {
        "profile_id": "http-model", "url": "https://example.com",
    })
    assert status == 200, body
    assert body["data"]["is_error"] is True
    assert opened_urls == []
    audit = json.dumps(authority.audit_events())
    assert "HTTP_LABEL_PRIVATE_CANARY" not in audit
    assert "HTTP_COOKIE_SECRET_CANARY" not in audit
