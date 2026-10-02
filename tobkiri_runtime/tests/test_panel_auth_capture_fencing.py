"""Capture and Authority fencing for launcher-issued panel credentials."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

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


def test_panel_journal_scope_is_stable_per_launcher_bootstrap_secret() -> None:
    binding = _binding(activation_id="activation:scope", security_epoch=3)
    first = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    second = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    other = PanelAuthManager(bootstrap_secret="other-bootstrap")

    first_exchange = first.exchange_code(
        str(first.issue_login_code(binding)["code"]), binding
    )
    second_exchange = second.exchange_code(
        str(second.issue_login_code(binding)["code"]), binding
    )
    other_exchange = other.exchange_code(
        str(other.issue_login_code(binding)["code"]), binding
    )
    assert first_exchange is not None
    assert second_exchange is not None
    assert other_exchange is not None
    scope = first_exchange["journal_scope"]
    assert scope == second_exchange["journal_scope"]
    assert scope != other_exchange["journal_scope"]
    assert isinstance(scope, str)
    assert scope.startswith("sha256:") and len(scope) == 71
    assert "desktop-bootstrap" not in scope


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


def test_approval_presenter_grant_binds_window_session_to_owner_journal() -> None:
    """The verified-owner grant lets the approval window join the owner."""

    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    owner_journal = str(owner_session["session_id"])

    manager.record_approval_presenter_grant("req-presenter", owner_journal, binding)
    window = manager.exchange_code(
        str(
            manager.issue_login_code(
                binding, presenter_request_id="req-presenter"
            )["code"]
        ),
        binding,
        presenter_request_id="req-presenter",
    )
    assert window is not None
    assert window["session_id"] != owner["session_id"]
    resolved = manager.verify_session(str(window["session_id"]), binding)
    assert resolved is not None
    assert resolved["session_id"] == owner_journal
    assert resolved["csrf_token"] != owner["csrf_token"]
    assert resolved["request_scope"] == "req-presenter"
    # The granted exchange must not consume the owner's own session.
    assert owner_session is not None
    assert owner_session["request_scope"] == ""
    assert manager.verify_session(str(owner["session_id"]), binding) is not None


def test_approval_presenter_grant_is_bound_to_one_dedicated_code() -> None:
    """Only the code minted for the grant can claim it, exactly once."""

    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    owner_journal = str(owner_session["session_id"])

    manager.record_approval_presenter_grant("req-presenter", owner_journal, binding)

    # A different normal bootstrap code cannot pull the grant by naming the
    # request id: it simply mints an ordinary independent session.
    foreign = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]),
        binding,
        presenter_request_id="req-presenter",
    )
    assert foreign is not None
    foreign_session = manager.verify_session(str(foreign["session_id"]), binding)
    assert foreign_session is not None
    assert foreign_session["session_id"] != owner_journal
    assert foreign_session["request_scope"] == ""
    assert manager._presenter_grants["req-presenter"]["code_hash"] is None

    # The dedicated code minted for the grant consumes it on exchange.
    dedicated = str(
        manager.issue_login_code(binding, presenter_request_id="req-presenter")[
            "code"
        ]
    )
    assert manager._presenter_grants["req-presenter"]["code_hash"] == (
        manager._hash_value(dedicated)
    )
    window = manager.exchange_code(
        dedicated, binding, presenter_request_id="req-presenter"
    )
    assert window is not None
    resolved = manager.verify_session(str(window["session_id"]), binding)
    assert resolved is not None
    assert resolved["session_id"] == owner_journal
    assert resolved["request_scope"] == "req-presenter"
    assert "req-presenter" not in manager._presenter_grants

    # A second presenter-named code cannot replay the consumed grant: it is
    # issued as an ordinary code and its exchange keeps a separate journal.
    replay_code = str(
        manager.issue_login_code(binding, presenter_request_id="req-presenter")[
            "code"
        ]
    )
    replay = manager.exchange_code(
        replay_code, binding, presenter_request_id="req-presenter"
    )
    assert replay is not None
    replayed = manager.verify_session(str(replay["session_id"]), binding)
    assert replayed is not None
    assert replayed["session_id"] != owner_journal
    assert replayed["request_scope"] == ""


def test_approval_presenter_dedicated_code_fails_closed_on_mismatch() -> None:
    """A dedicated code denies rather than degrade to an unscoped session."""

    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    owner_journal = str(owner_session["session_id"])

    manager.record_approval_presenter_grant("req-presenter", owner_journal, binding)
    dedicated = str(
        manager.issue_login_code(binding, presenter_request_id="req-presenter")[
            "code"
        ]
    )
    # The dedicated code must name its exact request at exchange.
    assert manager.exchange_code(dedicated, binding) is None
    assert (
        manager.exchange_code(
            dedicated, binding, presenter_request_id="req-other"
        )
        is None
    )
    # A failed presenter exchange does not burn the dedicated code.
    window = manager.exchange_code(
        dedicated, binding, presenter_request_id="req-presenter"
    )
    assert window is not None
    resolved = manager.verify_session(str(window["session_id"]), binding)
    assert resolved is not None
    assert resolved["session_id"] == owner_journal
    assert resolved["request_scope"] == "req-presenter"


def test_approval_presenter_grant_stays_scoped_and_bounded() -> None:
    """Absent, unrelated, and expired grants never inherit the owner journal."""

    for case in ("absent", "other-request", "expired"):
        manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
        binding = _binding(activation_id="activation:presenter", security_epoch=7)
        owner = manager.exchange_code(
            str(manager.issue_login_code(binding)["code"]), binding
        )
        assert owner is not None
        owner_session = manager.verify_session(str(owner["session_id"]), binding)
        assert owner_session is not None
        owner_journal = str(owner_session["session_id"])

        if case == "other-request":
            manager.record_approval_presenter_grant(
                "req-other", owner_journal, binding
            )
        elif case == "expired":
            manager.record_approval_presenter_grant(
                "req-target", owner_journal, binding
            )
            manager._presenter_grants["req-target"]["expires_at"] = 0
        exchanged = manager.exchange_code(
            str(
                manager.issue_login_code(
                    binding, presenter_request_id="req-target"
                )["code"]
            ),
            binding,
            presenter_request_id="req-target",
        )
        # In every case the bootstrap-time grant bind fails closed: an absent
        # grant, a grant for another request, or an expired grant leaves the
        # code unmarked and the exchange mints an ordinary session.
        assert exchanged is not None, case
        resolved = manager.verify_session(str(exchanged["session_id"]), binding)
        assert resolved is not None
        assert resolved["session_id"] != owner_journal, case
        assert resolved["request_scope"] == "", case


def test_approval_presenter_grant_rejects_malformed_bindings() -> None:
    """Grant recording accepts only request-id-shaped keys and raw journals."""

    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    for request_id in ("", "has space", "has/slash", "x" * 161):
        with pytest.raises(ValueError, match="presenter grant"):
            manager.record_approval_presenter_grant(request_id, "journal", binding)
    for journal in ("", "owner.session", "x" * 513):
        with pytest.raises(ValueError, match="presenter grant"):
            manager.record_approval_presenter_grant("req-ok", journal, binding)
    assert manager._presenter_grants == {}


def test_http_exchange_passes_the_presenter_request_id() -> None:
    """The real HTTP bootstrap dedicates the code the window exchange uses."""

    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    current = [binding]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(binding, current),
    )
    server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"
        manager.record_approval_presenter_grant(
            "req-presenter", str(owner_session["session_id"]), binding
        )
        # The Launcher's approval-window bootstrap names the request so the
        # issued code is dedicated to that grant.
        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={"request_id": "req-presenter"},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, response, headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={
                "code": bootstrap["data"]["code"],
                "request_id": "req-presenter",
            },
            headers={"Origin": origin},
        )
        assert status == 200, response
        cookie = next(
            value for key, value in headers if key.lower() == "set-cookie"
        )
        window = manager.verify_session(
            cookie.split(";", 1)[0].split("=", 1)[1], binding
        )
        assert window is not None
        assert window["session_id"] == owner_session["session_id"]
        assert window["request_scope"] == "req-presenter"
        # The grant was consumed by the dedicated code's exchange.
        assert manager._presenter_grants == {}

        # A bootstrap exchange that only names the request id at exchange —
        # without the dedicated code — mints an ordinary session instead.
        status, foreign_bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, foreign_bootstrap
        status, foreign_response, foreign_headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={
                "code": foreign_bootstrap["data"]["code"],
                "request_id": "req-presenter",
            },
            headers={"Origin": origin},
        )
        assert status == 200, foreign_response
        foreign_cookie = next(
            value for key, value in foreign_headers if key.lower() == "set-cookie"
        )
        foreign = manager.verify_session(
            foreign_cookie.split(";", 1)[0].split("=", 1)[1], binding
        )
        assert foreign is not None
        assert foreign["session_id"] != owner_session["session_id"]
        assert foreign["request_scope"] == ""
    finally:
        server.stop()


def test_approval_navigation_retargets_reused_scoped_webview(
    tmp_path: Path,
) -> None:
    """A code-bearing B navigation replaces a valid A-scoped webview session."""

    web_root = tmp_path / "approval-ui"
    web_root.mkdir()
    (web_root / "shell.html").write_text(
        "approval application", encoding="utf-8"
    )
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    current = [binding]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    owner_journal = str(owner_session["session_id"])
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(binding, current),
        web_mounts=(
            {
                "path_prefix": "/approval",
                "web_root": web_root,
                "spa_fallback": True,
                "index_file": "shell.html",
                "auth_required": True,
                "auth_bootstrap": True,
            },
        ),
    )
    server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"

        def dedicated_code(request_id: str) -> str:
            manager.record_approval_presenter_grant(
                request_id, owner_journal, binding
            )
            status, bootstrap, _ = _request(
                server,
                "POST",
                "/api/panel/auth/bootstrap",
                body={"request_id": request_id},
                headers={
                    "X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"
                },
            )
            assert status == 200, bootstrap
            return str(bootstrap["data"]["code"])

        def exchange(code: str, request_id: str, cookie: str = "") -> str:
            headers = {"Origin": origin}
            if cookie:
                headers["Cookie"] = cookie
            status, response, response_headers = _request(
                server,
                "POST",
                "/api/panel/auth/exchange",
                body={"code": code, "request_id": request_id},
                headers=headers,
            )
            assert status == 200, response
            return next(
                value
                for key, value in response_headers
                if key.lower() == "set-cookie"
            ).split(";", 1)[0]

        cookie_a = exchange(dedicated_code("req-a"), "req-a")
        code_b = dedicated_code("req-b")

        connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=5
        )
        connection.request(
            "GET",
            f"/approval?request_id=req-b&code={code_b}",
            headers={"Cookie": cookie_a},
        )
        response = connection.getresponse()
        document = response.read().decode("utf-8")
        connection.close()
        assert response.status == 200
        assert "/api/panel/auth/exchange" in document
        assert 'location.replace("/approval?request_id=req-b")' in document
        assert code_b not in document

        cookie_b = exchange(code_b, "req-b", cookie_a)
        session_b = manager.verify_session(cookie_b.split("=", 1)[1], binding)
        assert session_b is not None
        assert session_b["session_id"] == owner_journal
        assert session_b["request_scope"] == "req-b"
        assert session_b["request_scope"] != "req-a"
        assert manager._presenter_grants == {}
    finally:
        server.stop()


def test_presenter_exchange_mints_dedicated_approval_cookie(
    tmp_path: Path,
) -> None:
    """A confined presenter session lives under its own cookie name.

    The approval window and the main window share an origin; only distinct
    cookie names keep one surface's session from overwriting the other's in
    a shared store.
    """

    web_root = tmp_path / "approval-ui"
    web_root.mkdir()
    (web_root / "shell.html").write_text(
        "approval application", encoding="utf-8"
    )
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    current = [binding]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    owner_journal = str(owner_session["session_id"])
    lifecycle = _PackVMLifecycle()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(binding, current),
        packvm_lifecycle=lifecycle,  # type: ignore[arg-type]
        web_mounts=(
            {
                "path_prefix": "/approval",
                "web_root": web_root,
                "spa_fallback": True,
                "index_file": "shell.html",
                "auth_required": True,
                "auth_bootstrap": True,
            },
        ),
    )
    server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"
        manager.record_approval_presenter_grant(
            "req-presenter", owner_journal, binding
        )
        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={"request_id": "req-presenter"},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, response, headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={
                "code": bootstrap["data"]["code"],
                "request_id": "req-presenter",
            },
            headers={"Origin": origin},
        )
        assert status == 200, response
        approval_cookie = next(
            value for key, value in headers if key.lower() == "set-cookie"
        )
        assert approval_cookie.startswith("rumi_approval_session=")
        assert "Path=/" in approval_cookie
        assert "HttpOnly" in approval_cookie
        assert "SameSite=Strict" in approval_cookie
        assert "request_scope" not in json.dumps(response.get("data") or {})

        # An ordinary exchange still mints the panel cookie, even when a
        # foreign request id rides along on an unmarked code.
        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, response, headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={
                "code": bootstrap["data"]["code"],
                "request_id": "req-presenter",
            },
            headers={"Origin": origin},
        )
        assert status == 200, response
        panel_cookie = next(
            value for key, value in headers if key.lower() == "set-cookie"
        )
        assert panel_cookie.startswith("rumi_panel_session=")
        assert "Path=/" in panel_cookie
    finally:
        server.stop()


def test_approval_cookie_authenticates_without_fencing_the_panel(
    tmp_path: Path,
) -> None:
    """Coexisting cookies resolve per surface and never clobber each other."""

    web_root = tmp_path / "approval-ui"
    web_root.mkdir()
    (web_root / "shell.html").write_text(
        "approval application", encoding="utf-8"
    )
    binding = _binding(activation_id="activation:presenter", security_epoch=7)
    current = [binding]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    lifecycle = _PackVMLifecycle()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(binding, current),
        packvm_lifecycle=lifecycle,  # type: ignore[arg-type]
        web_mounts=(
            {
                "path_prefix": "/approval",
                "web_root": web_root,
                "spa_fallback": True,
                "index_file": "shell.html",
                "auth_required": True,
                "auth_bootstrap": True,
            },
        ),
    )
    server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"

        def bootstrap_code(request_id: str = "") -> str:
            body = {"request_id": request_id} if request_id else {}
            status, bootstrap, _ = _request(
                server,
                "POST",
                "/api/panel/auth/bootstrap",
                body=body,
                headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
            )
            assert status == 200, bootstrap
            return str(bootstrap["data"]["code"])

        def exchange(code: str, request_id: str = "") -> str:
            body = (
                {"code": code, "request_id": request_id}
                if request_id
                else {"code": code}
            )
            status, response, headers = _request(
                server,
                "POST",
                "/api/panel/auth/exchange",
                body=body,
                headers={"Origin": origin},
            )
            assert status == 200, response
            return next(
                value
                for key, value in headers
                if key.lower() == "set-cookie"
            ).split(";", 1)[0]

        # The main window's session.
        panel_cookie = exchange(bootstrap_code())

        # The approval window's dedicated exchange must not overwrite the
        # panel session above — it mints under `rumi_approval_session`.
        owner_session = manager.verify_session(
            panel_cookie.split("=", 1)[1], binding
        )
        assert owner_session is not None
        manager.record_approval_presenter_grant(
            "req-presenter", str(owner_session["session_id"]), binding
        )
        approval_cookie = exchange(
            bootstrap_code("req-presenter"), "req-presenter"
        )
        assert approval_cookie.startswith("rumi_approval_session=")

        # The approval cookie alone authenticates the approval surface.
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=5
        )
        connection.request(
            "GET", "/approval", headers={"Cookie": approval_cookie}
        )
        mount_response = connection.getresponse()
        assert mount_response.read() == b"approval application"
        connection.close()
        assert mount_response.status == 200

        # The confined session stays fenced away from ordinary panel routes,
        # and the fenced refresh still reissues under its own name rather
        # than clobbering the panel cookie.
        status, denied, headers = _request(
            server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": approval_cookie},
        )
        assert status == 401, denied
        assert lifecycle.doctor_calls == 0
        refreshed = next(
            value for key, value in headers if key.lower() == "set-cookie"
        )
        assert refreshed.startswith("rumi_approval_session=")

        # With both cookies in one jar the panel session still wins, so a
        # presenter cookie can never fence the main window's traffic.
        status, healthy, headers = _request(
            server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": f"{panel_cookie}; {approval_cookie}"},
        )
        assert status == 200, healthy
        assert lifecycle.doctor_calls == 1
        refreshed = next(
            value for key, value in headers if key.lower() == "set-cookie"
        )
        assert refreshed.startswith("rumi_panel_session=")
    finally:
        server.stop()


def test_approval_session_reexchange_keeps_its_confinement(
    tmp_path: Path,
) -> None:
    """A confined session's re-exchange stays under the approval cookie.

    ``previous_session`` resolves the caller's own surface cookie — not just
    ``rumi_panel_session`` — so an approval-window re-auth carries its
    ``request_scope`` confinement forward instead of minting an unconfined
    session under the panel name.
    """

    web_root = tmp_path / "approval-ui"
    web_root.mkdir()
    (web_root / "shell.html").write_text(
        "approval application", encoding="utf-8"
    )
    binding = _binding(activation_id="activation:reauth", security_epoch=7)
    current = [binding]
    manager = PanelAuthManager(bootstrap_secret="desktop-bootstrap")
    owner = manager.exchange_code(
        str(manager.issue_login_code(binding)["code"]), binding
    )
    assert owner is not None
    owner_session = manager.verify_session(str(owner["session_id"]), binding)
    assert owner_session is not None
    lifecycle = _PackVMLifecycle()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=_CapturedDispatch(binding, current),
        packvm_lifecycle=lifecycle,  # type: ignore[arg-type]
        web_mounts=(
            {
                "path_prefix": "/approval",
                "web_root": web_root,
                "spa_fallback": True,
                "index_file": "shell.html",
                "auth_required": True,
                "auth_bootstrap": True,
            },
        ),
    )
    server.start()
    try:
        origin = f"http://127.0.0.1:{server.port}"
        manager.record_approval_presenter_grant(
            "req-presenter", str(owner_session["session_id"]), binding
        )
        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={"request_id": "req-presenter"},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, response, headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={
                "code": bootstrap["data"]["code"],
                "request_id": "req-presenter",
            },
            headers={"Origin": origin},
        )
        assert status == 200, response
        approval_cookie = next(
            value for key, value in headers if key.lower() == "set-cookie"
        ).split(";", 1)[0]
        assert approval_cookie.startswith("rumi_approval_session=")

        # Re-auth: a fresh unmarked code exchanged by the isolated approval
        # window — whose jar carries only the approval cookie — must carry
        # the confinement forward and keep minting under its own name.
        status, bootstrap, _ = _request(
            server,
            "POST",
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
        )
        assert status == 200, bootstrap
        status, response, headers = _request(
            server,
            "POST",
            "/api/panel/auth/exchange",
            body={"code": bootstrap["data"]["code"]},
            headers={"Origin": origin, "Cookie": approval_cookie},
        )
        assert status == 200, response
        refreshed = next(
            value for key, value in headers if key.lower() == "set-cookie"
        )
        assert refreshed.startswith("rumi_approval_session=")
        assert "Path=/" in refreshed
        new_session_id = refreshed.split(";", 1)[0].split("=", 1)[1]
        session = manager.verify_session(new_session_id, binding)
        assert session is not None
        assert session["request_scope"] == "req-presenter"

        # The carried-over confinement still fences ordinary panel routes.
        status, denied, _ = _request(
            server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": refreshed.split(";", 1)[0]},
        )
        assert status == 401, denied
        assert lifecycle.doctor_calls == 0
    finally:
        server.stop()
