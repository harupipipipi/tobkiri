"""Bounded CI controller for the actual, isolated macOS debug Tauri app.

Tauri's invalid-key diagnostics include its real invoke key. Native output
therefore stays in bounded memory, never the console or an uploaded log.
Only a validated closed observation and non-secret build identities survive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import selectors
import signal
import subprocess
import sys
import time

PREFIX = b"TOBKIRI_NATIVE_IPC_RESULT "
BAD_KEY = b"__TAURI_INVOKE_KEY__ expected "
MAX_CAPTURE = 2 * 1024 * 1024
MAX_RECEIPT = 4096
COMMANDS = 11
ROOT = Path(__file__).resolve().parents[2]
FORMAT = "tobkiri.native-ipc-controller/1"
UNTESTED = (
    "native_approval_ui_tested", "configuration_api_tested", "full_product_gui_tested",
)


class InvalidObservation(Exception):
    """Closed failure with no source text or captured native output."""


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON members instead of choosing an apparent result."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidObservation("schema")
        result[key] = value
    return result


def exact_keys(value: object, names: set[str]) -> bool:
    """Require a closed object, including on failure paths."""
    return type(value) is dict and set(value) == names


def evaluate(
    stdout: bytes, stderr: bytes, returncode: int, controller_failure: str | None,
) -> dict[str, object]:
    """Validate positive controls and independently observed raw-key rejections."""
    result: dict[str, object] = {
        "format": FORMAT, "passed": False, "failure": "missing-result",
        "native_caller_entries": None, "raw_native_rejections": 0,
        "observation": None, **{name: False for name in UNTESTED},
    }
    if len(stdout) > MAX_CAPTURE or len(stderr) > MAX_CAPTURE:
        result["failure"] = "capture-limit"
        return result
    # Never copy or decode these lines: they contain the expected invoke key.
    rejected = sum(line.startswith(BAD_KEY) for line in stderr.splitlines())
    result["raw_native_rejections"] = rejected
    if controller_failure is not None:
        if controller_failure not in {"timeout", "capture-limit", "spawn", "capture"}:
            controller_failure = "controller"
        result["failure"] = controller_failure
        return result
    if returncode != 0:
        result["failure"] = "process-exit"
        return result
    if any(marker in stderr for marker in (
        b"Core shutdown failed", b"Core shutdown budget reached or worker lost",
    )):
        result["failure"] = "shutdown"
        return result
    candidates = [line[len(PREFIX):] for line in stdout.splitlines()
                  if line.startswith(PREFIX)]
    if not candidates:
        return result
    if len(candidates) != 1 or len(candidates[0]) > MAX_RECEIPT:
        result["failure"] = "result-count-or-size"
        return result
    try:
        receipt = json.loads(candidates[0], object_pairs_hook=unique_object)
        if not exact_keys(receipt, {
            "format", "passed", "native_caller_entries", "report", *UNTESTED,
        }):
            raise InvalidObservation("schema")
        if (receipt["format"] != "tobkiri.native-ipc-observation/1"
                or type(receipt["passed"]) is not bool
                or type(receipt["native_caller_entries"]) is not int
                or not 0 <= receipt["native_caller_entries"] <= 1000
                or any(receipt[name] is not False for name in UNTESTED)):
            raise InvalidObservation("schema")
        report = receipt["report"]
        booleans = {
            "root_start", "iframe_loaded", "root_stop", "parent_dom_blocked",
            "parent_internals_blocked", "storage_blocked", "sdk_exposed",
        }
        integers = {
            "version", "sdk_attempts", "sdk_rejected", "raw_surfaces",
            "raw_sent", "command_count",
        }
        if (not exact_keys(report, booleans | integers | {"failure"})
                or any(type(report[name]) is not bool for name in booleans)
                or any(type(report[name]) is not int
                       or not 0 <= report[name] <= 1000 for name in integers)
                or report["failure"] not in {
                    "none", "root-start", "child-report", "stop", "timeout", "schema",
                }):
            raise InvalidObservation("schema")
        # Reconstruct instead of republishing arbitrary native or renderer data.
        result["observation"] = {name: report[name] for name in sorted(report)}
        result["native_caller_entries"] = receipt["native_caller_entries"]
        completed = (
            receipt["passed"] is True and receipt["native_caller_entries"] == 2
            and report["version"] == 1 and report["command_count"] == COMMANDS
            and report["failure"] == "none"
            and all(report[name] for name in booleans - {"sdk_exposed"})
            and report["sdk_attempts"] == (COMMANDS if report["sdk_exposed"] else 0)
            and report["sdk_rejected"] == report["sdk_attempts"]
            and 1 <= report["raw_surfaces"] <= 2
            and report["raw_sent"] == report["raw_surfaces"] * COMMANDS
        )
        if not completed:
            result["failure"] = "incomplete-observation"
        elif rejected != report["raw_sent"]:
            result["failure"] = "unconfirmed-raw-rejection"
        else:
            result.update(passed=True, failure="none")
    except (ValueError, TypeError, KeyError, InvalidObservation):
        result["failure"] = "schema"
    return result


def capture(
    command: list[str], environment: dict[str, str], timeout: float = 60,
    limit: int = MAX_CAPTURE,
) -> tuple[bytes, bytes, int, str | None]:
    """Drain both pipes without deadlock, bounding wall time and retained bytes."""
    buffers = [bytearray(), bytearray()]
    failure = None
    try:
        child = subprocess.Popen(
            command, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError:
        return b"", b"", -1, "spawn"
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((child.stdout, child.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    failure = "timeout"
                    break
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    if len(buffer) + len(chunk) > limit:
                        failure = "capture-limit"
                        break
                    buffer.extend(chunk)
                if failure:
                    break
    except (OSError, ValueError):
        failure = "capture"
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                child.wait()
        for stream in (child.stdout, child.stderr):
            stream.close()
    return bytes(buffers[0]), bytes(buffers[1]), child.returncode, failure


def sha256(path: Path) -> str:
    """Stream file identities without loading a native binary into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def execute(app: Path, fixture: Path, head: str) -> dict[str, object]:
    """Run only on the pinned CI platform with a complete private debug scope."""
    temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    fixture = fixture.resolve(strict=True)
    if (os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Darwin"
            or platform.machine() != "arm64" or platform.release() != "24.6.0"
            or len(head) != 40 or any(char not in "0123456789abcdef" for char in head)
            or not fixture.is_relative_to(temp) or fixture.stat().st_mode & 0o077):
        raise InvalidObservation("scope")
    binary = app.resolve(strict=True) / "Contents/MacOS/tobkiri-launcher"
    data = (fixture / "data").resolve(strict=True)
    if not data.is_relative_to(fixture) or data.stat().st_mode & 0o077:
        raise InvalidObservation("scope")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("TOBKIRI_")}
    environment.update(
        TOBKIRI_CORE_DEBUG_ROOT=str(data),
        TOBKIRI_CORE_DEBUG_NONCE=secrets.token_hex(12),
        TOBKIRI_CORE_DEBUG_PROFILE="prof_native_ipc_probe",
        TOBKIRI_CORE_IPC_PROBE="1",
    )
    paths = [
        Path(__file__).relative_to(ROOT),
        Path("tools/native-ipc-probe/root.js"),
        Path("tools/native-ipc-probe/attack.js"),
        Path("tools/native-ipc-probe/index.html"),
        Path("crates/tobkiri-e2e/examples/native_ipc_fixture.rs"),
        Path("tobkiri_launcher/src-tauri/src/native_ipc_probe.rs"),
        Path("tobkiri_launcher/src-tauri/src/core_host.rs"),
        Path("tobkiri_launcher/src-tauri/tauri.core-host.conf.json"),
        Path("tobkiri_launcher/src-tauri/tauri.core-host.macos.dev.conf.json"),
        Path("tobkiri_launcher/src-tauri/capabilities/core-host.json"),
        Path("tobkiri_launcher/src-tauri/Cargo.lock"),
        Path("tobkiri_launcher/frontend/package-lock.json"),
    ]
    identities = {path.as_posix(): sha256(ROOT / path) for path in paths}
    identities["native-binary"] = sha256(binary)
    identities["fixture-pack"] = sha256(fixture / "pack/native-ipc-probe.tpk")
    result = evaluate(*capture([str(binary)], environment))
    result.update(
        source_head=head, identities=identities, platform="macos-arm64-darwin24.6",
        github_run_id=os.environ.get("GITHUB_RUN_ID"),
        github_run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
    )
    return result


def main() -> int:
    """Write only closed metadata. Failure never echoes a native exception/log."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = execute(args.app, args.fixture, args.head)
    except (OSError, ValueError, KeyError, InvalidObservation):
        result = {"format": FORMAT, "passed": False, "failure": "preflight"}
    payload = json.dumps(result, sort_keys=True, indent=2) + "\n"
    # The upload target contains metadata only, never either native pipe.
    with args.receipt.open("x", encoding="utf-8") as output:
        output.write(payload)
    print(json.dumps({"passed": result["passed"], "failure": result["failure"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
