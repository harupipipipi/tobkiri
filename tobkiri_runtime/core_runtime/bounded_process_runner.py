"""Host-owned, fail-closed process execution boundary."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|password|secret|token)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\"'\s,}]+)"
)
_MAX_ARGV_ITEMS = 256
_MAX_ARG_BYTES = 64 * 1024
_MAX_REDACT_VALUES = 256
_MAX_REDACT_VALUE_BYTES = 64 * 1024
_MAX_REDACT_TOTAL_BYTES = 1024 * 1024
_MAX_POLICY_STREAM_BYTES = 256 * 1024 * 1024
_PROCESS_TERM_GRACE_SECONDS = 0.5
_PROCESS_REAP_GRACE_SECONDS = 0.5
_IO_JOIN_GRACE_SECONDS = 0.5
_BACKEND_RESULT_KEYS = frozenset(
    {
        "command",
        "diagnostics",
        "error",
        "error_type",
        "execution_boundary",
        "exit_code",
        "ok",
        "process_failed",
        "provider_id",
        "request",
        "returncode",
        "sandbox_id",
        "sandbox_stage",
        "stderr",
        "stderr_truncated",
        "stdout",
        "stdout_truncated",
        "success",
        "timed_out",
    }
)


@dataclass(frozen=True)
class ProcessExecutionPolicy:
    """Exact Host policy for one bounded process request."""

    allowed_executables: frozenset[str]
    allowed_argv: tuple[tuple[str, ...], ...]
    allowed_cwds: tuple[Path, ...]
    allowed_environment: frozenset[str] = frozenset()
    max_stdin_bytes: int = 1024 * 1024
    max_stdout_bytes: int = 256 * 1024
    max_stderr_bytes: int = 64 * 1024
    max_timeout_seconds: float = 300.0
    redact_values: tuple[str, ...] = ()
    allow_path_search: bool = False
    allow_inherited_readonly_fds: bool = False


@dataclass(frozen=True)
class HostProcessAttestation:
    """Host measurement of the boundary that actually executed the process."""

    authority: str
    boundary: str
    sandboxed: bool
    process_tree_kill: str


@dataclass(frozen=True)
class BoundedProcessResult:
    """Bounded, redacted result. Raw process material is never persisted here."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    attestation: HostProcessAttestation
    transport_error: str | None = None

    @property
    def completed(self) -> bool:
        return self.exit_code is not None


class ProcessExecutionCancelled(RuntimeError):
    """A Host-owned process was cancelled after its process tree was reaped.

    ``result`` retains only the normal bounded and redacted output.  It is
    ``None`` when cancellation was requested before a child process started.
    """

    def __init__(self, result: BoundedProcessResult | None = None) -> None:
        super().__init__("Host process execution was cancelled.")
        self.result = result


@dataclass
class _CappedBytes:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        remaining = max(0, self.limit - len(self.data))
        self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True


@dataclass
class _CappedFile:
    handle: Any
    limit: int
    written: int = 0
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        remaining = max(0, self.limit - self.written)
        accepted = chunk[:remaining]
        if accepted:
            self.handle.write(accepted)
            self.written += len(accepted)
        if len(chunk) > remaining:
            self.truncated = True


class HostBoundedProcessRunner:
    """Validate, execute, cap, redact, and attest Host process requests."""

    AUTHORITY = "core_runtime.bounded_process_runner"

    def run_local(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | bytes | None,
        timeout_seconds: float,
        environment: Mapping[str, str],
        policy: ProcessExecutionPolicy,
        cancel_event: threading.Event | None = None,
        inherited_fds: Sequence[int] = (),
        inherited_fd_tokens: Sequence[str] = (),
    ) -> BoundedProcessResult:
        """Run one exact command, or raise after a requested cancellation.

        When ``cancel_event`` is set while the command is live, the Host
        terminates and reaps its entire process tree before raising
        :class:`ProcessExecutionCancelled`.  The exception carries the normal
        bounded, redacted result.  A timeout remains a distinct successful
        return value with ``timed_out=True``.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise ProcessExecutionCancelled()
        pass_fds = self._pin_inherited_fds(inherited_fds, policy)
        try:
            request = self._validate_request(
                argv=argv,
                cwd=cwd,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                environment=environment,
                policy=policy,
            )
            request["stdin"] = self._materialize_inherited_fd_tokens(
                request["stdin"], inherited_fd_tokens, pass_fds
            )
        except Exception:
            self._close_inherited_fds(pass_fds)
            raise
        redaction_lookahead = self._redaction_lookahead_bytes(policy)
        stdout_buffer = _CappedBytes(policy.max_stdout_bytes + redaction_lookahead)
        stderr_buffer = _CappedBytes(policy.max_stderr_bytes + redaction_lookahead)
        popen_kwargs: dict[str, Any] = {
            "args": request["argv"],
            "cwd": request["cwd"],
            "env": request["environment"],
            "stdin": (subprocess.PIPE if request["stdin"] is not None else subprocess.DEVNULL),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "close_fds": True,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
            popen_kwargs["pass_fds"] = pass_fds
        elif os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        try:
            process = subprocess.Popen(**popen_kwargs)
        finally:
            # Popen has either copied these descriptors into the child or
            # failed. The parent-owned pinned duplicates never outlive spawn.
            self._close_inherited_fds(pass_fds)
        io_threads = [
            threading.Thread(
                target=self._drain,
                args=(process.stdout, stdout_buffer),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(process.stderr, stderr_buffer),
                daemon=True,
            ),
        ]
        if request["stdin"] is not None and process.stdin is not None:
            io_threads.append(
                threading.Thread(
                    target=self._write_stdin,
                    args=(process.stdin, request["stdin"]),
                    daemon=True,
                )
            )
        for io_thread in io_threads:
            io_thread.start()
        timed_out = False
        cancelled = False
        process_tree_termination_failed = False
        deadline = time.monotonic() + request["timeout_seconds"]
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                process_tree_termination_failed = not self._terminate_process_tree(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process_tree_termination_failed = not self._terminate_process_tree(process)
                break
            try:
                # Polling keeps the cancellation latency bounded without
                # moving process ownership out of this Host boundary.
                process.wait(timeout=min(remaining, 0.05))
            except subprocess.TimeoutExpired:
                continue
        self._reap_process(process)
        self._join_io_threads(io_threads)
        stdout_incomplete = io_threads[0].is_alive()
        stderr_incomplete = io_threads[1].is_alive()
        attestation = HostProcessAttestation(
            authority=self.AUTHORITY,
            boundary="bounded_host_process",
            sandboxed=False,
            process_tree_kill=self._process_tree_kill_attestation(
                termination_failed=process_tree_termination_failed,
            ),
        )
        result = self._result(
            exit_code=process.returncode,
            stdout=bytes(stdout_buffer.data),
            stderr=bytes(stderr_buffer.data),
            timed_out=timed_out,
            stdout_truncated=(
                stdout_buffer.truncated
                or len(stdout_buffer.data) > policy.max_stdout_bytes
                or stdout_incomplete
            ),
            stderr_truncated=(
                stderr_buffer.truncated
                or len(stderr_buffer.data) > policy.max_stderr_bytes
                or stderr_incomplete
            ),
            attestation=attestation,
            transport_error=None,
            policy=policy,
        )
        # Preserve the historical cancellation contract: a cancellation that
        # races with normal child completion is still reported as cancelled.
        # A timeout observed first remains a timeout, not a cancellation.
        if not timed_out and (cancelled or (cancel_event is not None and cancel_event.is_set())):
            raise ProcessExecutionCancelled(result)
        return result

    @staticmethod
    def _pin_inherited_fds(
        inherited_fds: Sequence[int], policy: ProcessExecutionPolicy
    ) -> tuple[int, ...]:
        """Duplicate first, then validate the identities inherited by Popen."""

        requested = tuple(int(value) for value in inherited_fds)
        if not requested:
            return ()
        if os.name != "posix" or not policy.allow_inherited_readonly_fds:
            raise PermissionError("inherited process descriptors are not allowed")
        if len(requested) != len(set(requested)) or len(requested) > 8:
            raise PermissionError("inherited process descriptors are invalid")
        import fcntl

        pinned: list[int] = []
        try:
            for descriptor in requested:
                if descriptor < 0:
                    raise PermissionError("inherited process descriptor is invalid")
                duplicate = os.dup(descriptor)
                pinned.append(duplicate)
                metadata = os.fstat(duplicate)
                access_mode = fcntl.fcntl(duplicate, fcntl.F_GETFL) & os.O_ACCMODE
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink not in {0, 1}
                    or access_mode != os.O_RDONLY
                ):
                    raise PermissionError(
                        "only read-only regular descriptors may be inherited"
                    )
            return tuple(pinned)
        except Exception:
            HostBoundedProcessRunner._close_inherited_fds(pinned)
            raise

    @staticmethod
    def _materialize_inherited_fd_tokens(
        stdin: str | bytes | None,
        tokens: Sequence[str],
        descriptors: Sequence[int],
    ) -> str | bytes | None:
        """Substitute exact descriptor tokens only after identities are pinned."""

        if not descriptors:
            if tokens:
                raise PermissionError("inherited descriptor tokens are not allowed")
            return stdin
        if len(tokens) != len(descriptors) or stdin is None:
            raise PermissionError("inherited descriptor token binding is incomplete")
        if len(tokens) != len(set(tokens)):
            raise PermissionError("inherited descriptor tokens are invalid")
        for token in tokens:
            if (
                not token.startswith("__TOBKIRI_")
                or not token.endswith("__")
                or len(token) > 128
            ):
                raise PermissionError("inherited descriptor token is invalid")
        if isinstance(stdin, bytes):
            materialized_bytes = stdin
            for token, descriptor in zip(tokens, descriptors, strict=True):
                needle = token.encode("ascii")
                replacement = str(descriptor).encode("ascii")
                if materialized_bytes.count(needle) != 1:
                    raise PermissionError(
                        "inherited descriptor token binding is ambiguous"
                    )
                materialized_bytes = materialized_bytes.replace(needle, replacement)
            return materialized_bytes
        materialized_text = stdin
        for token, descriptor in zip(tokens, descriptors, strict=True):
            if materialized_text.count(token) != 1:
                raise PermissionError(
                    "inherited descriptor token binding is ambiguous"
                )
            materialized_text = materialized_text.replace(token, str(descriptor))
        return materialized_text

    @staticmethod
    def _close_inherited_fds(descriptors: Sequence[int]) -> None:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def run_attested_backend(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | bytes | None,
        timeout_seconds: float,
        environment: Mapping[str, str],
        policy: ProcessExecutionPolicy,
        backend: Callable[[], Mapping[str, Any]],
        boundary: str,
        sandboxed: bool,
        process_tree_kill: str,
    ) -> BoundedProcessResult:
        """Apply the same policy to a Host-owned sandbox transport."""
        self._validate_request(
            argv=argv,
            cwd=cwd,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            environment=environment,
            policy=policy,
        )
        try:
            payload = backend()
        except Exception as exc:
            error_bytes, error_truncated = self._bounded_bytes(
                str(exc).encode("utf-8", errors="replace"),
                policy.max_stderr_bytes,
            )
            return self._result(
                exit_code=None,
                stdout=b"",
                stderr=error_bytes,
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=error_truncated,
                attestation=HostProcessAttestation(
                    authority=self.AUTHORITY,
                    boundary=boundary,
                    sandboxed=sandboxed,
                    process_tree_kill=process_tree_kill,
                ),
                transport_error="provider_unavailable",
                policy=policy,
            )
        self._validate_backend_result(payload)
        redaction_lookahead = self._redaction_lookahead_bytes(policy)
        stdout, stdout_over_limit = self._bounded_bytes(
            str(payload["stdout"]).encode("utf-8", errors="replace"),
            policy.max_stdout_bytes + redaction_lookahead,
        )
        stderr, stderr_over_limit = self._bounded_bytes(
            str(payload["stderr"]).encode("utf-8", errors="replace"),
            policy.max_stderr_bytes + redaction_lookahead,
        )
        exit_code = payload["exit_code"]
        timed_out = bool(payload.get("timed_out"))
        transport_error = None
        if exit_code is None:
            raw_transport_error = str(
                payload.get("error_type") or payload.get("error") or "provider_unavailable"
            )
            transport_error, _ = self._redacted_bounded_text(
                raw_transport_error.encode("utf-8", errors="replace"),
                policy.max_stderr_bytes,
                policy,
            )
        return self._result(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stdout_truncated=(
                bool(payload.get("stdout_truncated"))
                or stdout_over_limit
                or len(stdout) > policy.max_stdout_bytes
            ),
            stderr_truncated=(
                bool(payload.get("stderr_truncated"))
                or stderr_over_limit
                or len(stderr) > policy.max_stderr_bytes
            ),
            attestation=HostProcessAttestation(
                authority=self.AUTHORITY,
                boundary=boundary,
                sandboxed=sandboxed,
                process_tree_kill=process_tree_kill,
            ),
            transport_error=transport_error,
            policy=policy,
        )

    def run_local_to_file(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | bytes | None,
        timeout_seconds: float,
        environment: Mapping[str, str],
        policy: ProcessExecutionPolicy,
        stdout_path: Path,
    ) -> BoundedProcessResult:
        """Run locally while streaming bounded stdout to a new regular file."""
        request = self._validate_request(
            argv=argv,
            cwd=cwd,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            environment=environment,
            policy=policy,
        )
        output_path = Path(stdout_path)
        if (
            not output_path.is_absolute()
            or output_path.exists()
            or output_path.is_symlink()
            or output_path.parent.is_symlink()
            or not output_path.parent.is_dir()
        ):
            raise PermissionError("process stdout path must be a new file in a real directory")
        stderr_buffer = _CappedBytes(
            policy.max_stderr_bytes + self._redaction_lookahead_bytes(policy)
        )
        timed_out = False
        process_tree_termination_failed = False
        with output_path.open("xb") as output_handle:
            stdout_file = _CappedFile(output_handle, policy.max_stdout_bytes)
            popen_kwargs: dict[str, Any] = {
                "args": request["argv"],
                "cwd": request["cwd"],
                "env": request["environment"],
                "stdin": (subprocess.PIPE if request["stdin"] is not None else subprocess.DEVNULL),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "close_fds": True,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt":
                popen_kwargs["creationflags"] = getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                )
            process = subprocess.Popen(**popen_kwargs)
            io_threads = [
                threading.Thread(
                    target=self._drain,
                    args=(process.stdout, stdout_file),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._drain,
                    args=(process.stderr, stderr_buffer),
                    daemon=True,
                ),
            ]
            if request["stdin"] is not None and process.stdin is not None:
                io_threads.append(
                    threading.Thread(
                        target=self._write_stdin,
                        args=(process.stdin, request["stdin"]),
                        daemon=True,
                    )
                )
            for io_thread in io_threads:
                io_thread.start()
            try:
                process.wait(timeout=request["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                process_tree_termination_failed = not self._terminate_process_tree(process)
            self._reap_process(process)
            self._join_io_threads(io_threads)
            output_handle.flush()
            os.fsync(output_handle.fileno())
            stdout_incomplete = io_threads[0].is_alive()
            stderr_incomplete = io_threads[1].is_alive()
        return self._result(
            exit_code=process.returncode,
            stdout=b"",
            stderr=bytes(stderr_buffer.data),
            timed_out=timed_out,
            stdout_truncated=stdout_file.truncated or stdout_incomplete,
            stderr_truncated=(
                stderr_buffer.truncated
                or len(stderr_buffer.data) > policy.max_stderr_bytes
                or stderr_incomplete
            ),
            attestation=HostProcessAttestation(
                authority=self.AUTHORITY,
                boundary="bounded_host_process_file_sink",
                sandboxed=False,
                process_tree_kill=(
                    self._process_tree_kill_attestation(
                        termination_failed=process_tree_termination_failed,
                    )
                ),
            ),
            transport_error=None,
            policy=policy,
        )

    @staticmethod
    def _validate_request(
        *,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | bytes | None,
        timeout_seconds: float,
        environment: Mapping[str, str],
        policy: ProcessExecutionPolicy,
    ) -> dict[str, Any]:
        limits = (
            policy.max_stdin_bytes,
            policy.max_stdout_bytes,
            policy.max_stderr_bytes,
        )
        if (
            any(
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit <= 0
                or limit > _MAX_POLICY_STREAM_BYTES
                for limit in limits
            )
            or isinstance(policy.max_timeout_seconds, bool)
            or not isinstance(policy.max_timeout_seconds, (int, float))
            or policy.max_timeout_seconds <= 0
            or policy.max_timeout_seconds > 3600
        ):
            raise ValueError("process policy violates the bounded schema")
        if (
            not isinstance(policy.redact_values, tuple)
            or len(policy.redact_values) > _MAX_REDACT_VALUES
            or any(
                not isinstance(value, str)
                or "\x00" in value
                or len(value.encode("utf-8")) > _MAX_REDACT_VALUE_BYTES
                for value in policy.redact_values
            )
            or sum(
                len(value.encode("utf-8"))
                for value in policy.redact_values
                if isinstance(value, str)
            )
            > _MAX_REDACT_TOTAL_BYTES
        ):
            raise ValueError("process redaction policy violates the bounded schema")
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise ValueError("process argv must be a sequence of strings")
        normalized_argv = tuple(argv)
        if (
            not normalized_argv
            or not isinstance(normalized_argv[0], str)
            or not normalized_argv[0]
            or len(normalized_argv) > _MAX_ARGV_ITEMS
            or any(not isinstance(item, str) or "\x00" in item for item in normalized_argv)
            or sum(len(item.encode("utf-8")) for item in normalized_argv) > _MAX_ARG_BYTES
        ):
            raise ValueError("process argv violates the bounded schema")
        if normalized_argv[0] not in policy.allowed_executables:
            raise PermissionError("process executable is not allowlisted")
        if not policy.allow_path_search and not Path(normalized_argv[0]).is_absolute():
            raise PermissionError("process executable must be an absolute path")
        if normalized_argv not in policy.allowed_argv:
            raise PermissionError("process arguments are not exactly allowlisted")
        raw_cwd = Path(cwd)
        if not raw_cwd.is_absolute() or raw_cwd.is_symlink():
            raise PermissionError("process cwd must be an absolute non-symlink directory")
        normalized_cwd = raw_cwd.resolve()
        allowed_cwds = tuple(Path(item).resolve() for item in policy.allowed_cwds)
        if normalized_cwd not in allowed_cwds or not normalized_cwd.is_dir():
            raise PermissionError("process cwd is not exactly allowlisted")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > policy.max_timeout_seconds
        ):
            raise ValueError("process timeout violates the bounded schema")
        if stdin is not None and not isinstance(stdin, (str, bytes)):
            raise ValueError("process stdin must be text, bytes, or null")
        stdin_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        if stdin_bytes is not None and len(stdin_bytes) > policy.max_stdin_bytes:
            raise ValueError("process stdin exceeds the policy limit")
        normalized_environment: dict[str, str] = {}
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or "=" in key
                or "\x00" in key
                or "\x00" in value
            ):
                raise ValueError("process environment violates the bounded schema")
            if key not in policy.allowed_environment:
                raise PermissionError(f"process environment key is not allowlisted: {key}")
            normalized_environment[key] = value
        return {
            "argv": normalized_argv,
            "cwd": str(normalized_cwd),
            "stdin": stdin_bytes,
            "timeout_seconds": float(timeout_seconds),
            "environment": normalized_environment,
        }

    @staticmethod
    def _validate_backend_result(payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("process backend output must be an object")
        if "exit_code" not in payload or "stdout" not in payload or "stderr" not in payload:
            raise ValueError("process backend output is missing required fields")
        unknown_keys = set(payload) - _BACKEND_RESULT_KEYS
        if unknown_keys:
            raise ValueError("process backend output has unknown fields")
        exit_code = payload["exit_code"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError("process backend exit_code must be an integer or null")
        if not isinstance(payload["stdout"], str) or not isinstance(payload["stderr"], str):
            raise ValueError("process backend stdout and stderr must be strings")
        timed_out = payload.get("timed_out", False)
        if not isinstance(timed_out, bool):
            raise ValueError("process backend timed_out must be boolean")
        for key in (
            "stdout_truncated",
            "stderr_truncated",
            "success",
            "ok",
            "process_failed",
        ):
            if key in payload and not isinstance(payload[key], bool):
                raise ValueError(f"process backend {key} must be boolean")
        for key in ("error", "error_type"):
            if key in payload and not isinstance(payload[key], str):
                raise ValueError(f"process backend {key} must be a string")
        if "returncode" in payload:
            returncode = payload["returncode"]
            if returncode is not None and (
                isinstance(returncode, bool) or not isinstance(returncode, int)
            ):
                raise ValueError("process backend returncode must be an integer or null")
            if returncode != exit_code:
                raise ValueError("process backend returncode conflicts with exit_code")
        if exit_code is None and not (
            timed_out or payload.get("error") or payload.get("error_type")
        ):
            raise ValueError("process backend null exit_code requires timeout or transport error")
        if timed_out and exit_code == 0:
            raise ValueError("process backend timeout conflicts with exit_code")
        if "success" in payload and "ok" in payload:
            if payload["success"] != payload["ok"]:
                raise ValueError("process backend success conflicts with ok")
        if payload.get("success") is True or payload.get("ok") is True:
            if exit_code != 0 or timed_out:
                raise ValueError("process backend success conflicts with process outcome")
        if "process_failed" in payload:
            expected_process_failed = exit_code not in (0, None)
            if payload["process_failed"] != expected_process_failed:
                raise ValueError("process backend process_failed conflicts with exit_code")

    @staticmethod
    def _drain(stream: Any, output: _CappedBytes | _CappedFile) -> None:
        if stream is None:
            return
        try:
            for chunk in iter(lambda: stream.read(8192), b""):
                output.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _write_stdin(stream: Any, value: bytes) -> None:
        try:
            stream.write(value)
            stream.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            stream.close()

    @classmethod
    def _terminate_process_tree(cls, process: subprocess.Popen[Any]) -> bool:
        if os.name == "posix":
            process_group = process.pid
            try:
                os.killpg(process_group, signal.SIGTERM)
            except ProcessLookupError:
                return True
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
                return False
            deadline = time.monotonic() + _PROCESS_TERM_GRACE_SECONDS
            while time.monotonic() < deadline:
                process.poll()
                if not cls._posix_process_group_exists(process_group):
                    break
                time.sleep(0.01)
            if cls._posix_process_group_exists(process_group):
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return not cls._posix_process_group_exists(process_group)
        taskkill = cls._windows_taskkill_path()
        if taskkill is not None:
            try:
                completed = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    close_fds=True,
                    timeout=_PROCESS_TERM_GRACE_SECONDS,
                )
                if completed.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
        # Do not leave the direct child alive merely because taskkill could
        # not be verified.  The caller records this as an unverified tree
        # termination rather than claiming the Windows tree guarantee.
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        return False

    @staticmethod
    def _windows_taskkill_path() -> Path | None:
        """Resolve taskkill from Windows itself, never through PATH."""
        if os.name != "nt":
            return None
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            kernel32 = ctypes.CDLL("kernel32", use_last_error=True)
            length = int(kernel32.GetSystemDirectoryW(buffer, len(buffer)))
            if length <= 0 or length >= len(buffer):
                return None
            candidate = Path(buffer.value) / "taskkill.exe"
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                return None
            return candidate
        except (AttributeError, OSError):
            return None

    @staticmethod
    def _process_tree_kill_attestation(*, termination_failed: bool) -> str:
        if os.name == "posix":
            return "posix_process_group"
        if termination_failed:
            return "windows_process_tree_unverified"
        return "windows_process_tree"

    @staticmethod
    def _posix_process_group_exists(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _reap_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=_PROCESS_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=_PROCESS_REAP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _join_io_threads(io_threads: Sequence[threading.Thread]) -> None:
        deadline = time.monotonic() + _IO_JOIN_GRACE_SECONDS
        for io_thread in io_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            io_thread.join(timeout=remaining)

    @staticmethod
    def _bounded_bytes(value: bytes, limit: int) -> tuple[bytes, bool]:
        return value[:limit], len(value) > limit

    @staticmethod
    def _redaction_lookahead_bytes(policy: ProcessExecutionPolicy) -> int:
        return max(
            (len(value.encode("utf-8")) - 1 for value in policy.redact_values if value),
            default=0,
        )

    @classmethod
    def _result(
        cls,
        *,
        exit_code: int | None,
        stdout: bytes,
        stderr: bytes,
        timed_out: bool,
        stdout_truncated: bool,
        stderr_truncated: bool,
        attestation: HostProcessAttestation,
        transport_error: str | None,
        policy: ProcessExecutionPolicy,
    ) -> BoundedProcessResult:
        stdout_text, redacted_stdout_truncated = cls._redacted_bounded_text(
            stdout,
            policy.max_stdout_bytes,
            policy,
        )
        stderr_text, redacted_stderr_truncated = cls._redacted_bounded_text(
            stderr,
            policy.max_stderr_bytes,
            policy,
        )
        return BoundedProcessResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated or redacted_stdout_truncated,
            stderr_truncated=stderr_truncated or redacted_stderr_truncated,
            attestation=attestation,
            transport_error=transport_error,
        )

    @classmethod
    def _redacted_bounded_text(
        cls,
        value: bytes,
        limit: int,
        policy: ProcessExecutionPolicy,
    ) -> tuple[str, bool]:
        redacted = cls._redact(value.decode("utf-8", errors="replace"), policy)
        encoded = redacted.encode("utf-8")
        clipped, truncated = cls._bounded_bytes(encoded, limit)
        return clipped.decode("utf-8", errors="replace"), truncated

    @staticmethod
    def _redact(value: str, policy: ProcessExecutionPolicy) -> str:
        redacted = value
        for secret in sorted(
            (item for item in policy.redact_values if item),
            key=len,
            reverse=True,
        ):
            redacted = redacted.replace(secret, "[REDACTED]")

        def redact_assignment(match: re.Match[str]) -> str:
            assignment_value = match.group(2)
            if assignment_value.casefold() in {"true", "false", "null"}:
                return match.group(0)
            return f"{match.group(1)}[REDACTED]"

        return _SECRET_ASSIGNMENT.sub(redact_assignment, redacted)
