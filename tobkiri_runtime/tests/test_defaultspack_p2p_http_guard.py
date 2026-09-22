from __future__ import annotations

import sys
from pathlib import Path

from core_runtime.host_contract import bind_host_contract

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_p2p_http_routes_require_sensitive_auth():
    from transport.http import (
        _RequestHandler,
        _is_sensitive_http_path,
        _requires_sensitive_http_auth,
    )

    guarded_routes = [
        ("POST", "/api/p2p/peers"),
        ("PUT", "/api/p2p/peers/peer-a"),
        ("POST", "/api/p2p/identity/rotate"),
        ("POST", "/api/p2p/messages/send"),
        ("POST", "/api/integrations/p2p/events"),
    ]
    for method, path in guarded_routes:
        assert _is_sensitive_http_path(path) is True
        assert _requires_sensitive_http_auth(method, path) is True

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Origin": "https://evil.example"}
    handler.client_address = ("127.0.0.1", 54321)

    assert handler._sensitive_request_error("POST", "/api/p2p/peers") == (
        403,
        "origin not allowed for sensitive integration route",
        "ORIGIN_DENIED",
    )

    handler.headers = {"Origin": "http://localhost:8766", "Authorization": "Bearer local-secret"}
    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"desktop_api_token": "local-secret"},
        }
    ):
        assert handler._sensitive_request_error("POST", "/api/integrations/p2p/events") == (
            403,
            "CSRF header required for sensitive integration mutation",
            "CSRF_REQUIRED",
        )

        handler.headers = {
            "Origin": "http://localhost:8766",
            "Authorization": "Bearer local-secret",
            "X-Rumi-CSRF": "1",
        }
        assert handler._sensitive_request_error("POST", "/api/integrations/p2p/events") is None


def test_prompt_routes_reject_token_authenticated_remote_clients():
    from transport.http import _RequestHandler

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Authorization": "Bearer local-secret"}
    handler.client_address = ("203.0.113.7", 54321)

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"desktop_api_token": "local-secret"},
        }
    ):
        assert handler._sensitive_request_error("GET", "/api/prompts") == (
            403,
            "sensitive local route requires a loopback client",
            "LOCAL_ONLY_REQUIRED",
        )

        handler.client_address = ("127.0.0.1", 54321)
        assert handler._sensitive_request_error("GET", "/api/prompts") is None
