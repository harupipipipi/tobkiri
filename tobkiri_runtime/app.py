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


def prepare_for_sealed_dispatch(scope: object) -> None:
    """Capture the Launcher-issued PackVM bundle identity before startup."""

    from core_runtime.packaged_application_bundle import (
        install_packvm_bundle_binding_from_sealed_scope,
    )

    install_packvm_bundle_binding_from_sealed_scope(scope, __file__)


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
        help="Capture the canonical Defaults activation and print its state",
    )
    return parser


def _create_defaultspack_kernel() -> Any:
    """Lazily construct the sole Pack v4 Host composition root."""

    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        create_defaultspack_kernel,
    )

    return create_defaultspack_kernel()


def _clear_restart_request() -> None:
    """Discard a test/process-local restart request before a fresh boot."""

    from core_runtime.restart_control import clear_kernel_restart_request

    clear_kernel_restart_request()


def _restart_requested() -> bool:
    """Return whether the post-response activation handoff requested a restart."""

    from core_runtime.restart_control import is_kernel_restart_requested

    return is_kernel_restart_requested()


def main(argv: Sequence[str] | None = None) -> int:
    """Start only the canonical v4 Host and wait for process termination."""
    args = _parser().parse_args(argv)
    kernel = _create_defaultspack_kernel()
    stop = threading.Event()
    try:
        _clear_restart_request()
        result = kernel.run_startup()
        if args.health:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
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
