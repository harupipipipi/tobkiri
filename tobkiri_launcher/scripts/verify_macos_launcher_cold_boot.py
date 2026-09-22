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
import http.client
import json
import os
import plistlib
import re
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


CI_BUNDLE_IDENTIFIER = "dev.tobkiri.launcher.ci-e2e"
CI_APP_DATA_DIRECTORY_NAME = CI_BUNDLE_IDENTIFIER
CI_APP_NAME = "Tobkiri Launcher CI E2E.app"
CI_EXECUTABLE_NAME = "tobkiri-launcher"
EXECUTABLE_DIRECTORY_RELATIVE = Path("Contents/MacOS")
INFO_PLIST_RELATIVE = Path("Contents/Info.plist")
BROKER_CONNECTION_RELATIVE = Path("user_data/host_broker/connection.json")
BROKER_HEALTH_PATH = "/api/host/health"
KERNEL_HEALTH_PATH = "/health"
PANEL_BOOTSTRAP_PATH = "/panel/"
DEFAULT_KERNEL_PORT = 8765
POLL_INTERVAL_SECONDS = 0.2
HTTP_TIMEOUT_SECONDS = 0.8
TERMINATION_GRACE_SECONDS = 5.0
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


@dataclass(frozen=True)
class ColdBootResult:
    """Non-sensitive evidence emitted after a successful cold boot."""

    broker_port: int
    kernel_port: int
    launcher_pid: int
    kernel_pid: int
    panel_reachable: bool


@dataclass(frozen=True)
class ColdBootProbes:
    """Injectable OS and loopback probes for deterministic unit testing."""

    port_available: Callable[[int], bool]
    reserve_broker_port: Callable[[int], int]
    http_get: Callable[[int, str], Optional[HttpResponse]]
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
    _canonical_directory(app_data_dir.parent, "application-data parent directory")
    if app_data_dir.exists() or app_data_dir.is_symlink():
        raise ColdBootError("CI/E2E application-data directory must be fresh")
    return app_data_dir


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
    return (
        ColdBootConfig(
            app_bundle=app_bundle,
            app_data_dir=app_data_dir,
            diagnostics_dir=diagnostics_dir,
            kernel_port=kernel_port,
            timeout_seconds=config.timeout_seconds,
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


def _http_get(port: int, path: str) -> Optional[HttpResponse]:
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            port,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(128 * 1024)
        headers = {key.lower(): value for key, value in response.getheaders()}
        return HttpResponse(response.status, headers, body)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


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
        http_get=_http_get,
        listener_pid=_listener_pid,
        parent_pid=_parent_pid,
        monotonic=time.monotonic,
        sleep=time.sleep,
        kill_process_group=os.killpg,
    )


def _local_only_environment(
    base_environment: Mapping[str, str],
    broker_port: int,
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


def _kernel_is_healthy(response: Optional[HttpResponse]) -> bool:
    if response is None or response.status != 200:
        return False
    document = response.json_object()
    if not isinstance(document, dict) or document.get("success") is not True:
        return False
    payload = document.get("data")
    return not isinstance(payload, dict) or payload.get("panel_ready") is not False


def _panel_bootstrap_is_reachable(response: Optional[HttpResponse]) -> bool:
    if response is None or response.status != 200:
        return False
    content_type = response.headers.get("content-type", "").lower()
    cache_control = response.headers.get("cache-control", "").lower()
    body = response.body.lower()
    return (
        "text/html" in content_type
        and "no-store" in cache_control
        and b"<!doctype html>" in body
        and b"/api/panel/auth/exchange" in body
        and b"tobkiri launcher authentication required" in body
    )


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
    connection_path = config.app_data_dir / BROKER_CONNECTION_RELATIVE

    while probes.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise ColdBootError(
                f"CI/E2E Launcher exited before readiness (status {exit_code})"
            )

        if not broker_ready:
            broker_response = probes.http_get(broker_port, BROKER_HEALTH_PATH)
            if _broker_is_ready(broker_response) and _broker_connection_is_embedded(
                connection_path,
                broker_port,
                int(process.pid),
            ):
                broker_ready = True

        if broker_ready:
            kernel_response = probes.http_get(config.kernel_port, KERNEL_HEALTH_PATH)
            if _kernel_is_healthy(kernel_response):
                kernel_pid = probes.listener_pid(config.kernel_port)
                if kernel_pid is not None and _is_descendant(
                    kernel_pid,
                    int(process.pid),
                    probes.parent_pid,
                ):
                    panel_reachable = _panel_bootstrap_is_reachable(
                        probes.http_get(config.kernel_port, PANEL_BOOTSTRAP_PATH)
                    )
                    if panel_reachable:
                        return ColdBootResult(
                            broker_port=broker_port,
                            kernel_port=config.kernel_port,
                            launcher_pid=int(process.pid),
                            kernel_pid=kernel_pid,
                            panel_reachable=True,
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
        "owned Kernel health and panel bootstrap did not become ready before timeout"
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
        process = launch(
            executable,
            config.app_bundle,
            _local_only_environment(environment, broker_port),
        )
        collector = _OutputCollector(process)
        return _wait_for_readiness(config, probes, process, broker_port)
    except BaseException as error:
        failure = error
        raise
    finally:
        if process is not None:
            _terminate_owned_process_group(process, probes)
        if failure is not None:
            _write_failure_diagnostic(
                config,
                broker_port,
                None if process is None else int(process.pid),
                failure,
                "" if collector is None else collector.text(),
            )


def parse_args() -> argparse.Namespace:
    """Parse the explicitly bounded cold-boot verifier inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-bundle", required=True, type=Path)
    parser.add_argument("--app-data-dir", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    parser.add_argument("--kernel-port", default=DEFAULT_KERNEL_PORT, type=int)
    parser.add_argument("--timeout-seconds", default=180.0, type=float)
    return parser.parse_args()


def main() -> int:
    """Execute the verifier without printing app output or sensitive state."""
    args = parse_args()
    result = verify_cold_boot(
        ColdBootConfig(
            app_bundle=args.app_bundle,
            app_data_dir=args.app_data_dir,
            diagnostics_dir=args.diagnostics_dir,
            kernel_port=args.kernel_port,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(
        "Verified local macOS CI/E2E cold boot "
        f"(broker={result.broker_port}, kernel={result.kernel_port}, "
        f"panel_reachable={result.panel_reachable})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
