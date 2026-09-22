#!/usr/bin/env python3
"""Emit deterministic pytest, Vitest, and cargo-like output for runner tests."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def _emit(stream: object, value: str) -> None:
    print(value, file=stream, flush=True)


def _success(framework: str) -> None:
    if framework == "pytest":
        _emit(
            sys.stdout,
            "============================= test session starts ==============================",
        )
        _emit(sys.stderr, "fixture.py::test_order PASSED")
        _emit(sys.stdout, "1 passed in 0.01s")
    elif framework == "vitest":
        _emit(sys.stdout, " RUN  v3.2.4 /fixture")
        _emit(sys.stderr, " ✓ fixture.test.ts (1 test)")
        _emit(sys.stdout, " Test Files  1 passed (1)")
        _emit(sys.stdout, "      Tests  1 passed (1)")
    else:
        _emit(sys.stdout, "running 1 test")
        _emit(sys.stderr, "test fixture_order ... ok")
        _emit(sys.stdout, "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured")


def _failure(framework: str, message: str | None) -> int:
    for index in range(7_000):
        stream = sys.stdout if index % 2 == 0 else sys.stderr
        _emit(stream, f"PASS-NOISE-{index:04d}")
    if framework == "pytest":
        _emit(
            sys.stdout,
            "=================================== FAILURES ===================================",
        )
        _emit(sys.stderr, "Traceback (most recent call last):")
        if message:
            _emit(sys.stderr, message)
        _emit(sys.stderr, '  File "fixture.py", line 7, in test_failure')
        _emit(sys.stderr, '    raise AssertionError("fixture boom")')
        _emit(sys.stderr, "AssertionError: fixture boom")
        _emit(
            sys.stdout,
            "=========================== 1 failed, 9 passed in 0.02s ========================",
        )
        return 1
    if framework == "vitest":
        _emit(sys.stdout, " FAIL  fixture.test.ts > suite > rejects")
        if message:
            _emit(sys.stderr, message)
        _emit(sys.stderr, "Error: expected 2 to be 3")
        _emit(sys.stderr, " ❯ fixture.test.ts:8:17")
        _emit(sys.stdout, " Test Files  1 failed (1)")
        _emit(sys.stdout, "      Tests  1 failed | 9 passed (10)")
        return 2
    _emit(sys.stdout, "---- fixture_failure stdout ----")
    if message:
        _emit(sys.stderr, message)
    _emit(sys.stderr, "thread 'fixture_failure' panicked at fixture.rs:9:5:")
    _emit(sys.stderr, "fixture boom")
    _emit(sys.stdout, "test result: FAILED. 9 passed; 1 failed; 0 ignored; 0 measured")
    return 101


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--framework", choices=("pytest", "vitest", "cargo"), required=True
    )
    parser.add_argument("--outcome", choices=("success", "failure"), default="success")
    parser.add_argument("--unicode", action="store_true")
    parser.add_argument("--signal", choices=("TERM",))
    parser.add_argument("--sleep", type=float, default=0)
    parser.add_argument("--message")
    arguments = parser.parse_args()
    _emit(sys.stdout, "ORDER-stdout-1")
    _emit(sys.stderr, "ORDER-stderr-2")
    _emit(sys.stdout, "ORDER-stdout-3")
    if arguments.unicode:
        _emit(sys.stderr, "Unicode: 日本語 🐦 café")
    if arguments.message and arguments.outcome != "failure":
        _emit(sys.stderr, arguments.message)
    if arguments.sleep:
        time.sleep(arguments.sleep)
    if arguments.signal == "TERM":
        _emit(sys.stderr, "fixture requests SIGTERM")
        os.kill(os.getpid(), signal.SIGTERM)
    if arguments.outcome == "failure":
        return _failure(arguments.framework, arguments.message)
    _success(arguments.framework)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
