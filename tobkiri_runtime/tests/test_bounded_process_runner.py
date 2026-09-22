from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core_runtime.bounded_process_runner import (
    BoundedProcessResult,
    HostBoundedProcessRunner,
    ProcessExecutionCancelled,
    ProcessExecutionPolicy,
)


def _policy(
    argv: tuple[str, ...],
    cwd: Path,
    **overrides: object,
) -> ProcessExecutionPolicy:
    values = {
        "allowed_executables": frozenset({argv[0]}),
        "allowed_argv": (argv,),
        "allowed_cwds": (cwd,),
        "allowed_environment": frozenset(),
        "max_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return ProcessExecutionPolicy(**values)


def test_runner_caps_redacts_and_preserves_exit_code(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-c",
        (
            "import sys; "
            "print('token=top-secret-' + 'x' * 200); "
            "print('password=hunter2', file=sys.stderr); "
            "raise SystemExit(7)"
        ),
    )

    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(
            argv,
            tmp_path,
            max_stdout_bytes=64,
            max_stderr_bytes=64,
            redact_values=("top-secret", "hunter2"),
        ),
    )

    assert result.exit_code == 7
    assert result.stdout_truncated is True
    assert "top-secret" not in result.stdout
    assert "hunter2" not in result.stderr
    assert "[REDACTED]" in result.stdout
    assert "[REDACTED]" in result.stderr
    assert result.attestation.authority == "core_runtime.bounded_process_runner"
    assert result.attestation.sandboxed is False


def test_runner_accepts_bounded_binary_stdin(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-c",
        "import sys; data=sys.stdin.buffer.read(); print(data.hex())",
    )
    payload = b"\x00\xffbinary\n"

    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=payload,
        timeout_seconds=1,
        environment={},
        policy=_policy(argv, tmp_path, max_stdin_bytes=len(payload)),
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == payload.hex()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor inheritance")
def test_runner_inherits_only_explicit_readonly_regular_descriptor(
    tmp_path: Path,
) -> None:
    payload = b"pinned-packvm-image"
    source = tmp_path / "image.img"
    source.write_bytes(payload)
    descriptor = os.open(source, os.O_RDONLY)
    argv = (
        sys.executable,
        "-c",
        "import os,sys; fd=int(sys.stdin.read()); print(os.read(fd, 4096).hex())",
    )
    token = "__TOBKIRI_TEST_IMAGE_FD__"
    try:
        result = HostBoundedProcessRunner().run_local(
            argv=argv,
            cwd=tmp_path,
            stdin=token,
            timeout_seconds=1,
            environment={},
            policy=_policy(
                argv,
                tmp_path,
                allow_inherited_readonly_fds=True,
            ),
            inherited_fds=(descriptor,),
            inherited_fd_tokens=(token,),
        )
    finally:
        os.close(descriptor)
    assert result.exit_code == 0
    assert result.stdout.strip() == payload.hex()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor inheritance")
def test_runner_rejects_writable_inherited_descriptor(tmp_path: Path) -> None:
    target = tmp_path / "writable.img"
    descriptor = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    argv = (sys.executable, "-c", "pass")
    try:
        with pytest.raises(PermissionError, match="read-only"):
            HostBoundedProcessRunner().run_local(
                argv=argv,
                cwd=tmp_path,
                stdin=None,
                timeout_seconds=1,
                environment={},
                policy=_policy(
                    argv,
                    tmp_path,
                    allow_inherited_readonly_fds=True,
                ),
                inherited_fds=(descriptor,),
                inherited_fd_tokens=("__TOBKIRI_TEST_WRITABLE_FD__",),
            )
    finally:
        os.close(descriptor)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor inheritance")
def test_runner_pins_fd_before_caller_number_is_closed_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.img"
    replacement = tmp_path / "replacement.img"
    original.write_bytes(b"verified-original")
    replacement.write_bytes(b"untrusted-replacement")
    caller_fd = os.open(original, os.O_RDONLY)
    token = "__TOBKIRI_RACE_IMAGE_FD__"
    argv = (
        sys.executable,
        "-c",
        "import os,sys; fd=int(sys.stdin.read()); print(os.read(fd,4096).decode())",
    )
    real_popen = subprocess.Popen
    reused_fd = -1
    inherited_duplicate = -1

    def reuse_before_spawn(**kwargs):
        nonlocal reused_fd, inherited_duplicate
        inherited_duplicate = int(kwargs["pass_fds"][0])
        os.close(caller_fd)
        reused_fd = os.open(replacement, os.O_RDONLY)
        assert reused_fd == caller_fd
        return real_popen(**kwargs)

    monkeypatch.setattr(subprocess, "Popen", reuse_before_spawn)
    try:
        result = HostBoundedProcessRunner().run_local(
            argv=argv,
            cwd=tmp_path,
            stdin=token,
            timeout_seconds=1,
            environment={},
            policy=_policy(
                argv,
                tmp_path,
                max_stdin_bytes=len(token),
                allow_inherited_readonly_fds=True,
            ),
            inherited_fds=(caller_fd,),
            inherited_fd_tokens=(token,),
        )
        assert result.stdout.strip() == "verified-original"
        with pytest.raises(OSError):
            os.fstat(inherited_duplicate)
    finally:
        if reused_fd >= 0:
            os.close(reused_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor inheritance")
def test_runner_closes_pinned_fd_when_spawn_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "image.img"
    source.write_bytes(b"verified")
    caller_fd = os.open(source, os.O_RDONLY)
    token = "__TOBKIRI_FAILED_SPAWN_FD__"
    argv = (sys.executable, "-c", "pass")
    pinned = -1

    def fail_spawn(**kwargs):
        nonlocal pinned
        pinned = int(kwargs["pass_fds"][0])
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(subprocess, "Popen", fail_spawn)
    try:
        with pytest.raises(OSError, match="synthetic spawn failure"):
            HostBoundedProcessRunner().run_local(
                argv=argv,
                cwd=tmp_path,
                stdin=token,
                timeout_seconds=1,
                environment={},
                policy=_policy(
                    argv,
                    tmp_path,
                    max_stdin_bytes=len(token),
                    allow_inherited_readonly_fds=True,
                ),
                inherited_fds=(caller_fd,),
                inherited_fd_tokens=(token,),
            )
        with pytest.raises(OSError):
            os.fstat(pinned)
    finally:
        os.close(caller_fd)


def test_runner_allows_exactly_allowlisted_empty_argument(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1] == '')",
        "",
    )

    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(argv, tmp_path),
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "True"


def test_runner_preserves_json_boolean_for_secret_named_field(
    tmp_path: Path,
) -> None:
    argv = (
        sys.executable,
        "-c",
        "print('{\"sibling_pack_secret\": false}')",
    )

    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(argv, tmp_path),
    )

    assert json.loads(result.stdout) == {"sibling_pack_secret": False}


def test_runner_streams_stdout_to_bounded_new_file(tmp_path: Path) -> None:
    argv = (sys.executable, "-c", "print('x' * 4096, end='')")
    output = tmp_path / "stream.bin"

    result = HostBoundedProcessRunner().run_local_to_file(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(argv, tmp_path, max_stdout_bytes=128),
        stdout_path=output,
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stdout_truncated is True
    assert output.read_bytes() == b"x" * 128


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("executable", "executable"),
        ("arguments", "arguments"),
        ("cwd", "cwd"),
        ("environment", "environment"),
    ],
)
def test_runner_allowlists_fail_closed(
    tmp_path: Path,
    change: str,
    expected: str,
) -> None:
    argv = (sys.executable, "-c", "print('ok')")
    policy = _policy(argv, tmp_path)
    actual_argv = argv
    actual_cwd = tmp_path
    environment = {}
    if change == "executable":
        actual_argv = ("/not/allowlisted", *argv[1:])
    elif change == "arguments":
        actual_argv = (*argv, "extra")
    elif change == "cwd":
        actual_cwd = tmp_path.parent
    else:
        environment = {"SECRET": "must-not-pass"}

    with pytest.raises((PermissionError, ValueError), match=expected):
        HostBoundedProcessRunner().run_local(
            argv=actual_argv,
            cwd=actual_cwd,
            stdin=None,
            timeout_seconds=1,
            environment=environment,
            policy=policy,
        )


def test_runner_timeout_kills_descendant_process_tree(tmp_path: Path) -> None:
    sentinel = tmp_path / "descendant-survived"
    child = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.7); "
        f"pathlib.Path({str(sentinel)!r}).write_text('alive')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(10)"
    )
    argv = (sys.executable, "-c", parent)

    started = time.monotonic()
    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=0.15,
        environment={},
        policy=_policy(argv, tmp_path),
    )
    elapsed = time.monotonic() - started
    time.sleep(0.8)

    assert result.timed_out is True
    assert result.exit_code is not None
    assert elapsed < 1.5
    assert not sentinel.exists()


def test_runner_cancellation_kills_descendant_process_tree_and_keeps_bounded_result(
    tmp_path: Path,
) -> None:
    started = tmp_path / "parent-started"
    sentinel = tmp_path / "descendant-survived"
    child = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.7); "
        f"pathlib.Path({str(sentinel)!r}).write_text('alive')"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        f"pathlib.Path({str(started)!r}).write_text('started'); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "sys.stdout.write('token=cancel-secret\\n'); sys.stdout.flush(); "
        "time.sleep(10)"
    )
    argv = (sys.executable, "-c", parent)
    cancel_event = threading.Event()
    outcome: dict[str, object] = {}

    def execute() -> None:
        try:
            HostBoundedProcessRunner().run_local(
                argv=argv,
                cwd=tmp_path,
                stdin=None,
                timeout_seconds=2,
                environment={},
                policy=_policy(argv, tmp_path, redact_values=("cancel-secret",)),
                cancel_event=cancel_event,
            )
        except ProcessExecutionCancelled as exc:
            outcome["exception"] = exc

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.01)
    assert started.exists()
    cancel_event.set()
    worker.join(timeout=3)
    time.sleep(0.8)

    assert not worker.is_alive()
    exc = outcome.get("exception")
    assert isinstance(exc, ProcessExecutionCancelled)
    assert exc.result is not None
    assert exc.result.timed_out is False
    assert "cancel-secret" not in exc.result.stdout
    assert "[REDACTED]" in exc.result.stdout
    assert not sentinel.exists()


def test_runner_never_attests_unverified_windows_tree_termination(monkeypatch) -> None:
    import core_runtime.bounded_process_runner as runner_module

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 123
            self.killed = False

        def poll(self):
            return None

        def kill(self) -> None:
            self.killed = True

    monkeypatch.setattr(runner_module.os, "name", "nt")
    monkeypatch.setattr(
        HostBoundedProcessRunner,
        "_windows_taskkill_path",
        staticmethod(lambda: None),
    )
    process = FakeProcess()

    verified = HostBoundedProcessRunner._terminate_process_tree(process)  # type: ignore[arg-type]

    assert verified is False
    assert process.killed is True
    assert (
        HostBoundedProcessRunner._process_tree_kill_attestation(
            termination_failed=True,
        )
        == "windows_process_tree_unverified"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe inheritance test")
def test_runner_does_not_wait_unbounded_for_descendant_pipe_holders(
    tmp_path: Path,
) -> None:
    child = "import time; time.sleep(2)"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    argv = (sys.executable, "-c", parent)

    started = time.monotonic()
    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(argv, tmp_path),
    )
    elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert elapsed < 1.5


def test_runner_redacts_secret_crossing_output_cap(tmp_path: Path) -> None:
    secret = "cross-boundary-secret"
    argv = (sys.executable, "-c", f"print('12345678901234{secret}')")

    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(
            argv,
            tmp_path,
            max_stdout_bytes=20,
            redact_values=(secret,),
        ),
    )

    assert result.stdout_truncated is True
    assert "cross-" not in result.stdout
    assert "[REDA" in result.stdout


def test_attested_backend_output_schema_requires_exit_code(tmp_path: Path) -> None:
    argv = ("python3", "-c", "print('ok')")

    with pytest.raises(ValueError, match="missing required fields"):
        HostBoundedProcessRunner().run_attested_backend(
            argv=argv,
            cwd=tmp_path,
            stdin=None,
            timeout_seconds=1,
            environment={},
            policy=_policy(argv, tmp_path, allow_path_search=True),
            backend=lambda: {"stdout": "ok", "stderr": ""},
            boundary="managed_sandbox",
            sandboxed=True,
            process_tree_kill="pid_namespace",
        )


def _run_backend(
    tmp_path: Path,
    payload: dict[str, object],
    **policy_overrides: object,
) -> BoundedProcessResult:
    argv = ("python3", "-c", "print('ok')")
    return HostBoundedProcessRunner().run_attested_backend(
        argv=argv,
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=1,
        environment={},
        policy=_policy(
            argv,
            tmp_path,
            allow_path_search=True,
            **policy_overrides,
        ),
        backend=lambda: payload,
        boundary="managed_sandbox",
        sandboxed=True,
        process_tree_kill="pid_namespace",
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
            },
            "null exit_code",
        ),
        (
            {
                "exit_code": 0,
                "returncode": 3,
                "stdout": "",
                "stderr": "",
            },
            "returncode conflicts",
        ),
        (
            {
                "exit_code": 2,
                "stdout": "",
                "stderr": "",
                "success": True,
            },
            "success conflicts",
        ),
        (
            {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "unexpected": "raw material",
            },
            "unknown fields",
        ),
        (
            {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": "yes",
            },
            "stdout_truncated must be boolean",
        ),
    ],
)
def test_attested_backend_output_schema_rejects_inconsistent_results(
    tmp_path: Path,
    payload: dict[str, object],
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        _run_backend(tmp_path, payload)


def test_attested_backend_propagates_truncation_and_redacts_transport_error(
    tmp_path: Path,
) -> None:
    secret = "backend-secret-value"
    result = _run_backend(
        tmp_path,
        {
            "exit_code": None,
            "returncode": None,
            "stdout": "already clipped",
            "stderr": f"token={secret}",
            "timed_out": True,
            "stdout_truncated": True,
            "stderr_truncated": True,
            "error_type": f"provider-{secret}",
            "success": False,
            "ok": False,
        },
        max_stderr_bytes=32,
        redact_values=(secret,),
    )

    assert result.exit_code is None
    assert result.timed_out is True
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert secret not in result.stderr
    assert secret not in str(result.transport_error)
    assert result.transport_error == "provider-[REDACTED]"
