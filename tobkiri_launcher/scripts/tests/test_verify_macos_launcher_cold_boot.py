from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import plistlib
import signal
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping, Optional

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tobkiri_launcher/scripts/verify_macos_launcher_cold_boot.py"
WORKFLOW = ROOT / ".github/workflows/desktop-installers.yml"
CI_ARTIFACT_SCRIPT = ROOT / ".github/scripts/macos_ci_artifact.py"


def _load_script(module_name: str, script: Path):
    """Load a standalone repository script as an isolated test module."""
    spec = importlib.util.spec_from_file_location(
        module_name,
        script,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = _load_script("verify_macos_launcher_cold_boot", SCRIPT)
CI_ARTIFACT = _load_script("macos_ci_artifact", CI_ARTIFACT_SCRIPT)
_TEST_EXECUTABLE_NAME = VERIFY.CI_EXECUTABLE_NAME


@pytest.mark.parametrize(
    "header_delay, byte_delay, budget, succeeds",
    [(1.0, 0.0, 2.0, True), (1.0, 0.0, 0.1, False), (0.0, 0.06, 0.2, False)],
)
def test_http_cold_response_obeys_original_budget(
    header_delay: float, byte_delay: float, budget: float, succeeds: bool,
) -> None:
    """Slow valid health can finish; a short budget or trickling body cannot."""
    body = b'{"success":true}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                time.sleep(header_delay)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for value in body:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(byte_delay)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            response = VERIFY._http_request(
                server.server_port, "GET", "/health", {}, b"", budget,
            )
            elapsed = time.monotonic() - started
        finally:
            server.shutdown()
            worker.join(timeout=2)
    if succeeds:
        assert response is not None and response.body == body
    else:
        assert response is None
        assert elapsed < budget + 0.5


def test_http_probe_budget_shrinks_and_late_success_is_rejected() -> None:
    """Each stage uses the remaining boot budget, including panel exchange."""
    clock = _Clock()
    budgets: list[float] = []

    def request(_port, _method, _path, _headers, _body, budget):
        budgets.append(budget)
        clock.value += 29.0
        return VERIFY.HttpResponse(200, {}, b'{"success":true}')

    probes = _probes(clock, request, lambda _pid: None, [])
    args = (probes, 31.0, 18765, "GET", "/health", {}, b"")
    assert VERIFY._request_before_deadline(*args) is not None
    assert VERIFY._request_before_deadline(*args) is None
    assert VERIFY._request_before_deadline(*args) is None
    assert budgets == [30.0, 2.0]


@dataclass
class _Clock:
    value: float = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Process:
    pid = 4242
    stdout = None

    def poll(self) -> None:
        return None


def _write_info_plist(app_bundle: Path, executable_name: object) -> None:
    """Write the minimal signed-bundle metadata used by cold-boot tests."""
    info_plist = app_bundle / VERIFY.INFO_PLIST_RELATIVE
    info_plist.parent.mkdir(exist_ok=True)
    with info_plist.open("wb") as output:
        plistlib.dump(
            {
                "CFBundleIdentifier": VERIFY.CI_BUNDLE_IDENTIFIER,
                "CFBundleExecutable": executable_name,
            },
            output,
        )


def _bundle_and_config(
    tmp_path: Path,
    executable_name: str = _TEST_EXECUTABLE_NAME,
) -> tuple[object, Path]:
    app_bundle = tmp_path / VERIFY.CI_APP_NAME
    executable = app_bundle / VERIFY.EXECUTABLE_DIRECTORY_RELATIVE / executable_name
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    _write_info_plist(app_bundle, executable_name)

    app_data_parent = tmp_path / "Application Support"
    app_data_parent.mkdir()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    config = VERIFY.ColdBootConfig(
        app_bundle=app_bundle,
        app_data_dir=app_data_parent / VERIFY.CI_APP_DATA_DIRECTORY_NAME,
        diagnostics_dir=diagnostics,
        kernel_port=18765,
        timeout_seconds=5.0,
    )
    return config, diagnostics


def _write_embedded_broker_connection(app_data_dir: Path, broker_port: int) -> None:
    connection = app_data_dir / VERIFY.BROKER_CONNECTION_RELATIVE
    connection.parent.mkdir(parents=True)
    connection.write_text(
        json.dumps(
            {
                "version": 1,
                "host": "127.0.0.1",
                "port": broker_port,
                "pid": _Process.pid,
                "token": "not-printed-test-token",
            }
        ),
        encoding="utf-8",
    )
    connection.chmod(0o600)


def _write_embedded_host_contract(app_data_dir: Path) -> None:
    contract = app_data_dir / VERIFY.HOST_CONTRACT_RELATIVE
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        json.dumps(
            {
                "schema_version": "tobkiri.host-contract.v1",
                "profile_id": "defaults",
                "profile_revision": "sha256:" + "a" * 64,
                "activation_id": "activation:bootstrap-template",
                "plan_digest": "sha256:" + "b" * 64,
                "values": {"panel_bootstrap_secret": "cold-boot-test-secret"},
            }
        ),
        encoding="utf-8",
    )
    contract.chmod(0o600)


def _authenticated_kernel_health_response(headers: object) -> object:
    assert isinstance(headers, dict)
    challenge = headers[VERIFY.DESKTOP_HEALTH_CHALLENGE_HEADER]
    assert isinstance(challenge, str) and challenge
    challenge_response = hmac.new(
        b"cold-boot-test-secret",
        challenge.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return VERIFY.HttpResponse(
        200,
        {"content-type": "application/json"},
        json.dumps(
            {
                "success": True,
                "data": {
                    "panel_ready": True,
                    "desktop_challenge_response": challenge_response,
                },
            }
        ).encode("utf-8"),
    )


def _probes(
    clock: _Clock,
    request: Callable[
        [int, str, str, Mapping[str, str], bytes, float], Optional[VERIFY.HttpResponse]
    ],
    parent_pid: Callable[[int], Optional[int]],
    signals: list[tuple[int, signal.Signals]],
) -> object:
    return VERIFY.ColdBootProbes(
        port_available=lambda _port: True,
        reserve_broker_port=lambda _kernel_port: 18770,
        http_request=request,
        listener_pid=lambda _port: 4243,
        parent_pid=parent_pid,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        kill_process_group=lambda process_group, sent: signals.append(
            (process_group, sent)
        ),
    )


def test_cold_boot_requires_embedded_broker_then_owned_kernel_and_panel(
    tmp_path: Path,
) -> None:
    config, _diagnostics = _bundle_and_config(tmp_path)
    clock = _Clock()
    signals: list[tuple[int, signal.Signals]] = []
    calls: list[tuple[int, str, str]] = []
    launched_environment: dict[str, str] = {}
    launched_executable: Optional[Path] = None

    def request(
        port: int,
        method: str,
        path: str,
        headers: object,
        body: bytes,
        _timeout: float,
    ) -> Optional[object]:
        calls.append((port, method, path))
        if port == 18770 and method == "GET" and path == VERIFY.BROKER_HEALTH_PATH:
            _write_embedded_broker_connection(config.app_data_dir, port)
            _write_embedded_host_contract(config.app_data_dir)
            return VERIFY.HttpResponse(
                200,
                {"content-type": "application/json"},
                b'{"ok":true,"status":"running"}',
            )
        if (
            port == config.kernel_port
            and method == "GET"
            and path == VERIFY.KERNEL_HEALTH_PATH
        ):
            return _authenticated_kernel_health_response(headers)
        if (
            port == config.kernel_port
            and method == "POST"
            and path == VERIFY.PANEL_AUTH_BOOTSTRAP_PATH
        ):
            assert isinstance(headers, dict)
            assert headers["X-Rumi-Desktop-Bootstrap"] == "cold-boot-test-secret"
            assert body == b"{}"
            return VERIFY.HttpResponse(
                200,
                {"content-type": "application/json"},
                b'{"success":true,"data":{"code":"one-time-code"}}',
            )
        if (
            port == config.kernel_port
            and method == "POST"
            and path == VERIFY.PANEL_AUTH_EXCHANGE_PATH
        ):
            assert isinstance(headers, dict)
            assert headers["Origin"] == f"http://127.0.0.1:{config.kernel_port}"
            assert json.loads(body) == {"code": "one-time-code"}
            return VERIFY.HttpResponse(
                200,
                {
                    "content-type": "application/json",
                    "set-cookie": "rumi_panel_session=sealed-session; HttpOnly; Path=/",
                },
                b'{"success":true,"data":{"csrf_token":"csrf-token"}}',
            )
        return None

    def launch(executable: Path, _bundle: Path, environment: object) -> _Process:
        nonlocal launched_executable
        launched_executable = executable
        launched_environment.update(environment)
        return _Process()

    result = VERIFY.verify_cold_boot(
        config,
        probes=_probes(
            clock,
            request,
            lambda process_id: 4242 if process_id == 4243 else 1,
            signals,
        ),
        launch=launch,
        base_environment={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "CI_TEST_SECRET": "must-not-reach-app",
        },
    )

    assert result.kernel_pid == 4243
    assert result.panel_reachable is True
    assert launched_executable == (
        config.app_bundle
        / VERIFY.EXECUTABLE_DIRECTORY_RELATIVE
        / _TEST_EXECUTABLE_NAME
    )
    assert calls == [
        (18770, "GET", VERIFY.BROKER_HEALTH_PATH),
        (config.kernel_port, "GET", VERIFY.KERNEL_HEALTH_PATH),
        (config.kernel_port, "POST", VERIFY.PANEL_AUTH_BOOTSTRAP_PATH),
        (config.kernel_port, "POST", VERIFY.PANEL_AUTH_EXCHANGE_PATH),
    ]
    assert launched_environment["RUMI_VIEWER_BROKER_PORT"] == "18770"
    assert launched_environment["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert launched_environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert "CI_TEST_SECRET" not in launched_environment
    assert signals == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]


def test_panel_auth_response_rejects_unauthenticated_static_panel() -> None:
    response = VERIFY.HttpResponse(
        200,
        {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
        },
        b'<html><script src="/panel/assets/index-test.js"></script></html>',
    )

    assert VERIFY._successful_panel_auth_response(response) is None


def test_embedded_bootstrap_contract_rejects_reused_profile_and_plan_digest(
    tmp_path: Path,
) -> None:
    config, _diagnostics = _bundle_and_config(tmp_path)
    _write_embedded_host_contract(config.app_data_dir)
    contract = config.app_data_dir / VERIFY.HOST_CONTRACT_RELATIVE
    document = json.loads(contract.read_text(encoding="utf-8"))
    document["plan_digest"] = document["profile_revision"]
    contract.write_text(json.dumps(document), encoding="utf-8")
    contract.chmod(0o600)

    assert VERIFY._embedded_panel_bootstrap_secret(config) is None


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        (
            {"content-type": "text/html; charset=utf-8"},
            (
                b"<!doctype html>Tobkiri Launcher authentication required"
                b"/api/panel/auth/exchange"
            ),
        ),
        (
            {
                "content-type": "text/html; charset=utf-8",
                "cache-control": "no-store",
            },
            b"<!doctype html>Tobkiri Launcher authentication required",
        ),
    ],
)
def test_panel_auth_response_requires_successful_json_envelope(
    headers: dict[str, str],
    body: bytes,
) -> None:
    response = VERIFY.HttpResponse(200, headers, body)

    assert VERIFY._successful_panel_auth_response(response) is None


def test_cold_boot_rejects_healthy_kernel_not_owned_by_launched_app(
    tmp_path: Path,
) -> None:
    config, diagnostics = _bundle_and_config(tmp_path)
    clock = _Clock()
    signals: list[tuple[int, signal.Signals]] = []

    def request(
        port: int,
        method: str,
        path: str,
        headers: object,
        _body: bytes,
        _timeout: float,
    ) -> Optional[object]:
        if port == 18770 and method == "GET" and path == VERIFY.BROKER_HEALTH_PATH:
            _write_embedded_broker_connection(config.app_data_dir, port)
            _write_embedded_host_contract(config.app_data_dir)
            return VERIFY.HttpResponse(200, {}, b'{"ok":true,"status":"running"}')
        if (
            port == config.kernel_port
            and method == "GET"
            and path == VERIFY.KERNEL_HEALTH_PATH
        ):
            return _authenticated_kernel_health_response(headers)
        return None

    with pytest.raises(VERIFY.ColdBootError, match="not owned"):
        VERIFY.verify_cold_boot(
            config,
            probes=_probes(clock, request, lambda _process_id: 1, signals),
            launch=lambda *_args: _Process(),
            base_environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )

    diagnostic = json.loads(
        (diagnostics / VERIFY.DIAGNOSTIC_FILENAME).read_text(encoding="utf-8")
    )
    assert diagnostic["status"] == "failed"
    assert "not-printed-test-token" not in json.dumps(diagnostic)
    assert signals == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (503, b"secret-response", "http_not_ok"),
        (200, b"not-json-secret", "invalid_envelope"),
        (200, b'{"success":true,"data":null}', "invalid_payload"),
        (200, b'{"success":true,"data":{"panel_ready":false}}', "panel_not_ready"),
        (200, b'{"success":true,"data":{}}', "challenge_missing"),
        (200, b'{"success":true,"data":{"desktop_challenge_response":"private-proof"}}',
         "challenge_mismatch"),
    ],
)
def test_kernel_health_failure_reports_only_fixed_codes(
    status: int, body: bytes, expected: str,
) -> None:
    response = VERIFY.HttpResponse(status, {}, body)
    assert VERIFY._kernel_health_failure(response, "private-key", "nonce") == expected
    assert VERIFY._kernel_is_healthy(response, "private-key", "nonce") is False
    assert VERIFY._kernel_health_failure(None, "private-key", "nonce") == "unreachable"


@pytest.mark.parametrize(
    "stage",
    ["bootstrap_contract", "authenticated_kernel_health", "panel_authentication"],
)
def test_cold_boot_timeout_identifies_stage_without_response_payload(
    tmp_path: Path, stage: str,
) -> None:
    config, diagnostics = _bundle_and_config(tmp_path)
    clock = _Clock()
    signals: list[tuple[int, signal.Signals]] = []

    def request(
        port: int, method: str, path: str, headers: object, _body: bytes, _timeout: float,
    ) -> Optional[object]:
        if path == VERIFY.BROKER_HEALTH_PATH:
            _write_embedded_broker_connection(config.app_data_dir, port)
            if stage != "bootstrap_contract":
                _write_embedded_host_contract(config.app_data_dir)
            return VERIFY.HttpResponse(200, {}, b'{"ok":true,"status":"running"}')
        if path == VERIFY.KERNEL_HEALTH_PATH and stage == "panel_authentication":
            return _authenticated_kernel_health_response(headers)
        return VERIFY.HttpResponse(503, {}, b'private-response-must-not-be-logged')

    with pytest.raises(VERIFY.ColdBootError, match=f"pending stage: {stage}"):
        VERIFY.verify_cold_boot(
            config,
            probes=_probes(clock, request, lambda _pid: _Process.pid, signals),
            launch=lambda *_args: _Process(),
            base_environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
    diagnostic = (diagnostics / VERIFY.DIAGNOSTIC_FILENAME).read_text()
    assert stage in diagnostic
    assert "private-response" not in diagnostic
    assert "cold-boot-test-secret" not in diagnostic
    assert "not-printed-test-token" not in diagnostic
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_cold_boot_fails_closed_when_ci_app_data_is_not_fresh(tmp_path: Path) -> None:
    config, _diagnostics = _bundle_and_config(tmp_path)
    config.app_data_dir.mkdir()
    launched = False

    def launch(*_args: object) -> _Process:
        nonlocal launched
        launched = True
        return _Process()

    with pytest.raises(VERIFY.ColdBootError, match="must be fresh"):
        VERIFY.verify_cold_boot(
            config,
            launch=launch,
            base_environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )

    assert launched is False


def test_cold_boot_executable_matches_the_ci_attestation_contract() -> None:
    attested_executable = Path(CI_ARTIFACT.SIGNED_PATHS[0])

    assert (
        VERIFY.EXECUTABLE_DIRECTORY_RELATIVE / VERIFY.CI_EXECUTABLE_NAME
        == attested_executable
    )


@pytest.mark.parametrize(
    "executable_name",
    [
        "",
        "/tmp/launcher",
        "nested/launcher",
        "../launcher",
        ".",
        "..",
        r"nested\\launcher",
        "unattested-launcher",
    ],
)
def test_cold_boot_rejects_unsafe_cf_bundle_executable(
    tmp_path: Path,
    executable_name: str,
) -> None:
    config, _diagnostics = _bundle_and_config(tmp_path)
    _write_info_plist(config.app_bundle, executable_name)
    launched = False

    def launch(*_args: object) -> _Process:
        nonlocal launched
        launched = True
        return _Process()

    with pytest.raises(VERIFY.ColdBootError, match="CFBundleExecutable"):
        VERIFY.verify_cold_boot(config, launch=launch)

    assert launched is False


def test_cold_boot_rejects_symlink_cf_bundle_executable(tmp_path: Path) -> None:
    config, _diagnostics = _bundle_and_config(tmp_path)
    executable = (
        config.app_bundle
        / VERIFY.EXECUTABLE_DIRECTORY_RELATIVE
        / _TEST_EXECUTABLE_NAME
    )
    outside_executable = tmp_path / "outside-launcher"
    outside_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_executable.chmod(0o755)
    executable.unlink()
    executable.symlink_to(outside_executable)

    with pytest.raises(VERIFY.ColdBootError, match="canonical regular file"):
        VERIFY.verify_cold_boot(config)


def test_workflow_runs_cold_boot_after_host_seal_and_before_dmg() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    host_seal = workflow.index("Host-seal and launch-test packaged Python")
    cold_boot = workflow.index("Cold-boot packaged macOS CI/E2E Launcher")
    dmg = workflow.index("Build macOS DMG installer")

    assert host_seal < cold_boot < dmg
    assert "verify_macos_launcher_cold_boot.py" in workflow
    assert "--kernel-port 8765" in workflow
    assert "--timeout-seconds 180" in workflow
    assert "launcher-cold-boot.v1.json" in workflow
