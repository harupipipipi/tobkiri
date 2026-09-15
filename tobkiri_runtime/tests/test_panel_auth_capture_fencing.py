"""Capture and Authority fencing for launcher-issued panel credentials."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Mapping

from core_runtime.pack_api_server import PackAPIHandler, PackAPIServer
from core_runtime.panel_auth import PanelAuthBinding, PanelAuthManager


PROFILE_REVISION = "sha256:" + "1" * 64
PLAN_DIGEST = "sha256:" + "2" * 64


def _binding(*, activation_id: str, security_epoch: int) -> PanelAuthBinding:
    return PanelAuthBinding(
        profile_id="defaults",
        profile_revision=PROFILE_REVISION,
        activation_id=activation_id,
        plan_digest=PLAN_DIGEST,
        security_epoch=security_epoch,
    )


def test_codes_and_sessions_are_bound_to_exact_current_capture() -> None:
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    capture_a = _binding(activation_id="activation:capture-a", security_epoch=7)
    capture_b = _binding(activation_id="activation:capture-b", security_epoch=7)
    epoch_b = _binding(activation_id="activation:capture-b", security_epoch=8)

    stale_code = str(manager.issue_login_code(capture_a)["code"])
    assert manager.exchange_code(stale_code, capture_b) is None
    assert manager._active_sessions == {}

    # A failed stale comparison neither consumes the one-time code nor mints a
    # session. The exact original capture can still linearize the exchange.
    exchanged = manager.exchange_code(stale_code, capture_a)
    assert exchanged is not None
    session_id = str(exchanged["session_id"])
    assert manager.verify_session(session_id, capture_a) is not None
    assert manager.verify_session(session_id, capture_b) is None
    assert manager.verify_session(session_id, epoch_b) is None

    current_code = str(manager.issue_login_code(epoch_b)["code"])
    current = manager.exchange_code(current_code, epoch_b)
    assert current is not None
    assert manager.verify_session(str(current["session_id"]), epoch_b) is not None


@dataclass(frozen=True)
class _CapturedDispatch:
    binding: PanelAuthBinding
    current: list[PanelAuthBinding]

    @property
    def profile_id(self) -> str:
        return self.binding.profile_id

    @property
    def profile_revision(self) -> str:
        return self.binding.profile_revision

    @property
    def activation_id(self) -> str:
        return self.binding.activation_id

    @property
    def plan_digest(self) -> str:
        return self.binding.plan_digest

    @property
    def security_epoch(self) -> int:
        return self.binding.security_epoch

    def assert_current(self) -> None:
        if self.current[0] != self.binding:
            raise RuntimeError("captured Profile activation is stale")


class _PackVMLifecycle:
    def __init__(self) -> None:
        self.doctor_calls = 0

    def doctor(self) -> Mapping[str, object]:
        self.doctor_calls += 1
        return {"ready": True}


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    response_headers = response.getheaders()
    connection.close()
    return response.status, payload, response_headers


def _publish_capture(server: PackAPIServer, session: _CapturedDispatch) -> None:
    """Publish the same immutable auth/session pair as runtime refresh."""

    handler = PackAPIHandler.canonical_v4_server_handler(
        panel_auth_manager=server._panel_auth_manager,
        dispatch_session=session,
        app_lifecycle_manager=server.app_lifecycle_manager,
        replay_guard=server._replay_guard,
        operation_journal=server._operation_journal,
        packvm_lifecycle=server._packvm_lifecycle,
    )
    handler._runtime_port = server.port
    server.handler_class = handler
    assert server.server is not None
    server.server.RequestHandlerClass = handler


def test_pack_api_rejects_stale_exchange_and_cookie_before_invocation() -> None:
    capture_a = _binding(activation_id="activation:capture-a", security_epoch=11)
    capture_b = _binding(activation_id="activation:capture-b", security_epoch=11)
    epoch_b = _binding(activation_id="activation:capture-b", security_epoch=12)
    current = [capture_a]
    lifecycle = _PackVMLifecycle()
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(capture_a, current),
        packvm_lifecycle=lifecycle,  # type: ignore[arg-type]
    )
    server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"
        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap

        current[0] = capture_b
        _publish_capture(server, _CapturedDispatch(capture_b, current))
        status, rejected, _ = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={"code": bootstrap["data"]["code"]},
            headers={"Origin": origin},
        )
        assert status == 401, rejected
        assert manager._active_sessions == {}

        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, exchanged, response_headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={"code": bootstrap["data"]["code"]},
            headers={"Origin": origin},
        )
        assert status == 200, exchanged
        cookie = next(
            value
            for key, value in response_headers
            if key.lower() == "set-cookie"
        ).split(";", 1)[0]
        status, healthy, _ = _request(
            server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": cookie},
        )
        assert status == 200, healthy
        assert lifecycle.doctor_calls == 1

        current[0] = epoch_b
        _publish_capture(server, _CapturedDispatch(epoch_b, current))
        status, rejected, _ = _request(
            server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": cookie},
        )
        assert status == 401, rejected
        assert lifecycle.doctor_calls == 1
    finally:
        server.stop()


def test_pack_api_without_dispatch_cannot_mint_or_exchange_credentials() -> None:
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
    )
    server.start()
    try:
        status, rejected, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 401, rejected
        assert manager._active_codes == {}
        assert manager._active_sessions == {}

        status, rejected, response_headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={"code": "host-contract-only-code"},
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        assert status == 401, rejected
        assert not any(
            key.lower() == "set-cookie" for key, _value in response_headers
        )
        assert manager._active_codes == {}
        assert manager._active_sessions == {}

        try:
            server.issue_panel_login_code()
        except RuntimeError as error:
            assert "capture is unavailable" in str(error)
        else:  # pragma: no cover - explicit fail-closed assertion
            raise AssertionError("no-session server minted a login code")
    finally:
        server.stop()


def test_cross_server_no_dispatch_rejects_live_code_and_cookie_without_mutation() -> None:
    capture = _binding(activation_id="activation:cross-server", security_epoch=21)
    current = [capture]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    captured_server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(capture, current),
    )
    captured_server.start()
    origin = f"http://127.0.0.1:{captured_server.port}"
    try:
        status, pending, _ = _request(
            captured_server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, pending
        status, bootstrap, _ = _request(
            captured_server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, exchanged, response_headers = _request(
            captured_server,
            "POST",
            "/api/panel/auth/exchange",
            body={"code": bootstrap["data"]["code"]},
            headers={"Origin": origin},
        )
        assert status == 200, exchanged
        cookie = next(
            value
            for key, value in response_headers
            if key.lower() == "set-cookie"
        ).split(";", 1)[0]
    finally:
        captured_server.stop()

    codes_before = {
        key: dict(value) for key, value in manager._active_codes.items()
    }
    sessions_before = {
        key: dict(value) for key, value in manager._active_sessions.items()
    }
    lifecycle = _PackVMLifecycle()
    uncaptured_server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        packvm_lifecycle=lifecycle,  # type: ignore[arg-type]
    )
    uncaptured_server.start()
    try:
        status, rejected, response_headers = _request(
            uncaptured_server,
            "POST",
            "/api/panel/auth/exchange",
            body={"code": pending["data"]["code"]},
            headers={"Origin": f"http://127.0.0.1:{uncaptured_server.port}"},
        )
        assert status == 401, rejected
        assert not any(
            key.lower() == "set-cookie" for key, _value in response_headers
        )

        status, rejected, _ = _request(
            uncaptured_server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": cookie},
        )
        assert status == 401, rejected
        assert lifecycle.doctor_calls == 0
        assert manager._active_codes == codes_before
        assert manager._active_sessions == sessions_before
    finally:
        uncaptured_server.stop()


def test_capture_reauthorization_preserves_journal_owner_and_rotates_credentials() -> None:
    """A new desktop code plus the old cookie retains only journal ownership."""
    from dataclasses import replace

    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    first = _binding(activation_id="first", security_epoch=7)
    second = replace(first, activation_id="second", profile_revision="sha256:" + "3" * 64)
    original = manager.exchange_code(str(manager.issue_login_code(first)["code"]), first)
    assert original is not None
    cookie = str(original["session_id"])
    owner = manager.verify_session(cookie, first)
    assert owner is not None
    assert manager.verify_session(cookie, second) is None
    assert manager.exchange_code("invalid", second, previous_session=cookie) is None
    assert manager.verify_session(cookie, first) == owner

    code = str(manager.issue_login_code(second)["code"])
    renewed = manager.exchange_code(code, second, previous_session=cookie)
    assert renewed is not None
    assert renewed["session_id"] != cookie
    assert renewed["csrf_token"] != original["csrf_token"]
    assert manager.verify_session(cookie, first) is None
    assert manager.verify_session(str(renewed["session_id"]), first) is None
    current = manager.verify_session(str(renewed["session_id"]), second)
    assert current is not None
    assert current["session_id"] == owner["session_id"]
    assert manager.exchange_code(code, second, previous_session=cookie) is None


def test_reauthorization_does_not_inherit_unrelated_or_expired_session() -> None:
    """Epoch changes, Profile changes and invalid cookies sever ownership."""
    from dataclasses import replace

    first = _binding(activation_id="first", security_epoch=7)
    for case in ("epoch", "profile", "expired", "revoked", "unknown"):
        manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
        original = manager.exchange_code(str(manager.issue_login_code(first)["code"]), first)
        assert original is not None
        cookie = str(original["session_id"])
        owner = manager.verify_session(cookie, first)
        assert owner is not None
        second = replace(first, activation_id="second")
        if case == "epoch":
            second = replace(second, security_epoch=8)
        elif case == "profile":
            second = replace(second, profile_id="other")
        elif case == "expired":
            manager._active_sessions[manager._hash_value(cookie)]["expires_at"] = 0
        elif case == "revoked":
            manager.revoke_session(cookie)
        else:
            cookie = "unrecognized-cookie"
        renewed = manager.exchange_code(
            str(manager.issue_login_code(second)["code"]), second, previous_session=cookie,
        )
        assert renewed is not None
        current = manager.verify_session(str(renewed["session_id"]), second)
        assert current is not None
        assert current["session_id"] != owner["session_id"], case


def test_http_exchange_carries_cookie_ownership_across_capture_refresh() -> None:
    """The real HTTP exchange passes the HttpOnly cookie to reauthorization."""
    first = _binding(activation_id="first", security_epoch=7)
    second = _binding(activation_id="second", security_epoch=7)
    current = [first]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    original = manager.exchange_code(str(manager.issue_login_code(first)["code"]), first)
    assert original is not None
    owner = manager.verify_session(str(original["session_id"]), first)
    server = PackAPIServer(
        port=0, panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(first, current),
    )
    server.start()
    try:
        current[0] = second
        _publish_capture(server, _CapturedDispatch(second, current))
        status, response, headers = _request(
            server, "POST", "/api/panel/auth/exchange",
            body={"code": manager.issue_login_code(second)["code"]},
            headers={
                "Origin": f"http://127.0.0.1:{server.port}",
                "Cookie": f"rumi_panel_session={original['session_id']}",
            },
        )
        assert status == 200, response
        cookie = next(value for key, value in headers if key.lower() == "set-cookie")
        renewed = manager.verify_session(cookie.split(";", 1)[0].split("=", 1)[1], second)
        assert renewed is not None and owner is not None
        assert renewed["session_id"] == owner["session_id"]
    finally:
        server.stop()
