from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Iterator, Sequence

from core_runtime.bounded_process_runner import (
    HostBoundedProcessRunner,
    ProcessExecutionCancelled,
    ProcessExecutionPolicy,
)


_DEFAULT_MAX_TIMEOUT_SECONDS = 3600.0
_DEFAULT_MAX_STDIN_BYTES = 128 * 1024 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_SECRET_ENV_KEY = re.compile(r"(?i)(?:api[_-]?key|password|secret|token)")


class RuntimeOperationCancelled(Exception):
    """Raised when a managed runtime operation is cancelled."""


class CancellationToken:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        """The event consumed by the Host-owned bounded process runner."""
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeOperationCancelled("Runtime operation was cancelled.")

    def cancel(self) -> bool:
        with self._lock:
            was_cancelled = self._cancelled.is_set()
            self._cancelled.set()
        return not was_cancelled


class CancellationRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tokens: dict[str, CancellationToken] = {}

    def register(self, operation_id: str, token: CancellationToken) -> None:
        with self._lock:
            self._tokens[str(operation_id)] = token

    def unregister(self, operation_id: str, token: CancellationToken) -> None:
        with self._lock:
            if self._tokens.get(str(operation_id)) is token:
                self._tokens.pop(str(operation_id), None)

    def cancel(self, operation_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(str(operation_id))
        if token is None:
            return False
        token.cancel()
        return True

    def contains(self, operation_id: str) -> bool:
        """Return whether this process owns an active operation worker."""
        with self._lock:
            return str(operation_id) in self._tokens


_LOCAL = threading.local()


@contextmanager
def cancellation_context(token: CancellationToken) -> Iterator[None]:
    previous = getattr(_LOCAL, "token", None)
    _LOCAL.token = token
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_LOCAL, "token")
            except AttributeError:
                pass
        else:
            _LOCAL.token = previous


def current_cancellation_token() -> CancellationToken | None:
    token = getattr(_LOCAL, "token", None)
    return token if isinstance(token, CancellationToken) else None


def run_cancellable_subprocess(
    command: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    token = current_cancellation_token()
    if token is not None:
        token.raise_if_cancelled()
    try:
        argv, cwd, environment = _host_process_request(command)
        timeout_seconds = _bounded_timeout(timeout)
        result = HostBoundedProcessRunner().run_local(
            argv=argv,
            cwd=cwd,
            stdin=input_text,
            timeout_seconds=timeout_seconds,
            environment=environment,
            policy=ProcessExecutionPolicy(
                allowed_executables=frozenset({argv[0]}),
                allowed_argv=(argv,),
                allowed_cwds=(cwd,),
                allowed_environment=frozenset(environment),
                max_stdin_bytes=_DEFAULT_MAX_STDIN_BYTES,
                max_stdout_bytes=_DEFAULT_MAX_OUTPUT_BYTES,
                max_stderr_bytes=_DEFAULT_MAX_OUTPUT_BYTES,
                max_timeout_seconds=timeout_seconds,
                redact_values=_environment_redact_values(environment),
            ),
            cancel_event=token.cancel_event if token is not None else None,
        )
    except ProcessExecutionCancelled as exc:
        raise RuntimeOperationCancelled("Runtime operation was cancelled.") from exc
    if result.timed_out:
        raise subprocess.TimeoutExpired(
            cmd=list(argv),
            timeout=timeout_seconds,
            output=result.stdout,
            stderr=result.stderr,
        )
    if result.exit_code is None:
        raise OSError(result.transport_error or "Host process transport failed")
    return subprocess.CompletedProcess(
        args=list(argv),
        returncode=int(result.exit_code),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _host_process_request(
    command: Sequence[str],
) -> tuple[tuple[str, ...], Path, dict[str, str]]:
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("runtime process command is empty")
    raw_argv = tuple(str(part) for part in command)
    executable = raw_argv[0]
    resolved_executable = (
        Path(executable).resolve()
        if Path(executable).is_absolute()
        else Path(shutil.which(executable) or "")
    )
    if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
        raise FileNotFoundError(executable)
    cwd = Path.cwd().resolve()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key and "=" not in key and "\x00" not in key and "\x00" not in value
    }
    return (str(resolved_executable), *raw_argv[1:]), cwd, environment


def _bounded_timeout(timeout: float | None) -> float:
    if timeout is None:
        return _DEFAULT_MAX_TIMEOUT_SECONDS
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("runtime process timeout must be a positive number")
    return min(float(timeout), _DEFAULT_MAX_TIMEOUT_SECONDS)


def _environment_redact_values(environment: dict[str, str]) -> tuple[str, ...]:
    """Return bounded secret values that must never survive child output."""
    values: list[str] = []
    total_bytes = 0
    for key in sorted(environment):
        value = environment[key]
        encoded_bytes = len(value.encode("utf-8"))
        if (
            not value
            or not _SECRET_ENV_KEY.search(key)
            or encoded_bytes > 4096
            or total_bytes + encoded_bytes > 64 * 1024
            or len(values) >= 128
        ):
            continue
        values.append(value)
        total_bytes += encoded_bytes
    return tuple(values)
