"""Live and structural tests for the finite Pack v4 HTTP boundary."""

from __future__ import annotations

import http.client
import json
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.control_reconciliation_v4 import ControlReconciliationStore
from core_runtime.frontend_contract_routes import FrontendContractBinding
from core_runtime.pack_api_server import (
    MAX_CONCURRENT_REQUESTS,
    PackAPIHandler,
    PackAPIServer,
    RuntimeHTTPConfig,
)
from core_runtime.pack_control_v4 import (
    PackControlConflict,
    PackControlDigestMismatch,
    PackControlTimedOut,
    PackControlUnavailable,
    PackControlUnapproved,
)
from core_runtime.panel_auth import PanelAuthManager
from tobkiri_host.errors import ProviderExecutionError
from tobkiri_protocol.canonical import canonical_digest


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


class _Dispatch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str = ">=1,<2",
    ) -> Mapping[str, object]:
        self.calls.append((contract_id, operation_id, dict(payload)))
        return {"contract_id": contract_id, "operation_id": operation_id}


class _RefreshDispatch(_Dispatch):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.profile_id = "defaults"
        self.plan_digest = "sha256:" + "a" * 64
        self.close_calls = 0
        self.read_fences = 0

    def close(self) -> None:
        self.close_calls += 1

    def cancel_pending_reads(self) -> None:
        self.read_fences += 1


class _Lifecycle:
    def check_setup_status(self) -> dict[str, object]:
        return {"needs_setup": False, "setup_state": "complete"}

    def get_health(self) -> dict[str, object]:
        return {"status": "ok", "runtime_ready": True}


class _PackVMLifecycle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def prepare(self, *, session_id: str | None = None) -> Mapping[str, object]:
        self.calls.append(("prepare", {}))
        return {
            "instance": "tobkiri-packvm-v4",
            "image_source": "https://images.invalid/pinned.img",
            "image_digest": "sha256:" + "a" * 64,
            "image_size_bytes": 700_000_000,
            "plan_digest": "sha256:" + "b" * 64,
            "ceremony_nonce": "c" * 32,
            "confirmation": "PROVISION tobkiri-packvm-v4 bbbbbbbbbbbb",
        }

    def consent(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]:
        self.calls.append(("consent", dict(payload)))
        return {"consent_id": "packvm-consent.test"}

    def provision(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]:
        self.calls.append(("provision", dict(payload)))
        return {"operation_id": payload["operation_id"], "state": "queued"}

    def doctor(self) -> Mapping[str, object]:
        self.calls.append(("doctor", {}))
        return {"ready": True, "attestation_digest": "sha256:" + "d" * 64}

    def readiness_snapshot(self) -> Mapping[str, object]:
        self.calls.append(("readiness_snapshot", {}))
        return {"ready": False}

    def progress(self, operation_id: str, *, session_id: str | None = None) -> Mapping[str, object]:
        self.calls.append(("progress", {"operation_id": operation_id}))
        if operation_id == "22222222-2222-4222-8222-222222222222":
            return {
                "operation_id": operation_id,
                "operation_kind": "cleanup",
                "state": "succeeded",
            }
        return {"operation_id": operation_id, "state": "succeeded"}

    def cancel(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]:
        self.calls.append(("cancel", dict(payload)))
        return {"operation_id": payload["operation_id"], "state": "cancelled"}

    def stop(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append(("stop", dict(payload)))
        return {"ready": False}

    def cleanup(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]:
        self.calls.append(("cleanup", dict(payload)))
        return {
            "operation_id": payload["operation_id"],
            "operation_kind": "cleanup",
            "state": "queued",
        }


def test_profile_activation_refresh_requires_durable_success_result() -> None:
    handler = object.__new__(PackAPIHandler)
    refreshes: list[object] = []
    handler._runtime_refresh = refreshes.append

    handler._refresh_after_operation(
        "profile.change.activate",
        {"state": "error", "code": "UNAPPROVED"},
    )
    assert refreshes == []

    handler._refresh_after_operation(
        "profile.change.activate",
        {"state": "active", "activation_id": "activation.test"},
    )
    assert refreshes == [None]


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("UNAPPROVED", 403),
        ("STALE_REVISION", 409),
        ("DIGEST_MISMATCH", 409),
        ("TIMEOUT", 504),
        ("API_FAILURE", 503),
        ("backend_unavailable", 503),
        ("pack_control_conflict", 409),
        ("pack_control_stale_revision", 409),
        ("pack_control_digest_mismatch", 409),
        ("pack_control_unapproved", 403),
        ("pack_control_unavailable", 503),
        ("pack_control_timeout", 504),
    ],
)
def test_runtime_surface_typed_errors_map_to_semantic_http_status(
    code: str,
    status: int,
) -> None:
    assert (
        PackAPIHandler._contract_result_status(
            {
                "state": "error",
                "code": code,
            }
        )
        == status
    )


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("UNAPPROVED", 403),
        ("STALE_REVISION", 409),
        ("DIGEST_MISMATCH", 409),
        ("TIMEOUT", 504),
        ("API_FAILURE", 503),
    ],
)
@pytest.mark.parametrize(
    "operation_id",
    [
        "profile.change.activate",
        "approval.revoke",
        "pack.enable",
    ],
)
def test_typed_error_initial_lost_response_and_restart_replay_are_exact(
    tmp_path: Path,
    code: str,
    expected_status: int,
    operation_id: str,
) -> None:
    contract_id = (
        "tobkiri.host.control-presentation.v4"
        if operation_id.startswith("profile.change.")
        else "tobkiri.host.pack-control.v4"
    )
    binding = FrontendContractBinding(
        method="POST",
        path=f"/test/{operation_id}",
        presentation="identity",
        targets=(),
    )
    request_id = "13131313-1313-4313-8313-131313131313"
    session_id = "session-a"
    handler = object.__new__(PackAPIHandler)
    captured: list[tuple[int, str]] = []
    handler._send_response = (  # type: ignore[method-assign]
        lambda response, status=200: captured.append((status, response.to_json()))
    )
    unsafe = {
        "state": "error",
        "code": code,
        "message": "sqlite /private/token.db DigestError token=secret",
        "digest": "sha256:" + "a" * 64,
    }
    safe = handler._safe_contract_result(unsafe)
    store_path = tmp_path / "reconciliation.sqlite3"
    first = ControlReconciliationStore(store_path, instance_id="first")
    first.begin_operation(
        request_id=request_id,
        session_id=session_id,
        operation_id=operation_id,
        contract_id=contract_id,
        request_digest=canonical_digest({"request": "exact"}),
    )
    first.finish_operation(
        request_id,
        session_id=session_id,
        state="failed",
        result=safe,
        safe_error_code=code,
    )
    handler._send_contract_outcome(binding, safe)
    initial = captured[-1]
    handler._send_contract_outcome(binding, safe)
    lost_response_retry = captured[-1]
    first.close()

    restarted = ControlReconciliationStore(store_path, instance_id="restarted")
    restarted.prepare_for_operation()
    replay = restarted.operation_status(request_id, session_id=session_id)
    handler._send_contract_outcome(binding, replay["result"])
    restart_replay = captured[-1]

    assert initial == lost_response_retry == restart_replay
    assert initial[0] == expected_status
    serialized = initial[1].lower()
    for secret in ("sqlite", "/private", "digesterror", "sha256:", "token"):
        assert secret not in serialized
    assert replay["result"] == safe


@pytest.mark.parametrize(
    ("error_type", "expected_code", "expected_status", "retryable"),
    [
        (PackControlConflict, "STALE_REVISION", 409, False),
        (PackControlDigestMismatch, "DIGEST_MISMATCH", 409, False),
        (PackControlUnapproved, "UNAPPROVED", 403, False),
        (PackControlUnavailable, "API_FAILURE", 503, True),
        (PackControlTimedOut, "TIMEOUT", 504, True),
    ],
)
def test_pack_control_exception_cause_chain_keeps_semantic_status_and_sanitizes(
    error_type: type[Exception],
    expected_code: str,
    expected_status: int,
    retryable: bool,
) -> None:
    """Typed outer failures win over unsafe implementation causes."""

    from core_runtime.pack_api_server import _exception_error_code

    cause = ValueError("sqlite /private/token.db sha256:secret")
    error = error_type("provider-controlled private detail")
    error.__cause__ = cause
    safe = PackAPIHandler._safe_contract_result(
        {"state": "error", "code": _exception_error_code(error), "message": str(error)}
    )

    assert safe["code"] == expected_code
    assert PackAPIHandler._contract_result_status(safe) == expected_status
    assert safe["retryable"] is retryable
    serialized = json.dumps(safe).lower()
    for secret in ("sqlite", "/private", "sha256:", "provider-controlled"):
        assert secret not in serialized


def test_provider_wrapper_preserves_inner_pack_control_conflict() -> None:
    """A Broker wrapper must not erase the typed Pack control cause."""

    from core_runtime.pack_api_server import _exception_error_code

    conflict = PackControlConflict("approval_revoked")
    wrapper = ProviderExecutionError("provider failed")
    wrapper.__cause__ = conflict

    assert _exception_error_code(wrapper) == "STALE_REVISION"


@pytest.fixture
def live_server() -> Iterator[tuple[PackAPIServer, _Dispatch]]:
    dispatch = _Dispatch()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-desktop"),
        dispatch_session=dispatch,
        app_lifecycle_manager=_Lifecycle(),
    )
    server.start()
    try:
        yield server, dispatch
    finally:
        server.stop()


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
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    response_headers = response.getheaders()
    connection.close()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    return response.status, payload, response_headers


def _panel_session(
    server: PackAPIServer,
) -> tuple[str, str, str]:
    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "verified-desktop"},
    )
    assert status == 200
    code = bootstrap["data"]["code"]
    status, exchange, headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": code},
        headers={"Origin": origin},
    )
    assert status == 200
    cookie = next(value for key, value in headers if key.lower() == "set-cookie")
    return cookie.split(";", 1)[0], exchange["data"]["csrf_token"], origin


def _assert_retired_generic_dispatch(
    status: int,
    payload: Mapping[str, object],
) -> None:
    """Assert the typed no-write retirement contract for generic dispatch."""

    assert status == 410
    assert payload["data"] == {
        "api_version": "io.tobkiri.pack-api.v4",
        "state": "legacy_api_retired",
        "retired_route": "/api/v4/dispatch",
        "write_set": [],
    }
    assert payload["error"] == ("Legacy API route is retired; use an exact Pack v4 operation")


def test_packvm_lifecycle_routes_require_auth_csrf_and_fresh_request_id() -> None:
    lifecycle = _PackVMLifecycle()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-desktop"),
        packvm_lifecycle=lifecycle,
    )
    refreshed: list[object] = []
    server._refresh_runtime_capture = lambda session=None: refreshed.append(session)  # type: ignore[method-assign]
    server.start()
    try:
        status, _payload, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/prepare",
            body={},
        )
        assert status == 401
        cookie, csrf, origin = _panel_session(server)
        authenticated = {
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
        }
        request_id = str(uuid.uuid4())
        status, prepared, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/prepare",
            body={},
            headers={**authenticated, "X-Tobkiri-Request-ID": request_id},
        )
        assert status == 200
        assert prepared["data"]["image_size_bytes"] == 700_000_000
        assert prepared["data"]["image_digest"] == "sha256:" + "a" * 64

        replay_status, _replay, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/prepare",
            body={},
            headers={**authenticated, "X-Tobkiri-Request-ID": request_id},
        )
        assert replay_status == 409
        assert [call[0] for call in lifecycle.calls].count("prepare") == 1

        consent_body = {
            "plan_digest": prepared["data"]["plan_digest"],
            "ceremony_nonce": prepared["data"]["ceremony_nonce"],
            "confirmation": prepared["data"]["confirmation"],
            "approve_image_download": True,
        }
        status, consent, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/consent",
            body=consent_body,
            headers={
                **authenticated,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200
        status, provisioned, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/provision",
            body={
                "consent_id": consent["data"]["consent_id"],
                "operation_id": "11111111-1111-4111-8111-111111111111",
            },
            headers={
                **authenticated,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200
        assert provisioned["data"]["state"] == "queued"
        assert refreshed == []

        status, progress, _headers = _request(
            server,
            "GET",
            "/api/v4/packvm/progress?operation_id=11111111-1111-4111-8111-111111111111",
            headers={"Cookie": cookie, "Origin": origin},
        )
        assert status == 200
        assert progress["data"]["state"] == "succeeded"

        status, cancelled, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/cancel",
            body={"operation_id": "11111111-1111-4111-8111-111111111111"},
            headers={
                **authenticated,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200
        assert cancelled["data"]["state"] == "cancelled"

        status, doctor, _headers = _request(
            server,
            "GET",
            "/api/v4/packvm/doctor",
            headers={"Cookie": cookie, "Origin": origin},
        )
        assert status == 200
        assert doctor["data"]["ready"] is True
        assert refreshed == [None]

        cleanup_id = "22222222-2222-4222-8222-222222222222"
        status, cleanup, _headers = _request(
            server,
            "POST",
            "/api/v4/packvm/cleanup",
            body={
                "confirmation": "DELETE tobkiri-packvm-v4",
                "operation_id": cleanup_id,
                "source_operation_id": None,
            },
            headers={**authenticated, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 200
        assert cleanup["data"] == {
            "operation_id": cleanup_id,
            "operation_kind": "cleanup",
            "state": "queued",
        }
        assert refreshed == [None]
        status, cleanup_progress, _headers = _request(
            server,
            "GET",
            f"/api/v4/packvm/progress?operation_id={cleanup_id}",
            headers={"Cookie": cookie, "Origin": origin},
        )
        assert status == 200
        assert cleanup_progress["data"]["state"] == "succeeded"
        assert refreshed == [None, None]
    finally:
        server.stop()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_runtime_http_config_canonicalizes_loopback(host: str) -> None:
    assert RuntimeHTTPConfig.verify(host, 8765).host == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "example.com"])
def test_runtime_http_config_rejects_non_loopback(host: str) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        RuntimeHTTPConfig.verify(host, 8765)


@pytest.mark.parametrize("port", [-1, 65536])
def test_runtime_http_config_rejects_invalid_port(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        RuntimeHTTPConfig.verify("127.0.0.1", port)


def test_bind_environment_has_no_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUMI_API_BIND_ADDRESS", "0.0.0.0")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    assert server.host == "127.0.0.1"


def test_server_construction_without_requests_is_filesystem_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "fresh-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))

    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )

    assert server.server is None
    assert not user_data.exists()


def test_server_stop_closes_drained_journal_heartbeat_and_restart_reads_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    request_id = "12121212-1212-4212-8212-121212121212"
    first = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    first.start()
    first._operation_journal.begin_operation(
        request_id=request_id,
        session_id="session-a",
        operation_id="profile.change.approve",
        contract_id="tobkiri.host.control-presentation.v4",
        request_digest=canonical_digest({"request_id": request_id}),
    )
    first._operation_journal.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    heartbeat = first._operation_journal._heartbeat_thread
    first.stop()

    assert heartbeat is not None
    assert not heartbeat.is_alive()
    restarted = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    assert (
        restarted._operation_journal.operation_status(
            request_id,
            session_id="session-a",
        )["state"]
        == "succeeded"
    )


def test_server_stop_reports_bounded_teardown_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed drain reports finite serving/request state before raising."""

    monkeypatch.setattr("core_runtime.pack_api_server.THREAD_JOIN_TIMEOUT_SECONDS", 0.01)
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    server.start()
    raw_server = server.server
    assert raw_server is not None
    monkeypatch.setattr(raw_server, "wait_for_request_drain", lambda _timeout: False)

    with pytest.raises(
        RuntimeError,
        match=r"serving_thread_alive.*True.*active_requests.*0",
    ):
        server.stop()

    assert "teardown incomplete" in caplog.text


def test_server_stop_and_restart_fence_pending_runtime_reads() -> None:
    class CancelableDispatch(_Dispatch):
        def __init__(self) -> None:
            super().__init__()
            self.read_fences = 0

        def cancel_pending_reads(self) -> None:
            self.read_fences += 1

    dispatch = CancelableDispatch()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=dispatch,
    )

    server.start()
    server.stop()
    server.start()
    server.stop()

    assert dispatch.read_fences == 2


def test_server_stop_drain_runs_outside_lifecycle_lock() -> None:
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    server.start()
    raw_server = server.server
    assert raw_server is not None
    original_drain = raw_server.wait_for_request_drain
    acquired: list[bool] = []

    def observe_lock(timeout: float) -> bool:
        lock_acquired = server._lifecycle_lock.acquire(blocking=False)
        acquired.append(lock_acquired)
        if lock_acquired:
            server._lifecycle_lock.release()
        return original_drain(timeout)

    raw_server.wait_for_request_drain = observe_lock  # type: ignore[method-assign]
    server.stop()

    assert acquired == [True]


def test_stopped_handler_generation_cannot_publish_runtime_capture() -> None:
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    server.start()
    handler = server.handler_class
    assert handler is not None
    refresh = handler._runtime_refresh
    assert refresh is not None

    server.stop()
    refresh(object())  # type: ignore[arg-type]

    assert server.handler_class is None
    assert server.server is None


def _prepare_refresh_race(
    server: PackAPIServer,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    import core_runtime.di_container as di_container_module
    import core_runtime.pack_api_server as pack_api_server_module
    import tobkiri_host.runtime as host_runtime_module

    monkeypatch.setattr(
        pack_api_server_module,
        "_load_production_capture_inputs",
        lambda: (Path("/runtime"), Path("/bundle"), object(), ()),
    )
    monkeypatch.setattr(di_container_module, "get_container", object)
    monkeypatch.setattr(
        host_runtime_module,
        "install_dispatch_session",
        lambda _container, _session: None,
    )
    with server._lifecycle_lock:
        server._lifecycle_state = "running"
        server._lifecycle_generation = 41
    return 41


def test_server_closes_server_captured_refresh_session_on_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import core_runtime.authority.v4 as authority_v4
    import core_runtime.bootstrap.production_v4 as production_v4
    import core_runtime.bootstrap.profile_capture as profile_capture

    initial = _RefreshDispatch("initial")
    captured = _RefreshDispatch("captured")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)
    monkeypatch.setattr(profile_capture, "capture_default_profile", lambda: object())
    monkeypatch.setattr(profile_capture, "runtime_user_data_root", lambda: tmp_path)
    monkeypatch.setattr(authority_v4, "AuthorityStore", lambda _path: object())
    monkeypatch.setattr(
        production_v4,
        "capture_production_dispatch",
        lambda *_args, **_kwargs: captured,
    )

    try:
        server._refresh_runtime_capture(None, lifecycle_generation=generation)
        assert server._dispatch_session is captured
        assert server._dispatch_session_owned_by_server is True
        assert initial.close_calls == 1
        assert captured.close_calls == 0
    finally:
        server.stop()

    assert captured.close_calls == 1
    assert server._dispatch_session is None
    assert server._dispatch_session_owned_by_server is False


def test_server_refresh_reuses_exact_packvm_lifecycle_for_backend_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A refresh must not silently construct an unavailable second provisioner."""

    import core_runtime.authority.v4 as authority_v4
    import core_runtime.bootstrap.production_v4 as production_v4
    import core_runtime.bootstrap.profile_capture as profile_capture

    class Lifecycle:
        def readiness_snapshot(self) -> dict[str, object]:
            return {"ready": True}

        def production_backend_registration(self) -> object:
            return object()

    lifecycle = Lifecycle()
    captured = _RefreshDispatch("captured")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=_RefreshDispatch("initial"),  # type: ignore[arg-type]
        packvm_lifecycle=lifecycle,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)
    monkeypatch.setattr(profile_capture, "capture_default_profile", lambda: object())
    monkeypatch.setattr(profile_capture, "runtime_user_data_root", lambda: tmp_path)
    monkeypatch.setattr(authority_v4, "AuthorityStore", lambda _path: object())
    seen: dict[str, object] = {}

    def capture(*_args: object, **kwargs: object) -> _RefreshDispatch:
        seen.update(kwargs)
        return captured

    monkeypatch.setattr(production_v4, "capture_production_dispatch", capture)

    try:
        server._refresh_runtime_capture(None, lifecycle_generation=generation)
    finally:
        server.stop()

    assert seen["packvm_provisioner"] is lifecycle
    readiness = seen["packvm_readiness_reader"]
    assert callable(readiness)
    assert readiness() == {"ready": True}


def test_older_same_generation_refresh_cannot_replace_newer_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _RefreshDispatch("initial")
    older = _RefreshDispatch("older")
    newer = _RefreshDispatch("newer")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)
    older_entered = threading.Event()
    release_older = threading.Event()

    def validate(session: object, _routes: object) -> None:
        if session is older:
            older_entered.set()
            assert release_older.wait(2.0)

    monkeypatch.setattr(server, "_validate_contract_capture", validate)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                server._refresh_runtime_capture,
                older,
                lifecycle_generation=generation,
            )
            assert older_entered.wait(2.0)
            server._refresh_runtime_capture(
                newer,
                lifecycle_generation=generation,
            )
            published_handler = server.handler_class
            published_routes = server._contract_routes
            release_older.set()
            pending.result(timeout=2.0)

        assert server._dispatch_session is newer
        assert server.handler_class is published_handler
        assert server._contract_routes is published_routes
        assert initial.close_calls == 1
        assert older.close_calls == 1
        assert newer.close_calls == 0
    finally:
        release_older.set()
        server.stop()


def test_only_latest_of_three_unordered_refreshes_can_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _RefreshDispatch("initial")
    older = _RefreshDispatch("older")
    middle = _RefreshDispatch("middle")
    latest = _RefreshDispatch("latest")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)
    entered = {candidate: threading.Event() for candidate in (older, middle)}
    release = {candidate: threading.Event() for candidate in (older, middle)}

    def validate(session: object, _routes: object) -> None:
        if session in entered:
            entered[session].set()
            assert release[session].wait(2.0)

    monkeypatch.setattr(server, "_validate_contract_capture", validate)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            oldest_pending = executor.submit(
                server._refresh_runtime_capture,
                older,
                lifecycle_generation=generation,
            )
            assert entered[older].wait(2.0)
            middle_pending = executor.submit(
                server._refresh_runtime_capture,
                middle,
                lifecycle_generation=generation,
            )
            assert entered[middle].wait(2.0)
            server._refresh_runtime_capture(
                latest,
                lifecycle_generation=generation,
            )
            published_handler = server.handler_class
            published_routes = server._contract_routes
            release[middle].set()
            release[older].set()
            middle_pending.result(timeout=2.0)
            oldest_pending.result(timeout=2.0)

        assert server._dispatch_session is latest
        assert server.handler_class is published_handler
        assert server._contract_routes is published_routes
        assert initial.close_calls == 1
        assert older.close_calls == 1
        assert middle.close_calls == 1
        assert latest.close_calls == 0
    finally:
        release[older].set()
        release[middle].set()
        server.stop()


def test_failed_latest_refresh_invalidates_older_pending_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _RefreshDispatch("initial")
    older = _RefreshDispatch("older")
    failed = _RefreshDispatch("failed")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)
    older_entered = threading.Event()
    release_older = threading.Event()

    def validate(session: object, _routes: object) -> None:
        if session is older:
            older_entered.set()
            assert release_older.wait(2.0)
        if session is failed:
            raise RuntimeError("capture failed")

    monkeypatch.setattr(server, "_validate_contract_capture", validate)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                server._refresh_runtime_capture,
                older,
                lifecycle_generation=generation,
            )
            assert older_entered.wait(2.0)
            with pytest.raises(RuntimeError, match="capture failed"):
                server._refresh_runtime_capture(
                    failed,
                    lifecycle_generation=generation,
                )
            release_older.set()
            pending.result(timeout=2.0)

        assert server._dispatch_session is initial
        assert initial.close_calls == 0
        assert older.close_calls == 1
        assert failed.close_calls == 1
    finally:
        release_older.set()
        server.stop()


def test_capture_input_failure_closes_unpublished_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core_runtime.pack_api_server as pack_api_server_module

    initial = _RefreshDispatch("initial")
    failed = _RefreshDispatch("failed")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)

    def fail_capture_inputs() -> object:
        raise RuntimeError("capture inputs failed")

    monkeypatch.setattr(
        pack_api_server_module,
        "_load_production_capture_inputs",
        fail_capture_inputs,
    )
    try:
        with pytest.raises(RuntimeError, match="capture inputs failed"):
            server._refresh_runtime_capture(
                failed,
                lifecycle_generation=generation,
            )

        assert server._dispatch_session is initial
        assert initial.close_calls == 0
        assert failed.close_calls == 1
    finally:
        server.stop()


def test_generation_change_immediately_before_publish_discards_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _RefreshDispatch("initial")
    candidate = _RefreshDispatch("candidate")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    generation = _prepare_refresh_race(server, monkeypatch)
    validation_complete = threading.Event()
    allow_publish = threading.Event()

    def validate(_session: object, _routes: object) -> None:
        validation_complete.set()
        assert allow_publish.wait(2.0)

    monkeypatch.setattr(server, "_validate_contract_capture", validate)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                server._refresh_runtime_capture,
                candidate,
                lifecycle_generation=generation,
            )
            assert validation_complete.wait(2.0)
            with server._lifecycle_lock:
                server._lifecycle_generation += 1
            allow_publish.set()
            pending.result(timeout=2.0)

        assert server._dispatch_session is initial
        assert initial.close_calls == 0
        assert candidate.close_calls == 1
    finally:
        allow_publish.set()
        server.stop()


def test_refresh_finishing_after_stop_restart_cannot_replace_new_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _RefreshDispatch("initial")
    stale = _RefreshDispatch("stale")
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        dispatch_session=initial,  # type: ignore[arg-type]
    )
    import core_runtime.pack_api_server as pack_api_server_module

    monkeypatch.setattr(
        pack_api_server_module,
        "_load_production_capture_inputs",
        lambda: (Path("/runtime"), Path("/bundle"), object(), ()),
    )
    stale_entered = threading.Event()
    release_stale = threading.Event()

    def validate(session: object, _routes: object) -> None:
        if session is stale:
            stale_entered.set()
            assert release_stale.wait(2.0)

    server.start()
    monkeypatch.setattr(server, "_validate_contract_capture", validate)
    generation = server._lifecycle_generation
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                server._refresh_runtime_capture,
                stale,
                lifecycle_generation=generation,
            )
            assert stale_entered.wait(2.0)
            server.stop()
            server.start()
            restarted_handler = server.handler_class
            restarted_routes = server._contract_routes
            release_stale.set()
            pending.result(timeout=2.0)

        assert server._dispatch_session is initial
        assert server.handler_class is restarted_handler
        assert server._contract_routes is restarted_routes
        assert stale.close_calls == 1
        assert initial.close_calls == 0
    finally:
        release_stale.set()
        server.stop()


def test_double_stop_and_restart_remain_bounded() -> None:
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )

    started = time.monotonic()
    server.start()
    server.stop()
    server.stop()
    server.start()
    server.stop()

    assert time.monotonic() - started < 5.0


def test_request_threads_are_bounded_and_overflow_gets_backpressure() -> None:
    entered = 0
    entered_lock = threading.Lock()
    release = threading.Event()

    class BlockingLifecycle(_Lifecycle):
        def get_health(self) -> dict[str, object]:
            nonlocal entered
            with entered_lock:
                entered += 1
            release.wait()
            return super().get_health()

    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        app_lifecycle_manager=BlockingLifecycle(),
    )
    server.start()
    request_count = MAX_CONCURRENT_REQUESTS + 8
    try:
        with ThreadPoolExecutor(max_workers=request_count) as executor:
            pending = [
                executor.submit(_request, server, "GET", "/health")
                for _index in range(request_count)
            ]
            deadline = time.monotonic() + 5.0
            while entered < MAX_CONCURRENT_REQUESTS and time.monotonic() < deadline:
                time.sleep(0.01)
            assert entered == MAX_CONCURRENT_REQUESTS
            assert server.server is not None
            assert server.server._active_requests == MAX_CONCURRENT_REQUESTS
            overflow_deadline = time.monotonic() + 2.0
            while (
                not any(future.done() for future in pending)
                and time.monotonic() < overflow_deadline
            ):
                time.sleep(0.01)
            assert any(future.done() for future in pending)
            release.set()
            statuses = [future.result(timeout=5.0)[0] for future in pending]
        assert statuses.count(503) >= 1
        assert set(statuses) <= {200, 503}
    finally:
        release.set()
        server.stop()


def test_packvm_failure_before_authorization_does_not_initialize_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "fresh-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    lifecycle = _PackVMLifecycle()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
        packvm_lifecycle=lifecycle,
    )
    server.start()
    try:
        status, _, _ = _request(
            server,
            "POST",
            "/api/v4/packvm/prepare",
            body={},
        )
        assert status == 401
        assert lifecycle.calls == []
        assert not user_data.exists()
    finally:
        server.stop()


def test_production_handler_has_no_legacy_route_state() -> None:
    for name in (
        "approval_manager",
        "internal_token",
        "load_api_routes",
        "load_pack_routes",
        "load_pre_auth_routes",
        "load_web_mounts",
        "_api_route_exact",
        "_pack_routes",
        "_pre_auth_table",
        "_web_mounts",
    ):
        assert not hasattr(PackAPIHandler, name)


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/packs"),
        ("GET", "/api/authority/events"),
        ("GET", "/api/runtime/available"),
        ("POST", "/api/packs/scan"),
        ("POST", "/api/routes/reload"),
        ("PUT", "/api/packs/example"),
        ("DELETE", "/api/packs/example"),
        ("PATCH", "/api/packs/example"),
    ],
)
def test_legacy_api_roots_have_one_typed_retirement(
    live_server: tuple[PackAPIServer, _Dispatch],
    method: str,
    path: str,
) -> None:
    server, _ = live_server
    status, payload, _ = _request(
        server,
        method,
        path,
        body={} if method != "GET" else None,
        headers={"Authorization": "Bearer formerly-valid-root"},
    )
    assert status == 410
    assert payload["data"] == {
        "api_version": "io.tobkiri.pack-api.v4",
        "state": "legacy_api_retired",
        "retired_route": path,
        "write_set": [],
    }


@pytest.mark.parametrize(
    "method",
    ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def test_setup_complete_is_method_independent_410_no_write(
    live_server: tuple[PackAPIServer, _Dispatch],
    method: str,
) -> None:
    server, _ = live_server
    status, payload, _ = _request(
        server,
        method,
        "/api/setup/complete",
        body={"username": "must-not-write"} if method != "GET" else None,
        headers={"Authorization": "Bearer formerly-valid-root"},
    )
    assert status == 410
    assert payload["data"]["state"] == "legacy_setup_retired"
    assert payload["data"]["write_set"] == []


def test_setup_complete_head_uses_header_only_410_semantics(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.request(
        "HEAD",
        "/api/setup/complete",
        headers={"Authorization": "Bearer formerly-valid-root"},
    )
    response = connection.getresponse()
    assert response.status == 410
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert int(response.getheader("Content-Length", "0")) > 0
    assert response.read() == b""
    connection.close()


def test_setup_complete_query_is_retired_but_trailing_slash_is_absent(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    status, payload, _ = _request(
        server,
        "GET",
        "/api/setup/complete?source=legacy",
    )
    assert status == 410
    assert payload["data"]["retired_route"] == "/api/setup/complete"
    status, payload, _ = _request(server, "GET", "/api/setup/complete/")
    assert status == 404
    assert payload["error"] == "Not found"


@pytest.mark.parametrize(
    "path",
    [
        "/api//setup/complete",
        "/api/setup/./complete",
        "/api/setup/%63omplete",
        "/api/setup/complete%2F",
    ],
)
def test_setup_complete_noncanonical_variants_remain_absent(
    live_server: tuple[PackAPIServer, _Dispatch],
    path: str,
) -> None:
    server, _ = live_server
    status, _, _ = _request(server, "GET", path)
    assert status == 404


def test_setup_complete_method_matrix_is_filesystem_immutable_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "fresh-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified"),
    )
    try:
        for _cycle in (1, 2):
            server.start()
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                status, payload, _ = _request(
                    server,
                    method,
                    "/api/setup/complete?invalid_credential=yes",
                    body={"mutation": True} if method != "GET" else None,
                    headers={"Authorization": "Bearer invalid"},
                )
                assert status == 410
                assert payload["data"]["write_set"] == []
            server.stop()
        assert not user_data.exists()
        assert list(tmp_path.iterdir()) == []
    finally:
        server.stop()


def test_unknown_api_route_is_physically_absent(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    status, payload, _ = _request(server, "GET", "/api/setup/unknown")
    assert status == 404
    assert payload["error"] == "Not found"


def test_health_is_public_and_typed(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    status, payload, _ = _request(server, "GET", "/health")
    assert status == 200
    assert payload["data"] == {"status": "ok", "runtime_ready": True}


def test_panel_bootstrap_rejects_wrong_secret(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    status, _, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "wrong"},
    )
    assert status == 401


def test_panel_exchange_rejects_foreign_origin(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    status, bootstrap, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "verified-desktop"},
    )
    assert status == 200
    status, _, _ = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},
        headers={"Origin": "https://attacker.invalid"},
    )
    assert status == 403


def test_dispatch_requires_panel_cookie_and_csrf(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, dispatch = live_server
    body = {
        "contract_id": "pack.control.v4",
        "operation_id": "catalog.read",
        "payload": {},
    }
    status, payload, _ = _request(server, "POST", "/api/v4/dispatch", body=body)
    _assert_retired_generic_dispatch(status, payload)
    cookie, csrf, origin = _panel_session(server)
    status, payload, _ = _request(
        server,
        "POST",
        "/api/v4/dispatch",
        body=body,
        headers={"Cookie": cookie, "Origin": origin},
    )
    _assert_retired_generic_dispatch(status, payload)
    status, payload, _ = _request(
        server,
        "POST",
        "/api/v4/dispatch",
        body=body,
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
        },
    )
    _assert_retired_generic_dispatch(status, payload)
    assert dispatch.calls == []


def test_authenticated_generic_dispatch_is_retired_before_production_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retired generic dispatch cannot reach the production Broker or ledger."""
    user_data = tmp_path / "clean-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    dispatch = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority" / "v4.sqlite3"),
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-desktop"),
        dispatch_session=dispatch,
        app_lifecycle_manager=_Lifecycle(),
    )
    server.start()
    try:
        cookie, csrf, origin = _panel_session(server)
        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            audit_before = authority.audit_events()
        headers = {
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
        }
        status, payload, _ = _request(
            server,
            "POST",
            "/api/v4/dispatch",
            body={
                "contract_id": "tobkiri.host.pack-control.v4",
                "operation_id": "catalog.read",
                "payload": {},
            },
            headers=headers,
        )
        _assert_retired_generic_dispatch(status, payload)

        status, payload, _ = _request(
            server,
            "POST",
            "/api/v4/dispatch",
            body={
                "contract_id": "tobkiri.host.pack-control.v4",
                "operation_id": "pack.install",
                "payload": {"pack_id": "rumi_git_read_pack"},
            },
            headers=headers,
        )
        _assert_retired_generic_dispatch(status, payload)
        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            assert authority.audit_events() == audit_before
    finally:
        server.stop()


@pytest.mark.parametrize("body", [[], "text", 1, None])
def test_dispatch_rejects_non_object_json_roots(
    live_server: tuple[PackAPIServer, _Dispatch],
    body: object,
) -> None:
    server, dispatch = live_server
    cookie, csrf, origin = _panel_session(server)
    status, payload, _ = _request(
        server,
        "POST",
        "/api/v4/dispatch",
        body=body,
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
        },
    )
    _assert_retired_generic_dispatch(status, payload)
    assert dispatch.calls == []


def test_server_restart_keeps_legacy_routes_retired(
    live_server: tuple[PackAPIServer, _Dispatch],
) -> None:
    server, _ = live_server
    server.stop()
    server.start()
    status, payload, _ = _request(server, "GET", "/api/packs")
    assert status == 410
    assert payload["data"]["state"] == "legacy_api_retired"


def test_log_redaction_removes_bootstrap_code() -> None:
    redacted = PackAPIHandler._redact_log_value("/panel/?code=top-secret&x=1")
    assert redacted == "/panel/?code=[REDACTED]&x=1"
    assert "top-secret" not in redacted
