from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "quality" / "compact_test_runner.py"
FIXTURE = ROOT / "scripts" / "quality" / "compact_runner_fixture.py"


def _run(
    tmp_path: Path,
    name: str,
    *fixture_arguments: str,
    timeout_seconds: float | None = None,
    parent_stdio_encoding: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    log_path = tmp_path / f"{name}.log"
    runner_arguments = [
        sys.executable,
        str(RUNNER),
        "--log-dir",
        str(tmp_path),
        "--log-file",
        log_path.name,
    ]
    if timeout_seconds is not None:
        runner_arguments.extend(("--timeout-seconds", str(timeout_seconds)))
    runner_arguments.extend(
        (
            "--",
            sys.executable,
            str(FIXTURE),
            *fixture_arguments,
        )
    )
    environment = os.environ.copy()
    if parent_stdio_encoding is not None:
        environment["PYTHONIOENCODING"] = parent_stdio_encoding
    result = subprocess.run(
        runner_arguments,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    return result, log_path


@pytest.mark.parametrize("framework", ("pytest", "vitest", "cargo"))
def test_success_is_at_most_ten_lines_and_full_log_preserves_order(
    tmp_path: Path, framework: str
) -> None:
    result, log_path = _run(
        tmp_path,
        f"success-{framework}",
        "--framework",
        framework,
    )

    assert result.returncode == 0
    assert 1 <= len(result.stdout.splitlines()) <= 10
    assert "status: passed (exit code 0)" in result.stdout
    assert "not detected" not in result.stdout
    assert f"full log: {log_path}" in result.stdout
    full_log = log_path.read_text(encoding="utf-8")
    ordered = ["ORDER-stdout-1", "ORDER-stderr-2", "ORDER-stdout-3"]
    positions = [full_log.index(value) for value in ordered]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("framework", "exit_code", "trace"),
    (
        ("pytest", 1, "AssertionError: fixture boom"),
        ("vitest", 2, "expected 2 to be 3"),
        ("cargo", 101, "fixture_failure' panicked"),
    ),
)
def test_failure_is_bounded_to_relevant_trace_and_preserves_exit_code(
    tmp_path: Path, framework: str, exit_code: int, trace: str
) -> None:
    result, log_path = _run(
        tmp_path,
        f"failure-{framework}",
        "--framework",
        framework,
        "--outcome",
        "failure",
    )

    assert result.returncode == exit_code
    assert len(result.stdout.splitlines()) <= 120
    assert f"status: failed (exit code {exit_code})" in result.stdout
    assert trace in result.stdout
    assert "PASS-NOISE-0000" not in result.stdout
    full_log = log_path.read_text(encoding="utf-8")
    assert "PASS-NOISE-0000" in full_log
    assert "PASS-NOISE-6999" in full_log
    assert len(full_log.splitlines()) >= 7_000
    assert trace in full_log


def test_unicode_is_kept_in_full_log_and_console_summary_stays_compact(
    tmp_path: Path,
) -> None:
    result, log_path = _run(
        tmp_path,
        "unicode",
        "--framework",
        "pytest",
        "--unicode",
        parent_stdio_encoding="cp1252",
    )

    assert result.returncode == 0
    assert "Unicode: 日本語 🐦 café" in log_path.read_text(encoding="utf-8")
    assert len(result.stdout.splitlines()) <= 10


def test_secret_shaped_child_output_is_redacted_from_console_but_logged(
    tmp_path: Path,
) -> None:
    secret = "api_key=should-not-reach-console"
    result, log_path = _run(
        tmp_path,
        "redaction",
        "--framework",
        "pytest",
        "--outcome",
        "failure",
        "--message",
        secret,
    )

    assert result.returncode == 1
    assert "should-not-reach-console" not in result.stdout
    assert "api_key=<redacted>" in result.stdout
    assert secret in log_path.read_text(encoding="utf-8")


def test_terminal_spoofing_controls_are_removed_but_unicode_is_preserved(
    tmp_path: Path,
) -> None:
    controls = "\x7f\x85\u202e\u2066\u200b\u200d"
    message = f"visible 日本語 🐦{controls}safe"
    result, log_path = _run(
        tmp_path,
        "controls",
        "--framework",
        "pytest",
        "--outcome",
        "failure",
        "--message",
        message,
        parent_stdio_encoding="cp1252",
    )

    assert result.returncode == 1
    assert "visible 日本語 🐦" in result.stdout
    assert "safe" in result.stdout
    assert all(control not in result.stdout for control in controls)
    assert message in log_path.read_text(encoding="utf-8")


def test_timeout_fails_closed_and_saves_partial_log(tmp_path: Path) -> None:
    result, log_path = _run(
        tmp_path,
        "timeout",
        "--framework",
        "pytest",
        "--sleep",
        "30",
        timeout_seconds=0.1,
    )

    assert result.returncode == 124
    assert "status: failed (timeout; wrapper exit 124)" in result.stdout
    assert "ORDER-stdout-1" in log_path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal return-code contract")
def test_child_signal_is_propagated_as_a_signal(tmp_path: Path) -> None:
    result, log_path = _run(
        tmp_path,
        "signal",
        "--framework",
        "cargo",
        "--signal",
        "TERM",
    )

    assert result.returncode == -signal.SIGTERM
    assert "fixture requests SIGTERM" in log_path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal return-code contract")
def test_signal_received_by_wrapper_is_forwarded_and_propagated(tmp_path: Path) -> None:
    log_path = tmp_path / "forwarded-signal.log"
    process = subprocess.Popen(
        [
            sys.executable,
            str(RUNNER),
            "--log-dir",
            str(tmp_path),
            "--log-file",
            log_path.name,
            "--",
            sys.executable,
            str(FIXTURE),
            "--framework",
            "pytest",
            "--sleep",
            "30",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log_path.exists() and "ORDER-stdout-1" in log_path.read_text(
            encoding="utf-8"
        ):
            break
        time.sleep(0.01)
    assert log_path.exists() and "ORDER-stdout-1" in log_path.read_text(
        encoding="utf-8"
    )

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == -signal.SIGTERM, (stdout, stderr)
    assert "received SIGTERM" in stdout
    assert "ORDER-stdout-1" in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "unsafe_name", ("../escape.log", "/tmp/escape.log", "a\\b.log")
)
def test_log_filename_rejects_traversal(tmp_path: Path, unsafe_name: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--log-dir",
            str(tmp_path),
            "--log-file",
            unsafe_name,
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 125
    assert not (tmp_path.parent / "escape.log").exists()


def test_existing_log_is_never_overwritten(tmp_path: Path) -> None:
    log_path = tmp_path / "existing.log"
    log_path.write_text("keep me", encoding="utf-8")
    result, _ = _run(tmp_path, "existing", "--framework", "pytest")

    assert result.returncode == 125
    assert log_path.read_text(encoding="utf-8") == "keep me"


def test_symlink_log_directory_is_rejected(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--log-dir",
            str(linked_directory),
            "--log-file",
            "unsafe.log",
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 125
    assert not (real_directory / "unsafe.log").exists()


def test_symlink_in_intermediate_log_directory_is_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    child = real_parent / "child"
    child.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--log-dir",
            str(linked_parent / "child"),
            "--log-file",
            "unsafe.log",
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 125
    assert not (child / "unsafe.log").exists()


def test_unique_placeholder_avoids_parallel_collisions(tmp_path: Path) -> None:
    commands = []
    for _ in range(4):
        commands.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER),
                    "--log-dir",
                    str(tmp_path),
                    "--log-file",
                    "parallel-{run}.log",
                    "--",
                    sys.executable,
                    str(FIXTURE),
                    "--framework",
                    "pytest",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    results = [command.communicate(timeout=10) for command in commands]
    assert all(command.returncode == 0 for command in commands), results
    assert len(list(tmp_path.glob("parallel-*.log"))) == 4


def test_missing_command_returns_127_without_echoing_its_path(tmp_path: Path) -> None:
    missing = "sensitive-command-name-that-does-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--log-dir",
            str(tmp_path),
            "--log-file",
            "missing.log",
            "--",
            missing,
            "--token=do-not-print",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 127
    assert missing not in result.stdout
    assert "do-not-print" not in result.stdout


def test_invalid_runner_arguments_fail_closed_without_reflection(
    tmp_path: Path,
) -> None:
    secret = "api_key=do-not-reflect"
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--log-dir",
            str(tmp_path),
            "--log-file",
            "invalid.log",
            "--timeout-seconds",
            secret,
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 125
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert not (tmp_path / "invalid.log").exists()
