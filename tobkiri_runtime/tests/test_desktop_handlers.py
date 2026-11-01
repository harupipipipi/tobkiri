"""Retirement tests for the historical desktop-token HTTP route."""

from __future__ import annotations

import http.client
import json
import unittest
from typing import Mapping

from core_runtime.pack_api_server import PackAPIHandler, PackAPIServer
from core_runtime.panel_auth import PanelAuthManager


class _Dispatch:
    """Minimal captured Host identity required by panel authentication."""

    profile_id = "defaults"
    profile_revision = "sha256:" + "b" * 64
    activation_id = "activation:desktop-handlers"
    plan_digest = "sha256:" + "a" * 64
    security_epoch = 1

    def assert_current(self) -> None:
        """Provide the verified dispatch-session check used by the API server."""


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if encoded is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    response_headers = response.getheaders()
    connection.close()
    return response.status, payload, response_headers


class TestDesktopHandlers(unittest.TestCase):
    """The removed desktop capability route cannot reach a handler or store."""

    def setUp(self) -> None:
        self.server = PackAPIServer(
            port=0,
            panel_auth_manager=PanelAuthManager(
                bootstrap_secret="verified-desktop"
            ),
            dispatch_session=_Dispatch(),
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def _assert_retired(
        self,
        method: str = "POST",
        body: object | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        status, payload, _ = _request(
            self.server,
            method,
            "/api/desktop/token",
            body,
            headers,
        )
        assert status == 410
        assert payload["success"] is False
        assert payload["data"] == {
            "api_version": "io.tobkiri.pack-api.v4",
            "state": "legacy_api_retired",
            "retired_route": "/api/desktop/token",
            "write_set": [],
        }
        assert payload["error"] == (
            "Legacy API route is retired; use an exact Pack v4 operation"
        )

    def _panel_session(self) -> tuple[str, str]:
        origin = f"http://127.0.0.1:{self.server.port}"
        status, bootstrap, _ = _request(
            self.server,
            "POST",
            "/api/panel/auth/bootstrap",
            {},
            {"X-Rumi-Desktop-Bootstrap": "verified-desktop"},
        )
        assert status == 200
        code = bootstrap["data"]["code"]
        status, exchange, response_headers = _request(
            self.server,
            "POST",
            "/api/panel/auth/exchange",
            {"code": code},
            {"Origin": origin},
        )
        assert status == 200
        cookie = next(
            value
            for key, value in response_headers
            if key.lower() == "set-cookie"
        )
        return cookie.split(";", 1)[0], str(exchange["data"]["csrf_token"])

    def test_missing_pack_id(self):
        """A missing legacy payload is retired before any handler dispatch."""
        self._assert_retired(body={})

    def test_empty_pack_id(self):
        """An empty legacy payload is retired before validation."""
        self._assert_retired(body={"pack_id": "  "})

    def test_invalid_pack_id(self):
        """A legacy Pack ID cannot enter the removed capability route."""
        self._assert_retired(body={"pack_id": "bad_pack"})

    def test_no_grant_returns_403(self):
        """No legacy grant manager is consulted at the v4 boundary."""
        self._assert_retired(body={"pack_id": "test_pack"})

    def test_handler_not_available(self):
        """The retired route has no dependency-injection handler fallback."""
        self._assert_retired(body={"pack_id": "test_pack"})

    def test_success(self):
        """A verified panel session still cannot authorize a retired route."""
        cookie, csrf_token = self._panel_session()
        self._assert_retired(
            body={"pack_id": "test_pack"},
            headers={"Cookie": cookie, "X-Rumi-CSRF": csrf_token},
        )

    def test_handler_error(self):
        """Removed handler errors are replaced by one typed retirement envelope."""
        self._assert_retired(body={"pack_id": "test_pack"})

    def test_persist_desktop_api_token_writes_next_to_user_data(self):
        """The removed filesystem token projection is absent and write-free."""
        assert not hasattr(PackAPIHandler, "_persist_desktop_api_token")
        self._assert_retired(body={"token": "desktop-token"})


if __name__ == "__main__":
    unittest.main()
