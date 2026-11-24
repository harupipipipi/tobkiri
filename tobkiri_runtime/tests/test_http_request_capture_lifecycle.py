"""Real socket regressions for capture publication between HTTP requests."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest

from core_runtime.host_contract import capture_host_contract
from core_runtime.pack_api_server import (
    PackAPIHandler,
    PackAPIServer,
    RuntimeCaptureInputs,
)
from core_runtime.panel_auth import PanelAuthManager
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractTarget,
)
from tests.conformance_support.host_contract import host_contract_for_session
from tests.test_pack_api_server import _Dispatch, _panel_session, _request
from tobkiri_host.runtime import V4DispatchSession

PATH = "/api/contracts/test/" + quote("GET /api/test/snapshot", safe="")


class _Capture(_Dispatch):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self._closing = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.effects = 0
        self.close_calls = 0

    def assert_current(self) -> None:
        V4DispatchSession._assert_open(self)

    def assert_operation_ready(self, *args: object) -> None:
        self.assert_current()

    def provider_metadata(self, contract: str) -> tuple[dict[str, object], ...]:
        return (
            {
                "provider_id": "test.snapshot",
                "operation_id": self.name,
                "profile_id": self.profile_id,
                "profile_revision": self.profile_revision,
                "activation_id": self.activation_id,
                "plan_digest": self.plan_digest,
            },
        )

    def close(self) -> None:
        self._closing.set()
        self.close_calls += 1

    def invoke(self, contract: str, operation: str, payload: dict) -> dict:
        self.assert_current()
        self.entered.set()
        if self.block:
            assert self.release.wait(5)
        self.assert_current()
        assert operation == self.name
        self.effects += 1
        self.calls.append((contract, operation, payload))
        return {
            "capture": self.name,
            "contract_snapshot": capture_host_contract()["values"]["snapshot"],
        }


def _binding(capture: _Capture) -> HTTPContractBinding:
    return HTTPContractBinding(
        method="GET",
        path="/api/test/snapshot",
        presentation="identity",
        application_id="test.application",
        route_namespace="test",
        targets=(
            HTTPContractTarget(
                contribution_id="test.snapshot",
                contract_id="test.snapshot.v1",
                operation_id=capture.name,
                provider_id="test.snapshot",
                function_id="test.snapshot",
            ),
        ),
    )


@pytest.fixture
def boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use real HTTP/session parsing and an isolated capture/dispatch leaf."""
    from core_runtime.bootstrap import profile_capture, production_v4
    from core_runtime import di_container, pack_api_server
    from tobkiri_host import runtime

    initial, current = _Capture("initial"), _Capture("current")
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(tmp_path / "contract.json"))
    monkeypatch.setattr(profile_capture, "runtime_user_data_root", lambda: tmp_path)
    monkeypatch.setattr(di_container, "get_container", object)
    monkeypatch.setattr(runtime, "install_dispatch_session", lambda *_args: None)
    monkeypatch.setattr(
        pack_api_server,
        "capture_host_contract_from_file",
        lambda **_kwargs: host_contract_for_session(
            current, values={"snapshot": current.name}
        ),
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-desktop"),
        dispatch_session=initial,
        contract_bindings=(_binding(initial),),
        host_contract=host_contract_for_session(
            initial, values={"snapshot": initial.name}
        ),
        runtime_capture_factory=lambda *_args: RuntimeCaptureInputs(
            bundle_root=tmp_path,
            ecosystem_root=tmp_path,
            contract_bindings=(_binding(current),),
        ),
    )
    monkeypatch.setattr(profile_capture, "capture_active_profile", object)
    monkeypatch.setattr(
        production_v4, "capture_production_dispatch", lambda *_args, **_kwargs: current
    )
    server.start()
    try:
        yield server, initial, current
    finally:
        initial.release.set()
        server.stop()


def _preconnect(
    server: PackAPIServer, monkeypatch: pytest.MonkeyPatch
) -> socket.socket:
    """Wait for construction before sending any request bytes."""
    constructed = threading.Event()
    old = server.handler_class
    original_setup = old.setup

    def setup(handler: PackAPIHandler) -> None:
        original_setup(handler)
        constructed.set()

    monkeypatch.setattr(old, "setup", setup)
    connection = socket.create_connection((server.host, server.port), timeout=5)
    assert constructed.wait(5)
    return connection


def _socket_request(connection: socket.socket, cookie: str) -> tuple[int, dict]:
    connection.sendall(
        (
            f"GET {PATH} HTTP/1.0\r\nHost: localhost\r\nCookie: {cookie}\r\n"
            f"X-Tobkiri-Request-ID: {uuid.uuid4()}\r\n\r\n"
        ).encode()
    )
    response = http.client.HTTPResponse(connection)
    response.begin()
    return response.status, json.loads(response.read())


def _refresh(server: PackAPIServer, current: _Capture | None = None) -> None:
    server._refresh_runtime_capture(
        current, lifecycle_generation=server._lifecycle_generation
    )


@pytest.mark.parametrize("refresh_kind", ["explicit", "native_admission"])
def test_constructed_idle_socket_uses_current_capture_after_refresh(
    boundary,
    monkeypatch: pytest.MonkeyPatch,
    refresh_kind: str,
) -> None:
    server, initial, current = boundary
    cookie, _, _ = _panel_session(server)
    connection = _preconnect(server, monkeypatch)
    try:
        if refresh_kind == "native_admission":
            from core_runtime import native_pack_onboarding

            monkeypatch.setattr(
                native_pack_onboarding,
                "commit_signed_pack",
                lambda *_args, **_kwargs: {
                    "pack_id": "test.normal",
                    "artifact_digest": "sha256:" + "a" * 64,
                    "publisher_id": "test.publisher",
                },
            )
            monkeypatch.setattr(
                server.handler_class,
                "_native_pack_trust_store",
                lambda *_args: Path("/isolated/policy"),
            )
            status, payload, _ = _request(
                server,
                "POST",
                "/api/internal/native-pack/commit",
                body={
                    "source_root": "/isolated/normal-pack",
                    "public_key_path": "/isolated/public.pem",
                    "preview_digest": "sha256:" + "b" * 64,
                },
                headers={"X-Rumi-Desktop-Bootstrap": "verified-desktop"},
            )
            assert status == 200, payload
            assert payload["data"]["catalog_refreshed"]
        else:
            _refresh(server, current)
        assert initial._closing.is_set()
        status, payload = _socket_request(connection, cookie)
        assert status == 200, payload
        assert payload["data"] == {"capture": "current", "contract_snapshot": "current"}
        assert initial.effects == 0
        assert current.effects == 1
    finally:
        connection.close()


def test_inflight_authenticated_request_retains_old_capture_and_is_fenced(
    boundary,
) -> None:
    server, initial, current = boundary
    cookie, _, _ = _panel_session(server)
    initial.block = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            _request,
            server,
            "GET",
            PATH,
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert initial.entered.wait(5)
        _refresh(server, current)
        initial.release.set()
        status, payload, _ = request.result(5)
    assert status == 503, payload
    assert initial.effects == current.effects == 0
    assert initial.close_calls == 1
    status, payload, _ = _request(
        server,
        "GET",
        PATH,
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, payload
    assert current.effects == 1


def test_new_identity_does_not_accept_old_authenticated_cookie(
    boundary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, initial, current = boundary
    cookie, _, _ = _panel_session(server)
    connection = _preconnect(server, monkeypatch)
    current.activation_id = "activation:successor"
    try:
        _refresh(server, current)
        status, payload = _socket_request(connection, cookie)
        assert status == 401, payload
        assert initial.effects == current.effects == 0
        fresh_cookie, _, _ = _panel_session(server)
        status, payload, _ = _request(
            server,
            "GET",
            PATH,
            headers={"Cookie": fresh_cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 200, payload
    finally:
        connection.close()


def test_no_refresh_baseline_uses_original_capture(boundary) -> None:
    server, initial, current = boundary
    cookie, _, _ = _panel_session(server)
    status, payload, _ = _request(
        server,
        "GET",
        PATH,
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    assert payload["data"] == {"capture": "initial", "contract_snapshot": "initial"}
    assert initial.effects == 1 and current.effects == 0


@pytest.mark.parametrize("failure", ["stopping", "foreign", "old_generation"])
def test_snapshot_failure_has_no_old_capture_fallback(
    boundary,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    server, initial, current = boundary
    cookie, _, _ = _panel_session(server)
    connection = _preconnect(server, monkeypatch)
    try:
        with server._lifecycle_lock:
            if failure == "stopping":
                server._lifecycle_state = "stopping"
            elif failure == "old_generation":
                server._lifecycle_generation += 1
            else:
                foreign = PackAPIHandler.canonical_v4_server_handler(
                    panel_auth_manager=server._panel_auth_manager,
                    dispatch_session=current,
                    app_lifecycle_manager=None,
                    request_snapshot_owner=object(),
                    request_snapshot_generation=server._lifecycle_generation,
                )
                server.handler_class = server.server.RequestHandlerClass = foreign
                server._dispatch_session = current
        status, payload = _socket_request(connection, cookie)
        assert status == 503, payload
        assert initial.effects == current.effects == 0
    finally:
        connection.close()
        with server._lifecycle_lock:
            server._lifecycle_state = "running"
