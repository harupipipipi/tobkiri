"""Native Windows proofs for the durable Pack API control journal."""

from __future__ import annotations

import http.client
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping
from urllib.parse import quote
import uuid

import pytest

import core_runtime.secure_sqlite_path as secure_paths
import core_runtime.process_identity as process_identity
from core_runtime.control_reconciliation_v4 import (
    ControlReconciliationStore,
    ControlReconciliationUnavailableError,
)
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding as FrontendContractBinding,
    HTTPContractTarget as FrontendContractTarget,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from core_runtime.secure_sqlite_path import SecurePathError, secure_parent
from tobkiri_protocol.canonical import canonical_digest
from tests.conformance_support.host_contract import host_contract_for_session


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows file handles and reparse points",
)


class _Dispatch:
    """Small complete captured-session double for real HTTP journal tests."""

    profile_id = "defaults"
    profile_revision = "sha256:" + "b" * 64
    activation_id = "activation:native-windows"
    plan_digest = "sha256:" + "a" * 64

    def __init__(self) -> None:
        self.calls = 0

    def assert_current(self) -> None:
        """Accept the fixed test capture."""

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        """Accept the one fixed test operation."""

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, object], ...]:
        """Return the exact provider pinned into the test capture."""

        return (
            {
                "provider_id": "test.provider",
                "operation_id": "test.write",
                "profile_id": self.profile_id,
                "profile_revision": self.profile_revision,
                "activation_id": self.activation_id,
                "plan_digest": self.plan_digest,
            },
        )

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, object]:
        """Return a deterministic terminal result and count actual dispatches."""

        self.calls += 1
        return {"state": "succeeded", "value": payload["value"]}


def _binding() -> FrontendContractBinding:
    return FrontendContractBinding(
        method="POST",
        path="/api/test/write",
        presentation="identity",
        targets=(
            FrontendContractTarget(
                contribution_id="test.write",
                contract_id="test.contract.v1",
                operation_id="test.write",
                provider_id="test.provider",
                function_id="test.provider",
                allowed_payload_keys=frozenset({"value"}),
            ),
        ),
        application_id="test.application",
        route_namespace="defaultspack",
    )


def _pending_owner(path_value: str, ready: object, release: object) -> None:
    store = ControlReconciliationStore(
        Path(path_value),
        instance_id="windows-child",
        heartbeat_interval_seconds=0.05,
    )
    store.begin_operation(
        request_id="99999999-9999-4999-8999-999999999999",
        session_id="session-a",
        operation_id="test.write",
        contract_id="test.contract.v1",
        request_digest=canonical_digest({"request": "windows-child"}),
    )
    ready.set()  # type: ignore[attr-defined]
    release.wait(30.0)  # type: ignore[attr-defined]
    store.close()


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
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


def _authenticate(
    server: PackAPIServer,
    bootstrap_secret: str,
) -> tuple[str, str, str]:
    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": bootstrap_secret},
    )
    assert status == 200
    status, exchange, response_headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},  # type: ignore[index]
        headers={"Origin": origin},
    )
    assert status == 200
    cookie = next(value for key, value in response_headers if key.lower() == "set-cookie").split(
        ";", 1
    )[0]
    return cookie, str(exchange["data"]["csrf_token"]), origin  # type: ignore[index]


def test_native_windows_prepare_post_status_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows supports prepare, POST, status, lost response, and restart replay."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    auth = PanelAuthManager(bootstrap_secret="native-windows")
    dispatch = _Dispatch()
    binding = _binding()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=auth,
        dispatch_session=dispatch,
        contract_bindings=(binding,),
        host_contract=host_contract_for_session(dispatch),
    )
    server._operation_journal.prepare_for_operation()
    server.start()
    request_id = str(uuid.uuid4())
    cookie, csrf, origin = _authenticate(server, "native-windows")
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
        "X-Tobkiri-Request-ID": request_id,
    }
    route = "/api/contracts/defaultspack/" + quote("POST /api/test/write", safe="")
    try:
        status, initial, _ = _request(
            server,
            "POST",
            route,
            body={"value": "persisted"},
            headers=headers,
        )
        assert status == 200
        assert initial["data"] == {"state": "succeeded", "value": "persisted"}
        handler = server.handler_class
        assert handler is not None
        panel_auth_binding = handler._current_panel_auth_binding()
        assert panel_auth_binding is not None
        panel_session = auth.verify_session(
            cookie.split("=", 1)[1],
            panel_auth_binding,
        )
        assert panel_session is not None
        operation = server._operation_journal.operation_status(
            request_id,
            session_id=str(panel_session["session_id"]),
        )
        assert operation["state"] == "succeeded"
        replay_status, replay, _ = _request(
            server,
            "POST",
            route,
            body={"value": "persisted"},
            headers=headers,
        )
        assert (replay_status, replay) == (status, initial)
        assert dispatch.calls == 1
    finally:
        server.stop()

    restarted = PackAPIServer(
        port=0,
        panel_auth_manager=auth,
        dispatch_session=dispatch,
        contract_bindings=(binding,),
        host_contract=host_contract_for_session(dispatch),
    )
    restarted.start()
    restart_headers = {**headers, "Origin": f"http://127.0.0.1:{restarted.port}"}
    try:
        replay_status, replay, _ = _request(
            restarted,
            "POST",
            route,
            body={"value": "persisted"},
            headers=restart_headers,
        )
        assert (replay_status, replay) == (status, initial)
        assert dispatch.calls == 1
    finally:
        restarted.stop()


def test_native_windows_junction_ancestor_is_rejected(tmp_path: Path) -> None:
    """A caller-controlled directory junction never reaches SQLite."""

    for index in range(20):
        target = tmp_path / f"target-{index}"
        target.mkdir()
        junction = tmp_path / f"junction-{index}"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        path = junction / "reconciliation-v4.sqlite3"
        with pytest.raises(ControlReconciliationUnavailableError, match="unsafe"):
            ControlReconciliationStore(path).prepare_for_operation()
        assert not (target / path.name).exists()


def test_native_windows_killed_owner_recovers_pending_after_restart(
    tmp_path: Path,
) -> None:
    """A killed child is dead evidence and its pending result becomes replayable."""

    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_pending_owner, args=(str(path), ready, release))
    process.start()
    assert ready.wait(20.0)
    process.terminate()
    process.join(20.0)
    assert process.exitcode is not None

    restarted = ControlReconciliationStore(path, instance_id="windows-restarted")
    assert restarted.recover_abandoned_operations() == 1
    status = restarted.operation_status(
        "99999999-9999-4999-8999-999999999999",
        session_id="session-a",
    )
    assert status["state"] == "indeterminate"
    assert status["safe_error_code"] == "PROCESS_RESTART"
    restarted.close()


def test_native_windows_file_race_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement between pathname stat and no-follow open changes File ID."""

    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    store = ControlReconciliationStore(path)
    store.prepare_for_operation()
    replacement = path.with_name("replacement.sqlite3")
    replacement.write_bytes(path.read_bytes())
    original_open = secure_paths._open_windows_no_follow
    raced = False

    def replace_then_open(
        opened_path: Path,
        flags: int,
        mode: int = 0o600,
        *,
        directory: bool = False,
    ) -> int:
        nonlocal raced
        if not directory and opened_path == path and not raced:
            raced = True
            path.unlink()
            replacement.rename(path)
        return original_open(opened_path, flags, mode, directory=directory)

    monkeypatch.setattr(secure_paths, "_open_windows_no_follow", replace_then_open)
    with pytest.raises(ControlReconciliationUnavailableError, match="unsafe"):
        store.operation_status("missing", session_id="session-a")
    assert raced


def test_native_windows_parent_race_and_pinned_identity_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent replacement and an explicitly wrong pinned File ID are rejected."""

    parent = tmp_path / "control"
    parent.mkdir()
    path = parent / "reconciliation-v4.sqlite3"
    path.write_bytes(b"journal")
    with secure_parent(path) as opened_parent:
        pinned = opened_parent.validate_open(path.name, required=True)
        assert pinned is not None
        wrong = secure_paths.FileIdentity(
            pinned.device,
            pinned.inode + 1,
            pinned.owner,
            pinned.file_type,
        )
        with pytest.raises(SecurePathError, match="pinned"):
            opened_parent.validate_open(path.name, required=True, expected=wrong)

    original_open = secure_paths._open_windows_no_follow
    for index in range(20):
        raced_parent = tmp_path / f"race-{index}"
        raced_parent.mkdir()
        raced_path = raced_parent / "reconciliation-v4.sqlite3"
        raced_path.write_bytes(b"journal")
        moved = tmp_path / f"race-original-{index}"
        raced = False

        def replace_parent_then_open(
            opened_path: Path,
            flags: int,
            mode: int = 0o600,
            *,
            directory: bool = False,
        ) -> int:
            nonlocal raced
            if directory and opened_path == raced_parent and not raced:
                raced = True
                raced_parent.rename(moved)
                raced_parent.mkdir()
            return original_open(opened_path, flags, mode, directory=directory)

        with monkeypatch.context() as patcher:
            patcher.setattr(
                secure_paths,
                "_open_windows_no_follow",
                replace_parent_then_open,
            )
            with pytest.raises(SecurePathError, match="identity changed"):
                with secure_parent(raced_path):
                    pass
        assert raced


def test_native_windows_process_queries_close_handles_and_denials_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated native FILETIME queries leak no handles; denial is not death."""

    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
        return int(count.value)

    before = handle_count()
    for _ in range(200):
        assert process_identity.process_start_identity(os.getpid()).state == "live"
    after = handle_count()
    assert after <= before + 2

    class DeniedAPI:
        def open_process(self, process_id: int) -> int | None:
            raise PermissionError("access denied")

        def process_creation_time(self, handle: int) -> int | None:
            raise AssertionError("unreachable")

        def close_handle(self, handle: int) -> None:
            raise AssertionError("unreachable")

    monkeypatch.setattr(process_identity, "_load_windows_process_api", lambda: DeniedAPI())
    assert process_identity.process_start_identity(424242).state == "unknown"
