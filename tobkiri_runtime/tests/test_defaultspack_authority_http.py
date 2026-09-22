from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class _FakeAuthorityService:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.approve_kwargs = []

    def approve_request(
        self,
        request_id,
        *,
        scope="once",
        config=None,
        expires_in_seconds=None,
        related_permissions=None,
        ui_operator=None,
        actor_principal=None,
    ):
        self.calls.append((request_id, scope, config, expires_in_seconds))
        self.approve_kwargs.append({
            "related_permissions": related_permissions,
            "ui_operator": ui_operator,
            "actor_principal": actor_principal,
        })
        return self.result

    def deny_request(self, request_id, *, reason="", persist=False, ui_operator=None, actor_principal=None):
        self.calls.append((request_id, reason, persist))
        return self.result

    def list_requests(self, status="all", *, actor_principal=None):
        self.calls.append((status,))
        return self.result


def test_authority_http_approve_returns_decision(monkeypatch):
    from blocks.authority import requests

    service = _FakeAuthorityService({
        "success": True,
        "approved": True,
        "request_id": "auth_1",
        "scope": "once",
        "token": "approval-token",
    })
    monkeypatch.setattr(requests, "_authority_service", lambda: service)

    result = requests.run({
        "action": "approve",
        "request_id": "auth_1",
        "scope": "once",
        "config": {"provider_ids": ["opencode-go"]},
        "expires_in_seconds": "60",
    })

    assert result["status"] == "ok"
    assert result["data"]["approved"] is True
    assert result["data"]["token"] == "approval-token"
    assert service.calls == [("auth_1", "once", {"provider_ids": ["opencode-go"]}, 60)]


def test_authority_http_transport_cannot_auto_approve_from_payload(tmp_path):
    from tests.v4_batch_support import (
        assert_lease_is_single_use,
        assert_payload_mutations_denied,
        harness,
    )

    authority = harness(tmp_path)
    assert_payload_mutations_denied(authority)
    assert_lease_is_single_use(authority)


def _browser_exchange_request(request_id="auth_1"):
    return {
        "request_id": request_id,
        "device_id": "fake-device-1",
        "window_id": "fake-window-1",
        "nonce": "fake-client-nonce-1",
        "origin": "http://127.0.0.1:8766",
        "_headers": {"Origin": "http://127.0.0.1:8766"},
        "_authority_subject": {"principal_id": "local-ui:fake-principal-digest"},
    }


def test_authority_browser_ui_operator_rejects_legacy_credential(monkeypatch):
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    request = _browser_exchange_request()
    request["browser_approval_token"] = "unmistakably-fake-revoked-value"

    result = server._handle_authority_browser_ui_operator(request, {})

    assert result["status"] == "error"
    assert result["_http_status"] == 410
    assert result["error"]["code"] == "LEGACY_BROWSER_APPROVAL_REVOKED"
    assert "unmistakably-fake" not in str(result)


def test_authority_browser_exchange_cannot_elevate_local_ui_bearer():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    result = server._handle_authority_browser_exchange(
        _browser_exchange_request(), {}
    )

    assert result["status"] == "error"
    assert result["_http_status"] == 404
    assert result["error"]["code"] == "AUTHORITY_BROWSER_TEST_DISABLED"


def test_authority_browser_ui_operator_cannot_mint_from_http_exchange(monkeypatch):
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer
    monkeypatch.setenv("RUMI_PANEL_BOOTSTRAP_SECRET", "fake-signing-key-" + ("x" * 32))
    server = DefaultsHttpServer.__new__(DefaultsHttpServer)

    redeem_request = _browser_exchange_request()
    redeem_request["exchange_code"] = "fake-attacker-selected-code"
    result = server._handle_authority_browser_ui_operator(redeem_request, {})

    assert result["status"] == "error"
    assert result["_http_status"] == 404
    assert result["error"]["code"] == "AUTHORITY_BROWSER_TEST_DISABLED"
    assert "fake-attacker-selected-code" not in str(result)


def test_authority_http_errors_preserve_status(monkeypatch):
    from blocks.authority import requests

    service = _FakeAuthorityService({
        "success": False,
        "error": "Authority request not found",
        "status_code": 404,
    })
    monkeypatch.setattr(requests, "_authority_service", lambda: service)

    result = requests.run({"action": "approve", "request_id": "missing"})

    assert result["status"] == "error"
    assert result["_http_status"] == 404
    assert result["error"]["code"] == "AUTHORITY_APPROVAL_FAILED"
