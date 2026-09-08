"""Real subprocess evidence for bounded guest pipe draining and cleanup."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from tobkiri_host.bounded_child_io import communicate_bounded
from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner as runner,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="guest uses POSIX pipe selectors")


@contextmanager
def _child(source: str) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", source],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_bidirectional_pipe_pressure_drains_stderr_and_preserves_exact_stdout() -> None:
    source = (
        "import sys; sys.stderr.buffer.write(b'e'*200000); sys.stderr.flush(); "
        "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"
    )
    payload = b"x" * 200000
    with _child(source) as process:
        assert communicate_bounded(process, payload, stdout_limit=len(payload),
                                   stderr_limit=200000, timeout=5) == payload
        assert process.returncode == 0
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
def test_runner_stops_and_reaps_flooding_child_without_retaining_diagnostics(
    monkeypatch: pytest.MonkeyPatch, pipe: str,
) -> None:
    monkeypatch.setattr(runner, "MAX_RESULT_BYTES", 4096)
    monkeypatch.setattr(runner, "MAX_CHILD_STDERR_BYTES", 4096)
    source = f"import os\nwhile True: os.write({1 if pipe == 'stdout' else 2}, b'private-diagnostic'*1000)"
    with _child(source) as process:
        stopped = _local_stop(monkeypatch, process)
        with pytest.raises(ValueError, match="output exceeds size limit") as error:
            runner._communicate_staged_implementation(process, {})
        assert "private-diagnostic" not in str(error.value)
        assert process.poll() is not None
        assert stopped == [process.pid]
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("source", [
    "import time; time.sleep(30)",
    "import os,time; os.close(0); os.close(1); os.close(2); time.sleep(30)",
])
def test_timeout_covers_both_open_pipes_and_child_wait_after_eof(source: str) -> None:
    with _child(source) as process:
        with pytest.raises(TimeoutError, match="timed out"):
            communicate_bounded(process, b"", stdout_limit=10, stderr_limit=10, timeout=0.1)
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_runner_accepts_exact_json_and_redacts_failed_child_stderr() -> None:
    with _child("import sys; sys.stdin.buffer.read(); print('{\"answer\":42}')") as process:
        assert runner._communicate_staged_implementation(process, {}) == {"answer": 42}
    with _child("import sys; sys.stderr.write('secret'); sys.exit(1)") as process:
        with pytest.raises(ValueError, match="implementation failed") as error:
            runner._communicate_staged_implementation(process, {})
        assert "secret" not in str(error.value)


def test_absolute_deadline_is_not_renewed_by_a_fresh_relative_timeout() -> None:
    with _child("import time; time.sleep(30)") as process:
        with pytest.raises(TimeoutError, match="timed out"):
            communicate_bounded(process, b"input", stdout_limit=10, stderr_limit=10,
                                timeout=60, deadline=time.monotonic() - 1)
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("encoded", [
    b'{"private-key":1,"private-key":2}',
    b'{"nested":{"private-key":1,"private-key":2}}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":1.5}',
    b'{"value":9007199254740992}',
    b'{"value":"\\ud800"}',
    b'{"value":"\xff"}',
    b'{"value":' + b'[' * 70 + b'0' + b']' * 70 + b'}',
    b'{"value":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
])
def test_child_result_is_strict_before_normalization_and_redacts_parser_errors(
    encoded: bytes,
) -> None:
    """Use actual pipe bytes; a dict fixture would already lose duplicate keys."""
    source = f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({encoded!r})"
    with _child(source) as process:
        with pytest.raises(ValueError) as error:
            runner._communicate_staged_implementation(process, {})
        assert str(error.value) == "PackVM implementation result is invalid"
        assert process.returncode == 0
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_runner_reaps_child_even_when_serialization_fails_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _child("import time; time.sleep(30)") as process:
        stopped = _local_stop(monkeypatch, process)
        with pytest.raises((TypeError, ValueError)):
            runner._communicate_staged_implementation(process, {"invalid": object()})
        assert process.poll() is not None
        assert stopped == [process.pid]


def _local_stop(monkeypatch: pytest.MonkeyPatch, process: subprocess.Popen[bytes]) -> list[int]:
    # Real pipe I/O and child reaping; Linux guest process-group policy is a
    # separate boundary, not something this macOS/Windows Host test emulates.
    stopped = []

    def stop(pid: int) -> list[str]:
        assert pid == process.pid
        stopped.append(pid)
        process.kill()
        return ["KILL"]

    monkeypatch.setattr(runner, "_terminate_process_group", stop)
    return stopped


def test_failed_termination_still_closes_pipes_without_claiming_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(pid: int) -> None:
        raise PermissionError("termination denied")

    monkeypatch.setattr(runner, "_terminate_process_group", denied)
    with _child("import time; time.sleep(30)") as process:
        with pytest.raises(PermissionError, match="termination denied"):
            runner._communicate_staged_implementation(process, {"invalid": object()})
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("resuming", [False, True])
@pytest.mark.parametrize("failure", [OSError, SystemExit])
def test_registration_failure_stops_child_without_unbounded_pipe_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    resuming: bool, failure: type[BaseException],
) -> None:
    """Both execution entries own cleanup even before request registration."""
    digest = "sha256:" + "a" * 64
    request = {
        "operation": "invoke", "request_id": "registration-failure",
        "target_domain": "test-domain", "artifact_digest": digest,
        "materialization_digest": digest, "guest_artifact_identity": digest,
        "contract_id": "test.contract", "contract_version": "1.0.0",
        "operation_id": "test.operation", "payload": {},
        "request_digest": digest, "deadline_monotonic": 100.0,
        "cancel_token": "b" * 64,
    }
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_verify_invocation_artifact", lambda request: digest)
    monkeypatch.setattr(runner, "_load_manifest", lambda target: {
        "implementation_path": "runtime/test.py",
    })

    def reject_registration(*args: object) -> None:
        raise failure("registration interrupted")

    def no_drain(*args: object, **kwargs: object) -> None:
        pytest.fail("registration cleanup must never call communicate")

    monkeypatch.setattr(runner, "_register_request", reject_registration)
    with _child("import os\nwhile True: os.write(2, b'private'*1000)") as process:
        stopped = _local_stop(monkeypatch, process)
        monkeypatch.setattr(runner, "_spawn_staged_implementation", lambda *args: process)
        monkeypatch.setattr(process, "communicate", no_drain)
        with pytest.raises(failure, match="registration interrupted"):
            if resuming:
                runner._resume_bridge_invocation(request, {"continuation": {}}, {})
            else:
                runner._invoke(request)
        assert stopped == [process.pid]
        assert process.returncode is not None
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
