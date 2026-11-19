#!/usr/bin/env python3
"""Verify a sealed macOS Launcher can complete a local, fresh-state boot.

The verifier intentionally starts the executable inside the non-publishable
CI/E2E app bundle directly.  It uses an empty application-data directory,
binds the host broker to a verifier-selected loopback port, and accepts the
Kernel only when its listener is a descendant of that launched application.

No app-data directory is removed by this tool.  A pre-existing directory is a
failure, which makes a CI run fail closed rather than accidentally reusing
credentials, an old Kernel, or a previous test result.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import plistlib
import re
import secrets
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode


CI_BUNDLE_IDENTIFIER = "dev.tobkiri.launcher.ci-e2e"
CI_APP_DATA_DIRECTORY_NAME = "ci-e2e-app-data"
CI_APP_DATA_ROOT_ENV = "TOBKIRI_CI_E2E_APP_DATA_ROOT"
CI_APP_NAME = "Tobkiri Launcher CI E2E.app"
CI_EXECUTABLE_NAME = "tobkiri-launcher"
EXECUTABLE_DIRECTORY_RELATIVE = Path("Contents/MacOS")
INFO_PLIST_RELATIVE = Path("Contents/Info.plist")
BROKER_CONNECTION_RELATIVE = Path("user_data/host_broker/connection.json")
HOST_CONTRACT_RELATIVE = Path("user_data/host_contract.json")
BROKER_HEALTH_PATH = "/api/host/health"
KERNEL_HEALTH_PATH = "/health"
DESKTOP_HEALTH_CHALLENGE_HEADER = "X-Rumi-Desktop-Health-Challenge"
PANEL_AUTH_BOOTSTRAP_PATH = "/api/panel/auth/bootstrap"
PANEL_AUTH_EXCHANGE_PATH = "/api/panel/auth/exchange"
SETUP_STATUS_PATH = "/api/setup/status"
PACKVM_DOCTOR_PATH = "/api/v4/packvm/doctor"
SETUP_PANEL_PATH = "/panel/setup"
DEFAULT_KERNEL_PORT = 8765
POLL_INTERVAL_SECONDS = 0.2
HTTP_CONNECT_TIMEOUT_SECONDS = 0.8
HTTP_RESPONSE_TIMEOUT_SECONDS = 30.0
TERMINATION_GRACE_SECONDS = 5.0
# Launcher waits up to 160 seconds for its managed shutdown to complete.
INSTALLED_SHUTDOWN_TIMEOUT_SECONDS = 165.0
MAX_ANCESTRY_DEPTH = 64
DIAGNOSTIC_FILENAME = "launcher-cold-boot.v1.json"


class ColdBootError(RuntimeError):
    """Raised when the packaged Launcher cannot prove a safe cold boot."""


@dataclass(frozen=True)
class HttpResponse:
    """A bounded local HTTP response used by the boot readiness probes."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json_object(self) -> Optional[dict[str, object]]:
        """Return a JSON object response, or ``None`` for any other payload."""
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class ColdBootConfig:
    """Validated inputs for the non-publishable macOS cold-boot gate."""

    app_bundle: Path
    app_data_dir: Path
    diagnostics_dir: Path
    kernel_port: int
    timeout_seconds: float
    expect_setup: bool = False
    user_setup_timeout_seconds: float | None = None
    user_chat: UserChatObservation | None = None
    expect_packvm_disabled: bool = False


@dataclass(frozen=True)
class UserChatObservation:
    """Expected saved Registry route for the operator's ordinary GUI chat."""

    timeout_seconds: float
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class ColdBootResult:
    """Non-sensitive evidence emitted after a successful cold boot."""

    broker_port: int
    kernel_port: int
    launcher_pid: int
    kernel_pid: int
    panel_reachable: bool
    setup_panel_reachable: bool = False
    setup_activation_verified: bool = False
    saved_chat_verified: bool = False


@dataclass(frozen=True)
class _HostContract:
    profile_id: str
    profile_revision: str
    plan_digest: str
    activation_id: str
    bootstrap_secret: str = field(repr=False)


@dataclass(frozen=True)
class _ActivatedSetup:
    result: ColdBootResult
    contract: _HostContract
    cookie: str = field(repr=False)


@dataclass(frozen=True)
class ColdBootProbes:
    """Injectable OS and loopback probes for deterministic unit testing."""

    port_available: Callable[[int], bool]
    reserve_broker_port: Callable[[int], int]
    http_request: Callable[
        [int, str, str, Mapping[str, str], bytes, float], Optional[HttpResponse]
    ]
    listener_pid: Callable[[int], Optional[int]]
    parent_pid: Callable[[int], Optional[int]]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    kill_process_group: Callable[[int, signal.Signals], None]


class _OutputCollector:
    """Collect a bounded amount of child output without writing it unredacted."""

    _MAX_CAPTURE_BYTES = 32 * 1024

    def __init__(self, process: Any) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        stream = getattr(process, "stdout", None)
        if stream is not None:
            self._thread = threading.Thread(
                target=self._drain,
                args=(stream,),
                name="tobkiri-cold-boot-output",
                daemon=True,
            )
            self._thread.start()

    def _drain(self, stream: Any) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            with self._lock:
                remaining = self._MAX_CAPTURE_BYTES - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])

    def text(self) -> str:
        """Return the captured output decoded with replacement characters."""
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        with self._lock:
            return bytes(self._buffer).decode("utf-8", errors="replace")


def _canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ColdBootError(f"{label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise ColdBootError(f"{label} is unavailable") from error
    if path != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ColdBootError(f"{label} must be a canonical real directory")
    return path


def _canonical_regular_file(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise ColdBootError(f"{label} is unavailable") from error
    if path != resolved or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ColdBootError(f"{label} must be a canonical regular file")
    return path


def _validate_port(value: int, label: str) -> int:
    if not 1 <= value <= 65535:
        raise ColdBootError(f"{label} must be between 1 and 65535")
    return value


def _read_info_plist(app_bundle: Path) -> Mapping[str, Any]:
    info_plist = _canonical_regular_file(app_bundle / INFO_PLIST_RELATIVE, "Info.plist")
    try:
        with info_plist.open("rb") as source:
            metadata = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as error:
        raise ColdBootError("CI/E2E Info.plist is unreadable") from error
    if not isinstance(metadata, dict):
        raise ColdBootError("CI/E2E Info.plist must contain a dictionary")
    return metadata


def _bundle_executable_name(metadata: Mapping[str, Any]) -> str:
    executable_name = metadata.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise ColdBootError("CI/E2E CFBundleExecutable must be a non-empty filename")
    if executable_name in {".", ".."}:
        raise ColdBootError("CI/E2E CFBundleExecutable must not traverse directories")
    if Path(executable_name).is_absolute():
        raise ColdBootError("CI/E2E CFBundleExecutable must be bundle-relative")
    if "/" in executable_name or "\\" in executable_name:
        raise ColdBootError(
            "CI/E2E CFBundleExecutable must not contain path separators"
        )
    if "\x00" in executable_name:
        raise ColdBootError("CI/E2E CFBundleExecutable contains an invalid character")
    if executable_name != CI_EXECUTABLE_NAME:
        raise ColdBootError("CI/E2E CFBundleExecutable is not the attested executable")
    return executable_name


def _validate_app_bundle(app_bundle: Path) -> tuple[Path, Path]:
    app_bundle = _canonical_directory(app_bundle, "application bundle")
    if app_bundle.name != CI_APP_NAME:
        raise ColdBootError("cold boot requires the non-publishable CI/E2E app bundle")
    metadata = _read_info_plist(app_bundle)
    if metadata.get("CFBundleIdentifier") != CI_BUNDLE_IDENTIFIER:
        raise ColdBootError("CI/E2E application bundle identifier is invalid")
    executable_name = _bundle_executable_name(metadata)
    executable = _canonical_regular_file(
        app_bundle / EXECUTABLE_DIRECTORY_RELATIVE / executable_name,
        "CI/E2E application executable",
    )
    if not os.access(executable, os.X_OK):
        raise ColdBootError("CI/E2E application executable is not executable")
    return app_bundle, executable


def _validate_fresh_app_data(app_data_dir: Path) -> Path:
    if not app_data_dir.is_absolute():
        raise ColdBootError("application-data directory must be absolute")
    if app_data_dir.name != CI_APP_DATA_DIRECTORY_NAME:
        raise ColdBootError("application-data directory is not the CI/E2E directory")
    parent = _canonical_directory(
        app_data_dir.parent,
        "application-data parent directory",
    )
    parent_metadata = parent.lstat()
    if (
        parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise ColdBootError(
            "application-data parent directory must be owned and private"
        )
    if app_data_dir.exists() or app_data_dir.is_symlink():
        raise ColdBootError("CI/E2E application-data directory must be fresh")
    return app_data_dir


def _create_fresh_app_data(app_data_dir: Path) -> None:
    """Create the validated CI/E2E root without permitting path substitution."""
    try:
        app_data_dir.mkdir(mode=0o700)
        metadata = app_data_dir.lstat()
        resolved = app_data_dir.resolve(strict=True)
    except OSError as error:
        raise ColdBootError(
            "CI/E2E application-data directory could not be created"
        ) from error
    if (
        resolved != app_data_dir
        or app_data_dir.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ColdBootError(
            "CI/E2E application-data directory is not owned and private"
        )


def _validate_config(config: ColdBootConfig) -> tuple[ColdBootConfig, Path]:
    app_bundle, executable = _validate_app_bundle(config.app_bundle)
    app_data_dir = _validate_fresh_app_data(config.app_data_dir)
    diagnostics_dir = _canonical_directory(
        config.diagnostics_dir,
        "diagnostics directory",
    )
    kernel_port = _validate_port(config.kernel_port, "kernel port")
    if not 5.0 <= config.timeout_seconds <= 300.0:
        raise ColdBootError("timeout must be between 5 and 300 seconds")
    if config.user_setup_timeout_seconds is not None and (
        not config.expect_setup or not 5.0 <= config.user_setup_timeout_seconds <= 900.0
    ):
        raise ColdBootError("user Setup wait requires fresh Setup and a 5–900 second timeout")
    if config.expect_packvm_disabled and not config.expect_setup:
        raise ColdBootError("the PackVM-disabled probe requires the native Setup installer")
    if config.user_chat is not None and (
        config.user_setup_timeout_seconds is None
        or not 5.0 <= config.user_chat.timeout_seconds <= 900.0
        or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value) is None
            for value in (config.user_chat.provider_id, config.user_chat.model_id)
        )
    ):
        raise ColdBootError(
            "user chat wait requires user Setup, a 5–900 second timeout and valid route IDs"
        )
    return (
        replace(
            config,
            app_bundle=app_bundle,
            app_data_dir=app_data_dir,
            diagnostics_dir=diagnostics_dir,
            kernel_port=kernel_port,
        ),
        executable,
    )


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _reserve_broker_port(kernel_port: int) -> int:
    for _attempt in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port != kernel_port:
            return port
    raise ColdBootError("could not reserve a distinct loopback broker port")


def _http_request(
    port: int,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> Optional[HttpResponse]:
    if timeout_seconds <= 0:
        return None
    deadline = time.monotonic() + timeout_seconds
    connection = http.client.HTTPConnection(
        "127.0.0.1", port,
        timeout=min(HTTP_CONNECT_TIMEOUT_SECONDS, timeout_seconds),
    )
    timer: Optional[threading.Timer] = None

    def remaining_timeout() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or connection.sock is None:
            raise TimeoutError("cold-boot HTTP budget expired")
        connection.sock.settimeout(remaining)

    try:
        connection.connect()
        remaining_timeout()
        owned_socket = connection.sock
        assert owned_socket is not None

        def expire_socket() -> None:
            # Socket timeouts reset on each read; a slow response must still
            # stop at the original request/boot deadline.
            try:
                owned_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(deadline - time.monotonic(), expire_socket)
        timer.daemon = True
        timer.start()
        connection.request(method, path, body=body, headers=dict(headers))
        remaining_timeout()
        response = connection.getresponse()
        # HTTP/1.0 getresponse may detach the socket from the connection. The
        # response retains its already bounded socket until the body is read.
        body = response.read(128 * 1024)
        if time.monotonic() >= deadline:
            return None
        headers = {key.lower(): value for key, value in response.getheaders()}
        return HttpResponse(response.status, headers, body)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def _request_before_deadline(
    probes: ColdBootProbes, deadline: float, port: int, method: str, path: str,
    headers: Mapping[str, str], body: bytes,
) -> Optional[HttpResponse]:
    """Allow a cold health read without resetting the whole boot deadline."""
    remaining = deadline - probes.monotonic()
    if remaining <= 0:
        return None
    response = probes.http_request(
        port, method, path, headers, body,
        min(HTTP_RESPONSE_TIMEOUT_SECONDS, remaining),
    )
    return response if probes.monotonic() < deadline else None


def _listener_pid(port: int) -> Optional[int]:
    command = [
        "/usr/sbin/lsof",
        "-nP",
        "-t",
        f"-iTCP@127.0.0.1:{port}",
        "-sTCP:LISTEN",
    ]
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
        timeout=2,
    )
    if result.returncode not in {0, 1}:
        return None
    values = {
        int(line)
        for line in result.stdout.splitlines()
        if line.isascii() and line.isdecimal() and int(line) > 0
    }
    return next(iter(values)) if len(values) == 1 else None


def _parent_pid(process_id: int) -> Optional[int]:
    result = subprocess.run(
        ["/bin/ps", "-p", str(process_id), "-o", "ppid="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
        timeout=2,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value.isascii() or not value.isdecimal():
        return None
    parent = int(value)
    return parent if parent > 0 else None


def _system_probes() -> ColdBootProbes:
    return ColdBootProbes(
        port_available=_port_available,
        reserve_broker_port=_reserve_broker_port,
        http_request=_http_request,
        listener_pid=_listener_pid,
        parent_pid=_parent_pid,
        monotonic=time.monotonic,
        sleep=time.sleep,
        kill_process_group=os.killpg,
    )


def _local_only_environment(
    base_environment: Mapping[str, str],
    broker_port: int,
    app_data_dir: Path,
) -> dict[str, str]:
    """Return a minimal child environment with cloud traffic looped locally."""
    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "USER",
    )
    environment = {
        key: value
        for key in allowed
        if (value := base_environment.get(key))
    }
    if "HOME" not in environment:
        raise ColdBootError("the CI/E2E launch environment must retain HOME")
    if "PATH" not in environment:
        raise ColdBootError("the CI/E2E launch environment must retain PATH")

    # The Launcher currently performs a delayed update check.  Route every
    # non-loopback HTTP client through an unbound loopback proxy, while keeping
    # the readiness checks for the embedded local services direct.  This keeps
    # the cold boot local even if a future startup path adds another HTTP call.
    local_proxy = "http://127.0.0.1:9"
    environment.update(
        {
            "ALL_PROXY": local_proxy,
            "HTTP_PROXY": local_proxy,
            "HTTPS_PROXY": local_proxy,
            "NO_PROXY": "127.0.0.1,localhost",
            "RUMI_VIEWER_BROKER_PORT": str(broker_port),
            CI_APP_DATA_ROOT_ENV: str(app_data_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "all_proxy": local_proxy,
            "http_proxy": local_proxy,
            "https_proxy": local_proxy,
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return environment


def _launch_application(
    executable: Path,
    app_bundle: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [str(executable)],
        cwd=app_bundle,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _broker_connection_is_embedded(
    connection_path: Path,
    broker_port: int,
    launcher_pid: int,
) -> bool:
    try:
        _canonical_regular_file(connection_path, "host broker connection")
        metadata = connection_path.lstat()
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            return False
        document = json.loads(connection_path.read_text(encoding="utf-8"))
    except (ColdBootError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    token = document.get("token")
    return (
        document.get("version") == 1
        and document.get("host") == "127.0.0.1"
        and document.get("port") == broker_port
        and document.get("pid") == launcher_pid
        and isinstance(token, str)
        and bool(token)
    )


def _broker_is_ready(response: Optional[HttpResponse]) -> bool:
    if response is None or response.status != 200:
        return False
    document = response.json_object()
    return document == {"ok": True, "status": "running"}


def _kernel_is_healthy(
    response: Optional[HttpResponse],
    bootstrap_secret: str,
    challenge: str,
) -> bool:
    """Verify both Kernel readiness and proof of the sealed bootstrap secret."""
    return _kernel_health_failure(response, bootstrap_secret, challenge) is None


def _kernel_health_failure(
    response: Optional[HttpResponse], bootstrap_secret: str, challenge: str,
) -> str | None:
    """Return a fixed diagnostic code, never response text or credential data."""
    if response is None:
        return "unreachable"
    if response.status != 200:
        return "http_not_ok"
    document = response.json_object()
    if not isinstance(document, dict) or document.get("success") is not True:
        return "invalid_envelope"
    payload = document.get("data")
    if not isinstance(payload, dict):
        return "invalid_payload"
    if payload.get("panel_ready") is False:
        return "panel_not_ready"
    response_mac = payload.get("desktop_challenge_response")
    if not isinstance(response_mac, str):
        return "challenge_missing"
    expected_mac = hmac.new(
        bootstrap_secret.encode("utf-8"),
        challenge.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return None if hmac.compare_digest(response_mac, expected_mac) else "challenge_mismatch"


def _read_host_contract(config: ColdBootConfig) -> _HostContract | None:
    """Read the owner-only contract used to authenticate the watched Kernel."""

    contract_path = config.app_data_dir / HOST_CONTRACT_RELATIVE
    try:
        _canonical_regular_file(contract_path, "host contract")
        metadata = contract_path.lstat()
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            return None
        document = json.loads(contract_path.read_text(encoding="utf-8"))
    except (ColdBootError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != "tobkiri.host-contract.v1":
        return None
    profile_id = document.get("profile_id")
    profile_revision = document.get("profile_revision")
    activation_id = document.get("activation_id")
    plan_digest = document.get("plan_digest")
    if (
        not isinstance(profile_id, str)
        or not profile_id
        or not isinstance(profile_revision, str)
        or not isinstance(plan_digest, str)
        or profile_revision == plan_digest
        or re.fullmatch(r"sha256:[0-9a-f]{64}", profile_revision) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", plan_digest) is None
        or not isinstance(activation_id, str)
        or re.fullmatch(r"activation:[a-z0-9][a-z0-9._-]{7,127}", activation_id)
        is None
    ):
        return None
    values = document.get("values")
    secret = values.get("panel_bootstrap_secret") if isinstance(values, dict) else None
    if not isinstance(secret, str) or not secret or secret != secret.strip():
        return None
    return _HostContract(profile_id, profile_revision, plan_digest, activation_id, secret)


def _successful_panel_auth_response(response: Optional[HttpResponse]) -> dict[str, object] | None:
    """Return a successful API response body without retaining arbitrary JSON."""

    if response is None or response.status != 200:
        return None
    payload = response.json_object()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _panel_session(
    config: ColdBootConfig,
    probes: ColdBootProbes,
    bootstrap_secret: str,
    deadline: float,
) -> str | None:
    """Exchange a private bootstrap code for an observer-only panel session."""

    bootstrap = _request_before_deadline(
        probes, deadline,
        config.kernel_port,
        "POST",
        PANEL_AUTH_BOOTSTRAP_PATH,
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Rumi-Desktop-Bootstrap": bootstrap_secret,
        },
        b"{}",
    )
    bootstrap_data = _successful_panel_auth_response(bootstrap)
    code = bootstrap_data.get("code") if bootstrap_data is not None else None
    if not isinstance(code, str) or not code or code != code.strip():
        return None

    exchange = _request_before_deadline(
        probes, deadline,
        config.kernel_port,
        "POST",
        PANEL_AUTH_EXCHANGE_PATH,
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{config.kernel_port}",
        },
        json.dumps({"code": code}, separators=(",", ":")).encode("utf-8"),
    )
    exchange_data = _successful_panel_auth_response(exchange)
    csrf_token = exchange_data.get("csrf_token") if exchange_data is not None else None
    if not isinstance(csrf_token, str) or not csrf_token or csrf_token != csrf_token.strip():
        return None
    return exchange.headers.get("set-cookie") if exchange is not None else None


def _panel_session_cookie(set_cookie: str) -> str | None:
    cookies: SimpleCookie[str] = SimpleCookie()
    try:
        cookies.load(set_cookie)
    except CookieError:
        return None
    session = cookies.get("rumi_panel_session")
    if session is None or not session.value or not session["httponly"]:
        return None
    return f"rumi_panel_session={session.coded_value}"


def _setup_status(
    config: ColdBootConfig, probes: ColdBootProbes, cookie: str, deadline: float,
) -> dict[str, object] | None:
    status = _request_before_deadline(
        probes, deadline, config.kernel_port, "GET", SETUP_STATUS_PATH,
        {"Accept": "application/json", "Cookie": cookie}, b"",
    )
    return _successful_panel_auth_response(status)


def _packvm_disabled_doctor(
    config: ColdBootConfig, probes: ColdBootProbes, cookie: str, deadline: float,
) -> bool:
    reply = _request_before_deadline(
        probes, deadline, config.kernel_port, "GET", PACKVM_DOCTOR_PATH,
        {"Accept": "application/json", "Cookie": cookie}, b"",
    )
    data = _successful_panel_auth_response(reply)
    return (
        data is not None
        and data.get("ready") is False
        and data.get("backend_id") == "disabled"
        and data.get("instance") == "disabled"
        and "attestation_digest" in data
        and data["attestation_digest"] is None
    )


def _fresh_setup_is_reachable(
    config: ColdBootConfig, probes: ColdBootProbes, set_cookie: str, deadline: float,
) -> bool:
    """Observe the original Setup route and its pending state without publishing."""
    cookie = _panel_session_cookie(set_cookie)
    if cookie is None:
        return False
    state = _setup_status(config, probes, cookie, deadline)
    if (
        state is None
        or state.get("setup_state") != "profile_transaction_required"
        or state.get("needs_setup") is not True
        or state.get("active_profile_ready") is not False
        or state.get("launch_ready") is not False
        or state.get("host_catalog_verified") is not True
    ):
        return False
    if config.expect_packvm_disabled and not _packvm_disabled_doctor(
        config, probes, cookie, deadline,
    ):
        raise ColdBootError("authenticated Kernel Doctor did not confirm PackVM is disabled")
    panel = _request_before_deadline(
        probes, deadline, config.kernel_port, "GET", SETUP_PANEL_PATH,
        {"Accept": "text/html", "Cookie": cookie}, b"",
    )
    return (
        panel is not None and panel.status == 200
        and panel.headers.get("content-type", "").startswith("text/html")
        and b'<div id="root">' in panel.body
        and b'type="module"' in panel.body
    )


def _panel_authentication_is_reachable(
    config: ColdBootConfig, probes: ColdBootProbes, bootstrap_secret: str, deadline: float,
) -> bool:
    set_cookie = _panel_session(config, probes, bootstrap_secret, deadline)
    if set_cookie is None:
        return False
    if config.expect_setup:
        return _fresh_setup_is_reachable(config, probes, set_cookie, deadline)
    return _panel_session_cookie(set_cookie) is not None


def _is_descendant(
    candidate_pid: int,
    ancestor_pid: int,
    parent_pid: Callable[[int], Optional[int]],
) -> bool:
    if candidate_pid <= 0 or candidate_pid == ancestor_pid:
        return False
    current_pid = candidate_pid
    for _depth in range(MAX_ANCESTRY_DEPTH):
        parent = parent_pid(current_pid)
        if parent is None or parent == current_pid:
            return False
        if parent == ancestor_pid:
            return True
        if parent <= 1:
            return False
        current_pid = parent
    return False


def _terminate_owned_process_group(
    process: Any,
    probes: ColdBootProbes,
) -> None:
    """Terminate only the new-session process group created for this launch."""
    process_id = int(process.pid)
    try:
        probes.kill_process_group(process_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = probes.monotonic() + TERMINATION_GRACE_SECONDS
    while process.poll() is None and probes.monotonic() < deadline:
        probes.sleep(POLL_INTERVAL_SECONDS)

    # Sending SIGKILL to the same pgid after its leader has exited also reaps
    # any direct group member.  We never search for or signal unrelated pids.
    try:
        probes.kill_process_group(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop_installed_launcher(
    process: Any, probes: ColdBootProbes, kernel_port: int, broker_port: int,
) -> None:
    """Wait for the Launcher's own shutdown, including its separate Kernel group."""
    process_id = int(process.pid)
    try:
        probes.kill_process_group(process_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = probes.monotonic() + INSTALLED_SHUTDOWN_TIMEOUT_SECONDS
    while probes.monotonic() < deadline:
        status = process.poll()
        if status is not None and status != 0:
            raise ColdBootError("installed Launcher reported incomplete shutdown")
        if status == 0 and all(probes.port_available(port) for port in (kernel_port, broker_port)):
            return
        probes.sleep(POLL_INTERVAL_SECONDS)
    if process.poll() is None:
        try:
            probes.kill_process_group(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise ColdBootError("installed Launcher shutdown did not release both listeners")


def _sanitize_diagnostic_text(value: str) -> str:
    value = re.sub(
        r"(?i)\b(?:api[_-]?key|authorization|credential|password|secret|token)"
        r"\s*[:=]\s*[^\s,;]+",
        "[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}",
        "[redacted]",
        value,
    )
    value = re.sub(r"[A-Za-z0-9+/=_-]{80,}", "[opaque]", value)
    return value[-8_000:]


def _write_failure_diagnostic(
    config: ColdBootConfig,
    broker_port: Optional[int],
    launcher_pid: Optional[int],
    error: BaseException,
    output: str,
) -> None:
    document = {
        "schema": "io.tobkiri.launcher.cold-boot.v1",
        "status": "failed",
        "broker_port": broker_port,
        "kernel_port": config.kernel_port,
        "launcher_pid": launcher_pid,
        "error": _sanitize_diagnostic_text(str(error)),
        "process_output_tail": _sanitize_diagnostic_text(output),
    }
    destination = config.diagnostics_dir / DIAGNOSTIC_FILENAME
    destination.write_text(
        json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)


def _write_preflight_diagnostic(
    config: ColdBootConfig,
    error: BaseException,
) -> None:
    """Preserve a safe diagnostic when validation fails before process launch."""
    try:
        diagnostics_dir = _canonical_directory(
            config.diagnostics_dir,
            "diagnostics directory",
        )
    except ColdBootError:
        return
    destination = diagnostics_dir / DIAGNOSTIC_FILENAME
    destination.write_text(
        json.dumps(
            {
                "schema": "io.tobkiri.launcher.cold-boot.v1",
                "status": "failed-preflight",
                "error": _sanitize_diagnostic_text(str(error)),
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)


def _wait_for_readiness(
    config: ColdBootConfig,
    probes: ColdBootProbes,
    process: Any,
    broker_port: int,
) -> ColdBootResult:
    deadline = probes.monotonic() + config.timeout_seconds
    broker_ready = False
    kernel_ownership_error = False
    panel_reachable = False
    pending_stage = "embedded_broker"
    health_failure = "not_checked"
    contract: _HostContract | None = None
    connection_path = config.app_data_dir / BROKER_CONNECTION_RELATIVE

    while probes.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise ColdBootError(
                f"CI/E2E Launcher exited before readiness (status {exit_code})"
            )

        if not broker_ready:
            broker_response = _request_before_deadline(
                probes, deadline,
                broker_port,
                "GET",
                BROKER_HEALTH_PATH,
                {"Accept": "application/json"},
                b"",
            )
            if _broker_is_ready(broker_response) and _broker_connection_is_embedded(
                connection_path,
                broker_port,
                int(process.pid),
            ):
                broker_ready = True

        if broker_ready:
            pending_stage = "bootstrap_contract"
            contract = contract or _read_host_contract(config)
            bootstrap_secret = contract.bootstrap_secret if contract is not None else None
            challenge = secrets.token_urlsafe(32)
            kernel_response = _request_before_deadline(
                probes, deadline,
                config.kernel_port,
                "GET",
                KERNEL_HEALTH_PATH,
                {
                    "Accept": "application/json",
                    DESKTOP_HEALTH_CHALLENGE_HEADER: challenge,
                },
                b"",
            )
            if bootstrap_secret is not None:
                pending_stage = "authenticated_kernel_health"
                health_failure = _kernel_health_failure(
                    kernel_response, bootstrap_secret, challenge,
                )
            if bootstrap_secret is not None and health_failure is None:
                kernel_pid = probes.listener_pid(config.kernel_port)
                pending_stage = "kernel_process_ownership"
                if kernel_pid is not None and _is_descendant(
                    kernel_pid,
                    int(process.pid),
                    probes.parent_pid,
                ):
                    pending_stage = "authenticated_setup" if config.expect_setup else "panel_authentication"
                    panel_reachable = (
                        _panel_authentication_is_reachable(
                            config,
                            probes,
                            bootstrap_secret,
                            deadline,
                        )
                        if bootstrap_secret is not None
                        else False
                    )
                    if panel_reachable and probes.monotonic() < deadline:
                        if config.expect_packvm_disabled and (
                            probes.listener_pid(config.kernel_port) != kernel_pid
                            or not _is_descendant(kernel_pid, int(process.pid), probes.parent_pid)
                            or _read_host_contract(config) != contract
                            or process.poll() is not None
                        ):
                            raise ColdBootError("the Launcher-owned Kernel changed after Doctor")
                        return ColdBootResult(
                            broker_port=broker_port,
                            kernel_port=config.kernel_port,
                            launcher_pid=int(process.pid),
                            kernel_pid=kernel_pid,
                            panel_reachable=True,
                            setup_panel_reachable=config.expect_setup,
                        )
                else:
                    kernel_ownership_error = True

        probes.sleep(POLL_INTERVAL_SECONDS)

    if kernel_ownership_error:
        raise ColdBootError(
            "Kernel health listener is not owned by the launched CI/E2E app"
        )
    if not broker_ready:
        raise ColdBootError("embedded host broker did not become ready before timeout")
    raise ColdBootError(
        "owned Kernel health and panel authentication did not become ready before "
        f"timeout (pending stage: {pending_stage}; health: {health_failure})"
    )


def _wait_for_user_setup_activation(
    config: ColdBootConfig, probes: ColdBootProbes, process: Any, initial: ColdBootResult,
) -> _ActivatedSetup:
    """Verify that the manually reviewed Setup was adopted by a new owned Kernel."""
    assert config.user_setup_timeout_seconds is not None
    deadline = probes.monotonic() + config.user_setup_timeout_seconds
    watched_session: tuple[int, _HostContract, str] | None = None
    while probes.monotonic() < deadline:
        if process.poll() is not None:
            raise ColdBootError("installed Launcher exited while observing Setup")
        pid = probes.listener_pid(config.kernel_port)
        contract = _read_host_contract(config)
        if (
            pid is not None
            and pid != initial.kernel_pid
            and _is_descendant(pid, initial.launcher_pid, probes.parent_pid)
            and contract is not None
            and contract.profile_id == "defaults"
            and contract.activation_id != "activation:bootstrap-template"
        ):
            if watched_session is None or watched_session[:2] != (pid, contract):
                watched_session = None
                challenge = secrets.token_urlsafe(32)
                health = _request_before_deadline(
                    probes, deadline, config.kernel_port, "GET", KERNEL_HEALTH_PATH,
                    {"Accept": "application/json", DESKTOP_HEALTH_CHALLENGE_HEADER: challenge},
                    b"",
                )
                if _kernel_is_healthy(health, contract.bootstrap_secret, challenge):
                    set_cookie = _panel_session(
                        config, probes, contract.bootstrap_secret, deadline,
                    )
                    cookie = _panel_session_cookie(set_cookie) if set_cookie is not None else None
                    if cookie is not None:
                        watched_session = (pid, contract, cookie)
            if watched_session is not None:
                state = _setup_status(config, probes, watched_session[2], deadline)
                if state is None:
                    watched_session = None
                elif (
                    state.get("setup_state") == "complete"
                    and state.get("needs_setup") is False
                    and state.get("active_profile_ready") is True
                    and state.get("host_catalog_verified") is True
                    and state.get("launch_ready") is True
                    and state.get("runtime_ready") is True
                    and state.get("runtime_status") == "runtime_ready"
                    and state.get("profile_id") == contract.profile_id
                    and state.get("plan_digest") == contract.plan_digest
                    and state.get("activation_id") == contract.activation_id
                    and probes.listener_pid(config.kernel_port) == pid
                    and _is_descendant(pid, initial.launcher_pid, probes.parent_pid)
                    and _read_host_contract(config) == contract
                    and process.poll() is None
                    and probes.monotonic() < deadline
                ):
                    if config.expect_packvm_disabled:
                        if not _packvm_disabled_doctor(config, probes, watched_session[2], deadline):
                            raise ColdBootError(
                                "restarted Kernel Doctor did not confirm PackVM is disabled"
                            )
                        if (
                            probes.listener_pid(config.kernel_port) != pid
                            or not _is_descendant(pid, initial.launcher_pid, probes.parent_pid)
                            or _read_host_contract(config) != contract
                            or process.poll() is not None
                        ):
                            raise ColdBootError("the active Launcher-owned Kernel changed after Doctor")
                    return _ActivatedSetup(
                        replace(initial, kernel_pid=pid, setup_activation_verified=True),
                        contract,
                        watched_session[2],
                    )
        else:
            watched_session = None
        probes.sleep(1.0)
    raise ColdBootError("user Setup did not reach an active Profile on the restarted Kernel")


def _read_panel_contract(
    config: ColdBootConfig, probes: ColdBootProbes, cookie: str, deadline: float,
    path: str, **query: str,
) -> dict[str, object]:
    target = path + ("?" + urlencode(query) if query else "")
    response = _request_before_deadline(
        probes, deadline, config.kernel_port, "GET",
        "/api/contracts/defaultspack/" + quote(f"GET {target}", safe=""),
        {
            "Accept": "application/json",
            "Cookie": cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
        b"",
    )
    return _successful_panel_auth_response(response) or {}


def _read_records(document: Mapping[str, object], key: str) -> list[dict[str, object]]:
    records = document.get(key)
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def _chat_message_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def _has_saved_chat(
    config: ColdBootConfig, probes: ColdBootProbes, active: _ActivatedSetup,
    expected: UserChatObservation, prompt: str, deadline: float,
) -> bool:
    def get(path: str, **query: str) -> dict[str, object]:
        return _read_panel_contract(config, probes, active.cookie, deadline, path, **query)

    providers = _read_records(get("/api/connections/status"), "providers")
    if not any(
        row.get("provider_instance_id") == expected.provider_id
        and row.get("enabled") is True
        and row.get("credential_status") in ("configured", "not_required")
        for row in providers
    ):
        return False
    models = {
        row["profile_id"] for row in _read_records(get("/api/ai/profiles"), "profiles")
        if isinstance(row.get("profile_id"), str)
        and row.get("model_id") == expected.model_id
        and row.get("provider_id") == expected.provider_id
        and row.get("route_configured") is True
    }
    if not models:
        return False
    for item in _read_records(get("/api/chat/conversations"), "conversations"):
        conversation_id = item.get("id")
        model = item.get("model")
        if (
            not isinstance(conversation_id, str) or not conversation_id
            or not isinstance(model, str) or model not in models
        ):
            continue
        conversation = get("/api/chat/conversation", conversation_id=conversation_id)
        if conversation.get("id") != conversation_id or conversation.get("model") != model:
            continue
        messages = _read_records(conversation, "messages")
        users = {
            msg["id"] for msg in messages if isinstance(msg.get("id"), str)
            and msg.get("role") == "user" and _chat_message_text(msg) == prompt
        }
        replies = {
            msg["id"] for msg in messages if isinstance(msg.get("id"), str)
            and msg.get("role") == "assistant" and _chat_message_text(msg).strip()
        }
        if not users or not replies:
            continue
        for turn in _read_records(get("/api/chat/turns", conversation_id=conversation_id), "turns"):
            reference = turn.get("result_reference")
            if (
                turn.get("profile_id") == active.contract.profile_id
                and turn.get("conversation_id") == conversation_id
                and turn.get("status") == "completed"
                and isinstance(reference, dict)
                and reference.get("conversation_id") == conversation_id
                and isinstance(reference.get("user_message_id"), str)
                and isinstance(reference.get("assistant_message_id"), str)
                and reference["user_message_id"] in users
                and reference["assistant_message_id"] in replies
                and type(reference.get("conversation_revision")) is int
                and type(conversation.get("conversation_revision")) is int
                and 0 < reference["conversation_revision"] <= conversation["conversation_revision"]
            ):
                return True
    return False


def _wait_for_user_chat(
    config: ColdBootConfig, probes: ColdBootProbes, process: Any, active: _ActivatedSetup,
    expected: UserChatObservation,
) -> ColdBootResult:
    deadline = probes.monotonic() + expected.timeout_seconds
    prompt = f"Reply with a short greeting. Tobkiri verification {secrets.token_hex(12)}."
    print("Launch Harness, configure the Provider in the GUI, then select the saved model in a new chat.")
    print(f"Send this message in Harness: {prompt}", flush=True)

    def check_owner() -> None:
        result = active.result
        if (
            process.poll() is not None
            or probes.listener_pid(config.kernel_port) != result.kernel_pid
            or not _is_descendant(result.kernel_pid, result.launcher_pid, probes.parent_pid)
            or _read_host_contract(config) != active.contract
        ):
            raise ColdBootError("the active Launcher-owned Kernel changed while observing chat")

    while probes.monotonic() < deadline:
        check_owner()
        if _has_saved_chat(config, probes, active, expected, prompt, deadline):
            check_owner()
            if probes.monotonic() < deadline:
                return replace(active.result, saved_chat_verified=True)
        probes.sleep(2.0)
    raise ColdBootError(
        "the GUI chat did not complete and persist with the expected saved model"
    )


def verify_cold_boot(
    config: ColdBootConfig,
    *,
    probes: Optional[ColdBootProbes] = None,
    launch: Optional[Callable[[Path, Path, Mapping[str, str]], Any]] = None,
    base_environment: Optional[Mapping[str, str]] = None,
) -> ColdBootResult:
    """Run the bounded, local-only cold-boot gate and return safe evidence."""
    try:
        config, executable = _validate_config(config)
    except BaseException as error:
        _write_preflight_diagnostic(config, error)
        raise

    process: Any = None
    collector: Optional[_OutputCollector] = None
    failure: Optional[BaseException] = None
    broker_port: Optional[int] = None
    try:
        probes = probes or _system_probes()
        launch = launch or _launch_application
        environment = base_environment or os.environ
        if not probes.port_available(config.kernel_port):
            raise ColdBootError("configured Kernel port must be unused before cold boot")
        broker_port = probes.reserve_broker_port(config.kernel_port)
        _validate_port(broker_port, "broker port")
        if broker_port == config.kernel_port:
            raise ColdBootError("broker port must differ from configured Kernel port")
        if not probes.port_available(broker_port):
            raise ColdBootError("reserved broker port became unavailable before cold boot")
        _create_fresh_app_data(config.app_data_dir)
        process = launch(
            executable,
            config.app_bundle,
            _local_only_environment(environment, broker_port, config.app_data_dir),
        )
        collector = _OutputCollector(process)
        result = _wait_for_readiness(config, probes, process, broker_port)
        if config.user_setup_timeout_seconds is not None:
            print("Open Setup in the isolated Launcher and review the confirmation.", flush=True)
            active = _wait_for_user_setup_activation(config, probes, process, result)
            if config.user_chat is not None:
                return _wait_for_user_chat(config, probes, process, active, config.user_chat)
            return active.result
        return result
    except BaseException as error:
        failure = error
        raise
    finally:
        shutdown_failure = None
        if process is not None:
            try:
                if config.expect_setup:
                    assert broker_port is not None
                    _stop_installed_launcher(process, probes, config.kernel_port, broker_port)
                else:
                    _terminate_owned_process_group(process, probes)
            except (ColdBootError, OSError) as error:
                shutdown_failure = error
        diagnostic = failure or shutdown_failure
        if diagnostic is not None:
            _write_failure_diagnostic(
                config,
                broker_port,
                None if process is None else int(process.pid),
                diagnostic,
                # Manual Settings and chat can put private text in application logs.
                collector.text() if collector is not None and config.user_chat is None else "",
            )
        if failure is None and shutdown_failure is not None:
            raise shutdown_failure


def parse_args() -> argparse.Namespace:
    """Parse the explicitly bounded cold-boot verifier inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-bundle", required=True, type=Path)
    parser.add_argument("--app-data-dir", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    parser.add_argument("--kernel-port", default=DEFAULT_KERNEL_PORT, type=int)
    parser.add_argument("--timeout-seconds", default=180.0, type=float)
    parser.add_argument("--expect-setup", action="store_true")
    parser.add_argument("--expect-packvm-disabled", action="store_true")
    parser.add_argument("--wait-for-user-setup-seconds", type=float)
    parser.add_argument(
        "--wait-for-user-chat", nargs=3, metavar=("SECONDS", "PROVIDER_ID", "MODEL_ID"),
        help="observe a message sent in the installed GUI after user-confirmed Setup",
    )
    return parser.parse_args()


def main() -> int:
    """Execute the verifier without printing app output or sensitive state."""
    args = parse_args()
    user_chat = None
    if args.wait_for_user_chat is not None:
        seconds, provider_id, model_id = args.wait_for_user_chat
        try:
            user_chat = UserChatObservation(float(seconds), provider_id, model_id)
        except ValueError:
            raise ColdBootError("user chat timeout must be numeric") from None
    result = verify_cold_boot(
        ColdBootConfig(
            app_bundle=args.app_bundle,
            app_data_dir=args.app_data_dir,
            diagnostics_dir=args.diagnostics_dir,
            kernel_port=args.kernel_port,
            timeout_seconds=args.timeout_seconds,
            expect_setup=args.expect_setup,
            user_setup_timeout_seconds=args.wait_for_user_setup_seconds,
            user_chat=user_chat,
            expect_packvm_disabled=args.expect_packvm_disabled,
        )
    )
    if result.setup_panel_reachable:
        print("Verified authenticated fresh Setup entry from the installed app")
    if args.expect_packvm_disabled:
        print("Verified authenticated PackVM-disabled status from the installed Kernel")
    if result.setup_activation_verified:
        print("Verified the confirmed Profile on the restarted Launcher-owned Kernel")
    if result.saved_chat_verified:
        print("Verified a completed GUI chat saved with the expected model")
    print(
        "Verified local macOS CI/E2E cold boot "
        f"(broker={result.broker_port}, kernel={result.kernel_port}, "
        f"panel_reachable={result.panel_reachable})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
