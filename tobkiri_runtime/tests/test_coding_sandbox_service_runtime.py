"""Focused Host-boundary tests for the Docker coding sandbox service."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core_runtime.bounded_process_runner import (
    BoundedProcessResult,
    HostProcessAttestation,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime import sandbox as runtime


_IMAGE = "fixture/runtime@sha256:" + ("0" * 64)
_DOCKER = "/opt/rumi/bin/docker"


def _bounded_result(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    transport_error: str | None = None,
) -> BoundedProcessResult:
    return BoundedProcessResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        attestation=HostProcessAttestation(
            authority="test",
            boundary="test",
            sandboxed=False,
            process_tree_kill="test",
        ),
        transport_error=transport_error,
    )


def _sandbox(tmp_path: Path) -> dict[str, object]:
    base = tmp_path / "base"
    work = tmp_path / "work"
    base.mkdir()
    work.mkdir()
    return {
        "id": "9b527e1e-3a61-46e0-9b09-b1f4663e736a",
        "base": base,
        "work": work,
    }


def test_docker_execution_uses_exact_bounded_host_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    responses = iter(
        (
            _bounded_result(exit_code=0),
            _bounded_result(
                exit_code=7,
                stdout="stdout",
                stderr="stderr",
                stdout_truncated=True,
            ),
        )
    )

    class FakeRunner:
        def run_local(self, **kwargs):
            calls.append(kwargs)
            return next(responses)

    monkeypatch.setattr(runtime.shutil, "which", lambda name: _DOCKER)
    monkeypatch.setattr(runtime, "HostBoundedProcessRunner", FakeRunner)
    sandbox = _sandbox(tmp_path)

    result = runtime.CodingSandboxRuntime(client=object(), profile_id="test")._execute(
        sandbox,
        {"image": _IMAGE, "command": ["/bin/sh", "-lc", "exit 7"], "timeout": 60},
    )

    work = tmp_path / "work"
    expected_run_argv = (
        _DOCKER,
        "run",
        "--rm",
        "--name",
        "rumi-coding-9b527e1e3a6146e09b09",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--cpus",
        "2",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--mount",
        f"type=bind,src={work},dst=/workspace,rw",
        "--workdir",
        "/workspace",
        _IMAGE,
        "/bin/sh",
        "-lc",
        "exit 7",
    )
    assert [call["argv"] for call in calls] == [
        (_DOCKER, "image", "inspect", _IMAGE),
        expected_run_argv,
    ]
    for call in calls:
        policy = call["policy"]
        assert call["cwd"] == work.resolve()
        assert call["stdin"] is None
        assert call["environment"] == {"PATH": os.defpath}
        assert policy.allowed_executables == frozenset({_DOCKER})
        assert policy.allowed_argv == (call["argv"],)
        assert policy.allowed_cwds == (work.resolve(),)
        assert policy.allowed_environment == frozenset({"PATH"})
        assert policy.max_stdin_bytes == 1
        assert policy.max_stdout_bytes == runtime._MAX_OUTPUT
        assert policy.max_stderr_bytes == runtime._MAX_OUTPUT
    assert calls[0]["timeout_seconds"] == 20
    assert calls[1]["timeout_seconds"] == 60
    assert result["exit_code"] == 7
    assert result["stdout"] == "stdout\n[truncated]\n"
    assert result["stderr"] == "stderr"
    assert result["network"] == "none"
    assert result["host_downgrade"] is False


def test_docker_timeout_attempts_bounded_container_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    responses = iter(
        (
            _bounded_result(exit_code=0),
            _bounded_result(exit_code=-15, timed_out=True),
            _bounded_result(exit_code=0),
        )
    )

    class FakeRunner:
        def run_local(self, **kwargs):
            calls.append(kwargs)
            return next(responses)

    monkeypatch.setattr(runtime.shutil, "which", lambda name: _DOCKER)
    monkeypatch.setattr(runtime, "HostBoundedProcessRunner", FakeRunner)
    sandbox = _sandbox(tmp_path)

    with pytest.raises(RuntimeError, match="timed out and was cancelled"):
        runtime.CodingSandboxRuntime(client=object(), profile_id="test")._execute(
            sandbox,
            {"image": _IMAGE, "command": ["sleep", "300"], "timeout": 60},
        )

    assert calls[-1]["argv"] == (
        _DOCKER,
        "rm",
        "-f",
        "rumi-coding-9b527e1e3a6146e09b09",
    )
    assert calls[-1]["timeout_seconds"] == 20
    assert calls[-1]["policy"].max_timeout_seconds == 20


def test_coding_sandbox_service_has_no_direct_docker_subprocess_path() -> None:
    source = Path(runtime.__file__).read_text(encoding="utf-8")

    assert "subprocess.run" not in source
    assert "subprocess.Popen" not in source
    assert "HostBoundedProcessRunner" in source
