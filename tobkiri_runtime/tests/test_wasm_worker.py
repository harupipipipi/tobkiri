"""Real process lifecycle tests, independent of optional Wasmtime installation."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

import pytest

from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.wasm_worker import ComponentWorker

_REAL_POPEN = subprocess.Popen


def worker(script: str) -> ComponentWorker:
    """Use explicit test-owned child code, not guest-controlled launch settings."""
    return ComponentWorker((sys.executable, "-I", "-B", "-c", script))


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen]]:
    """Record actual handles and clean up even when a lifecycle assertion fails."""
    original = subprocess.Popen
    processes: list[subprocess.Popen] = []

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("tobkiri_host.wasm_worker.subprocess.Popen", launch)
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        original.wait(process, timeout=5)


def test_success_is_reaped_and_cannot_be_reused(children: list) -> None:
    owned = worker(
        'import sys; sys.stdin.buffer.read(); print(\'{"status":"ok","data":{"count":1}}\')'
    )
    assert owned.invoke({}, cancelled=threading.Event()) == {"count": 1}
    assert len(children) == 1 and children[0].returncode == 0
    assert children[0].stdout.closed
    assert owned._process is None
    with pytest.raises(ProviderExecutionError, match="consumed"):
        owned.invoke({}, cancelled=threading.Event())
    assert len(children) == 1


def test_deadline_covers_blocked_input_and_reaps_child(children: list) -> None:
    owned = worker("import time; time.sleep(60)")
    with pytest.raises(ProviderExecutionError, match="deadline"):
        owned.invoke({"input": "x" * (1024 * 1024)}, cancelled=threading.Event(), timeout=0.2)
    assert len(children) == 1 and children[0].returncode is not None
    assert owned._process is None


def test_precancel_never_starts_process(children: list) -> None:
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProviderExecutionError, match="cancelled"):
        worker("raise AssertionError('must not start')").invoke({}, cancelled=cancelled)
    assert children == []


def test_cancel_reaps_live_child(children: list) -> None:
    cancelled = threading.Event()
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    try:
        with pytest.raises(ProviderExecutionError, match="cancelled"):
            worker("import time; time.sleep(60)").invoke({}, cancelled=cancelled)
    finally:
        timer.cancel()
        timer.join()
    assert len(children) == 1 and children[0].returncode is not None


def test_output_flood_is_bounded_and_child_reaped(children: list) -> None:
    owned = worker(
        "import os, sys; sys.stdin.buffer.read(); "
        "chunk = b'x' * 65536\nwhile True: os.write(1, chunk)"
    )
    with pytest.raises(ProviderExecutionError, match="output exceeds"):
        owned.invoke({}, cancelled=threading.Event())
    assert children[0].returncode is not None
    assert children[0].stdout.closed


def test_exit_failure_is_not_accepted_as_success(children: list) -> None:
    with pytest.raises(ProviderExecutionError, match="unsuccessfully"):
        worker("import sys; sys.stdin.buffer.read(); sys.exit(7)").invoke(
            {}, cancelled=threading.Event()
        )
    assert children[0].returncode == 7


def test_invalid_response_is_rejected_after_reaping(children: list) -> None:
    with pytest.raises(ProviderExecutionError, match="response is invalid"):
        worker("import sys; sys.stdin.buffer.read(); print('not-json')").invoke(
            {}, cancelled=threading.Event()
        )
    assert children[0].returncode == 0


def test_failed_exit_confirmation_retains_handle_for_retry(
    children: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = worker("import time; time.sleep(60)")
    original_wait = _REAL_POPEN.wait

    def unconfirmed(self, timeout=None):
        raise subprocess.TimeoutExpired("test-owned worker", timeout)

    original_exchange = owned._exchange

    def exchange(*args):
        monkeypatch.setattr(owned._process, "wait", unconfirmed.__get__(owned._process))
        return original_exchange(*args)

    monkeypatch.setattr(owned, "_exchange", exchange)
    with pytest.raises(ProviderExecutionError, match="termination is unconfirmed"):
        owned.invoke({}, cancelled=threading.Event(), timeout=0.2)
    assert owned._process is children[0]
    assert not children[0].stdout.closed
    monkeypatch.setattr(children[0], "wait", original_wait.__get__(children[0]))
    owned.close()
    assert owned._process is None
    assert children[0].returncode is not None


def test_environment_and_inheritable_descriptors_are_not_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOBKIRI_WORKER_TEST_SECRET", "never-forward")
    with (tmp_path / "private").open("wb") as private:
        os.set_inheritable(private.fileno(), True)
        script = (
            "import json, os, sys; sys.stdin.buffer.read()\n"
            f"try: os.fstat({private.fileno()}); inherited = True\n"
            "except OSError: inherited = False\n"
            "print(json.dumps({'status': 'ok', 'data': {"
            "'secret': os.getenv('TOBKIRI_WORKER_TEST_SECRET'), "
            "'inherited': inherited, 'cwd': os.getcwd()}}))"
        )
        assert worker(script).invoke({}, cancelled=threading.Event()) == {
            "secret": None,
            "inherited": False,
            "cwd": "/",
        }


@pytest.mark.parametrize("timeout", [True, 0, -1, 61, float("nan"), float("inf")])
def test_invalid_deadline_does_not_start(timeout: float, children: list) -> None:
    with pytest.raises(ValueError, match="deadline"):
        worker("pass").invoke({}, cancelled=threading.Event(), timeout=timeout)
    assert children == []
