from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class _RouteTableServer:
    def __getattr__(self, name):
        if name.startswith("_handle_"):
            return lambda _request_data, _path_params: {"status": "ok"}
        raise AttributeError(name)

    def _invoke_fallback_block(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _invoke_flow_route(self, *_args, **_kwargs):
        return {"status": "ok"}

    def _invoke_function_route(self, *_args, **_kwargs):
        return {"status": "ok"}


def _handler_with_fallback_routes():
    from transport.http import _RequestHandler
    from transport.registry import build_fallback_http_routes

    server = _RouteTableServer()
    server._routes = build_fallback_http_routes(server)
    handler = _RequestHandler.__new__(_RequestHandler)
    handler.server_ref = server
    handler.client_address = ("127.0.0.1", 54321)
    handler.headers = {}
    return handler


def _clear_local_auth_tokens(monkeypatch, tmp_path):
    for env_key in ("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "RUMI_API_TOKEN", "RUMI_TOKEN"):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("RUMI_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("RUMI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user" / "runtime.json"))


def test_p2p_http_routes_require_sensitive_auth(monkeypatch, tmp_path):
    from transport.http import (
        _RequestHandler,
        _is_sensitive_http_path,
        _requires_sensitive_http_auth,
    )

    guarded_routes = [
        ("POST", "/api/p2p/peers"),
        ("PUT", "/api/p2p/peers/peer-a"),
        ("POST", "/api/p2p/identity/rotate"),
        ("POST", "/api/p2p/pairing/accept"),
        ("POST", "/api/p2p/pairing/reject"),
        ("POST", "/api/p2p/messages/inbound"),
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

    _clear_local_auth_tokens(monkeypatch, tmp_path)

    handler.headers = {"Origin": "http://localhost:8766"}
    assert handler._sensitive_request_error("POST", "/api/p2p/pairing/start") == (
        403,
        "CSRF header required for sensitive integration mutation",
        "CSRF_REQUIRED",
    )

    handler.client_address = ("203.0.113.7", 54321)
    handler.headers = {"Origin": "http://localhost:8766", "X-Rumi-CSRF": "1"}
    assert handler._sensitive_request_error("POST", "/api/p2p/pairing/start") == (
        403,
        "sensitive local route requires a loopback client",
        "LOCAL_ONLY_REQUIRED",
    )

    handler.client_address = ("127.0.0.1", 54321)
    assert handler._sensitive_request_error("POST", "/api/p2p/pairing/start") is None

    for path in (
        "/api/p2p/pairing/accept",
        "/api/p2p/pairing/reject",
        "/api/p2p/messages/inbound",
        "/api/p2p/messages/send",
    ):
        assert handler._sensitive_request_error("POST", path) == (
            403,
            "local auth token is not configured",
            "AUTH_REQUIRED",
        )

    monkeypatch.setenv("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "local-secret")
    handler.headers = {"Origin": "http://localhost:8766", "Authorization": "Bearer local-secret"}
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


def test_prompt_routes_reject_token_authenticated_remote_clients(monkeypatch):
    from transport.http import _RequestHandler

    monkeypatch.setenv("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "local-secret")
    handler = _RequestHandler.__new__(_RequestHandler)
    handler.headers = {"Authorization": "Bearer local-secret"}
    handler.client_address = ("203.0.113.7", 54321)

    assert handler._sensitive_request_error("GET", "/api/prompts") == (
        403,
        "sensitive local route requires a loopback client",
        "LOCAL_ONLY_REQUIRED",
    )

    handler.client_address = ("127.0.0.1", 54321)
    assert handler._sensitive_request_error("GET", "/api/prompts") is None


def test_mobile_admin_routes_are_registered_sensitive_and_local_only():
    from transport.registry import canonical_http_route_specs

    specs = {
        (spec.method, spec.pattern): spec
        for spec in canonical_http_route_specs(include_always_available=False)
    }

    admin_routes = [
        ("POST", "/api/mobile/v1/pairings/{id}/approve"),
        ("GET", "/api/mobile/v1/pairings/{id}/review"),
        ("POST", "/api/mobile/v1/pairings/{id}/reject"),
        ("GET", "/api/mobile/v1/devices"),
        ("PATCH", "/api/mobile/v1/devices/{id}"),
        ("DELETE", "/api/mobile/v1/devices/{id}"),
    ]
    for route in admin_routes:
        assert specs[route].sensitive is True
        assert specs[route].local_only is True

    public_pairing_routes = [
        ("POST", "/api/mobile/v1/pairings/{id}/claim"),
        ("GET", "/api/mobile/v1/pairings/{id}/status"),
        ("POST", "/api/mobile/v1/pairings/{id}/token/pickup"),
    ]
    for route in public_pairing_routes:
        assert specs[route].sensitive is False
        assert specs[route].local_only is False


def test_admin_mobile_pairing_route_guard_denies_remote_and_requires_local_auth(
    monkeypatch,
    tmp_path,
):
    _clear_local_auth_tokens(monkeypatch, tmp_path)

    handler = _handler_with_fallback_routes()
    handler.client_address = ("203.0.113.7", 54321)

    for method, path in (
        ("GET", "/api/mobile/v1/pairings/pair-1/review"),
        ("POST", "/api/mobile/v1/pairings/pair-1/approve"),
        ("POST", "/api/mobile/v1/pairings/pair-1/reject"),
        ("GET", "/api/mobile/v1/devices"),
        ("PATCH", "/api/mobile/v1/devices/device-1"),
        ("DELETE", "/api/mobile/v1/devices/device-1"),
    ):
        handler.headers = {"Origin": "http://localhost:8766", "X-Rumi-CSRF": "1"}
        assert handler._sensitive_request_error(method, path) == (
            403,
            "sensitive local route requires a loopback client",
            "LOCAL_ONLY_REQUIRED",
        )

    handler.client_address = ("127.0.0.1", 54321)
    path = "/api/mobile/v1/pairings/pair-1/approve"
    handler.headers = {"Origin": "https://evil.example"}
    assert handler._sensitive_request_error("POST", path) == (
        403,
        "origin not allowed for sensitive integration route",
        "ORIGIN_DENIED",
    )

    handler.headers = {"Origin": "http://localhost:8766"}
    assert handler._sensitive_request_error("POST", path) == (
        403,
        "local auth token is not configured",
        "AUTH_REQUIRED",
    )

    monkeypatch.setenv("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "local-secret")
    assert handler._sensitive_request_error("POST", path) == (
        401,
        "local auth token required",
        "AUTH_REQUIRED",
    )

    handler.headers = {
        "Origin": "http://localhost:8766",
        "Authorization": "Bearer local-secret",
    }
    assert handler._sensitive_request_error("POST", path) == (
        403,
        "CSRF header required for sensitive integration mutation",
        "CSRF_REQUIRED",
    )

    handler.headers = {
        "Origin": "http://localhost:8766",
        "Authorization": "Bearer local-secret",
        "X-Rumi-CSRF": "1",
    }
    assert handler._sensitive_request_error("POST", path) is None
