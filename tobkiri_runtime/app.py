#!/usr/bin/env python3
"""Canonical process entrypoint for the Tobkiri Pack v4 Host.

Production startup has one composition root.  This module deliberately has no
Pack discovery, legacy manifest/Profile loading, host-execution environment
switch, permissive mode, Registry, managed-Python, or direct Pack launch path.
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Sequence
from typing import Any

_SEALED_NATIVE_FIRST_SETUP: bool | None = None


def prepare_for_sealed_dispatch(scope: object) -> None:
    """Capture the Launcher-issued bundle identity and Setup policy."""

    from core_runtime.packaged_application_bundle import (
        install_packvm_bundle_binding_from_sealed_scope,
    )

    global _SEALED_NATIVE_FIRST_SETUP
    native_first_setup_for = getattr(scope, "native_first_setup_for", None)
    if not callable(native_first_setup_for):
        raise RuntimeError("sealed dispatch scope lacks the Kernel Setup policy")
    native_first_setup = native_first_setup_for(__file__)
    if not isinstance(native_first_setup, bool):
        raise RuntimeError("sealed Kernel Setup policy is invalid")
    if (
        _SEALED_NATIVE_FIRST_SETUP is not None
        and _SEALED_NATIVE_FIRST_SETUP != native_first_setup
    ):
        raise RuntimeError("sealed Kernel Setup policy was already established")
    install_packvm_bundle_binding_from_sealed_scope(scope, __file__)
    _SEALED_NATIVE_FIRST_SETUP = native_first_setup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tobkiri Pack v4 Host")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Capture and validate the Host, then return without waiting",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Probe the running Host's /health endpoint and print its state",
    )
    return parser


def _create_defaultspack_kernel() -> Any:
    """Lazily construct the sole Pack v4 Host composition root."""

    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        create_defaultspack_kernel,
    )

    return create_defaultspack_kernel(
        native_first_setup=_SEALED_NATIVE_FIRST_SETUP is True,
    )


def _clear_restart_request() -> None:
    """Discard a test/process-local restart request before a fresh boot."""

    from core_runtime.restart_control import clear_kernel_restart_request

    clear_kernel_restart_request()


def _restart_requested() -> bool:
    """Return whether the post-response activation handoff requested a restart."""

    from core_runtime.restart_control import is_kernel_restart_requested

    return is_kernel_restart_requested()


def _probe_health_endpoint(port: int, *, timeout: float = 5.0) -> dict[str, Any]:
    """Read ``GET /health`` from an already-running Host.

    This is a plain HTTP client call, so a health check never binds the
    runtime listen socket and never constructs the kernel.
    """
    from urllib.error import HTTPError
    from urllib.request import urlopen

    url = f"http://127.0.0.1:{port}/health"
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {
            "status": "error",
            "runtime_ready": False,
            "port": port,
            "error": f"{url} returned HTTP {exc.code}",
        }
    except OSError as exc:
        return {
            "status": "down",
            "runtime_ready": False,
            "port": port,
            "error": f"no Tobkiri Host listening on {url} ({exc})",
        }
    except ValueError as exc:
        return {
            "status": "error",
            "runtime_ready": False,
            "port": port,
            "error": f"{url} returned an invalid payload ({exc})",
        }
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return {
            "status": "error",
            "runtime_ready": False,
            "port": port,
            "error": f"{url} returned an unexpected payload",
        }
    data = payload.get("data")
    result: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
    result.setdefault("status", "ok")
    result["port"] = port
    return result


def _run_health_probe() -> int:
    """Print the running Host's health without taking its listen socket."""
    from core_runtime.runtime_port import resolve_runtime_port

    result = _probe_health_endpoint(resolve_runtime_port())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Start only the canonical v4 Host and wait for process termination."""
    args = _parser().parse_args(argv)
    if args.health:
        return _run_health_probe()
    kernel = _create_defaultspack_kernel()
    stop = threading.Event()
    try:
        _clear_restart_request()
        kernel.run_startup()
        if args.headless:
            return 0

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, request_stop)
        while not stop.wait(0.1):
            if _restart_requested():
                # The Launcher treats this as a bounded, intentional cold
                # recapture boundary, not as an application crash.
                return 42
        return 42 if _restart_requested() else 0
    finally:
        kernel.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
