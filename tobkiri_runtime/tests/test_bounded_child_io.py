"""Real subprocess evidence for bounded guest pipe draining and cleanup."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
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
        with pytest.raises(ValueError, match="output exceeds size limit") as error:
            runner._communicate_staged_implementation(process, {})
        assert "private-diagnostic" not in str(error.value)
        assert process.poll() is not None
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


def test_runner_reaps_child_even_when_serialization_fails_before_io() -> None:
    with _child("import time; time.sleep(30)") as process:
        with pytest.raises((TypeError, ValueError)):
            runner._communicate_staged_implementation(process, {"invalid": object()})
        assert process.poll() is not None
