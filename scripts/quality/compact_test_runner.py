#!/usr/bin/env python3
"""Run a noisy test command while keeping CI output compact.

The child process' stdout and stderr share one pipe, so their observed ordering is
preserved in the full log.  Console output is a short status summary on success
and a bounded, terminal-safe failure excerpt otherwise.
"""

from __future__ import annotations

import argparse
import codecs
from collections import deque
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from types import FrameType
from typing import BinaryIO, Sequence, TextIO


MAX_SUCCESS_LINES = 10
MAX_FAILURE_LINES = 120
MAX_EXCERPT_LINES = 113
MAX_TRACKED_LINES = 2_000
MAX_TRACKED_LINE_CHARS = 32_768
RUNNER_ERROR_EXIT = 125
TIMEOUT_EXIT = 124
_LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\Z")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_SECRET_VALUE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key)"
    r"(\s*(?::|=)\s*)([^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_UNSAFE_FORMAT_CODEPOINTS = frozenset(
    (
        0x061C,  # Arabic Letter Mark
        0x200B,  # Zero Width Space
        0x200C,  # Zero Width Non-Joiner
        0x200D,  # Zero Width Joiner
        0x200E,  # Left-to-Right Mark
        0x200F,  # Right-to-Left Mark
        0x2060,  # Word Joiner
        0xFEFF,  # Zero Width No-Break Space
    )
)
_SUMMARY_PATTERNS = (
    re.compile(
        r"^=+\s+.*\b(?:passed|failed|errors?|skipped|xfailed|xpassed)\b.*\s+=+$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?=.*\b(?:passed|failed|errors?|skipped|xfailed|xpassed)\b)"
        r"\d+\s+\w+(?:,\s+\d+\s+\w+)*\s+in\s+\d+(?:\.\d+)?s$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:Test Files|Tests)\s+", re.IGNORECASE),
    re.compile(r"^test result:\s+(?:ok|FAILED)\.", re.IGNORECASE),
)
_FAILURE_ANCHORS = (
    re.compile(r"^=+\s+FAILURES\s+=+$", re.IGNORECASE),
    re.compile(r"^Traceback \(most recent call last\):$"),
    re.compile(r"^(?:FAIL|FAILED)\s+", re.IGNORECASE),
    re.compile(r"^\s*---- .+ stdout ----\s*$"),
    re.compile(r"^thread ['\"].+['\"] panicked at", re.IGNORECASE),
    re.compile(r"^Caused by:$", re.IGNORECASE),
    re.compile(r"^Error:\s+", re.IGNORECASE),
)
_SAFE_COMMAND_NAMES = re.compile(
    r"(?:python(?:\d+(?:\.\d+)*)?|pytest|npm|npx|node|cargo|vitest|"
    r"pnpm|yarn|bun|uv|just)(?:\.exe)?\Z",
    re.IGNORECASE,
)


class LogDestinationError(ValueError):
    """Raised when a requested log destination is not safely creatable."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Avoid reflecting invalid, potentially sensitive arguments to stderr."""

    def error(self, message: str) -> None:
        """Convert parser errors into the runner's generic fail-closed error."""
        raise ValueError("invalid compact runner arguments")


def _configure_standard_stream(stream: TextIO) -> None:
    """Emit runner diagnostics as UTF-8 even under a legacy parent locale."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (OSError, ValueError):
        # In-process callers may replace stdio with a stream that cannot be
        # reconfigured. It already accepts text, so leave it untouched.
        return


def _child_environment() -> dict[str, str]:
    """Return an environment that makes Python child stdio UTF-8."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


class OutputTracker:
    """Keep a bounded text tail and framework summary candidates."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self.lines: deque[str] = deque(maxlen=MAX_TRACKED_LINES)
        self.summaries: deque[str] = deque(maxlen=4)

    def feed(self, data: bytes) -> None:
        """Decode a chunk for bounded diagnostic tracking."""
        text = self._decoder.decode(data)
        self._consume(text, final=False)

    def finish(self) -> None:
        """Flush the decoder and record an unterminated final line."""
        self._consume(self._decoder.decode(b"", final=True), final=True)

    def _consume(self, text: str, *, final: bool) -> None:
        combined = self._partial + text
        pieces = combined.splitlines(keepends=True)
        self._partial = ""
        for index, piece in enumerate(pieces):
            terminated = piece.endswith(("\n", "\r"))
            if not terminated and index == len(pieces) - 1 and not final:
                self._partial = piece[-MAX_TRACKED_LINE_CHARS:]
                if len(piece) > MAX_TRACKED_LINE_CHARS:
                    self._partial = "[line prefix omitted] " + self._partial
                continue
            self._record(piece.rstrip("\r\n"))
        if final and self._partial:
            self._record(self._partial)
            self._partial = ""

    def _record(self, line: str) -> None:
        display = _safe_display(line[-MAX_TRACKED_LINE_CHARS:])
        if len(line) > MAX_TRACKED_LINE_CHARS:
            display = f"[line prefix omitted] {display}"
        self.lines.append(display)
        stripped = display.strip()
        if any(pattern.search(stripped) for pattern in _SUMMARY_PATTERNS):
            self.summaries.append(stripped)

    def summary(self) -> str:
        """Return the most useful one-line framework summary."""
        if not self.summaries:
            return "not detected (see full log)"
        return " | ".join(list(self.summaries)[-2:])[:1_000]

    def failure_excerpt(self) -> list[str]:
        """Return the final relevant failure block within the console budget."""
        tracked = list(self.lines)
        start = 0
        for index in range(len(tracked) - 1, -1, -1):
            candidate = tracked[index].strip()
            if any(pattern.search(candidate) for pattern in _FAILURE_ANCHORS):
                start = index
                break
        excerpt = tracked[start:]
        if len(excerpt) > MAX_EXCERPT_LINES:
            omitted = len(excerpt) - MAX_EXCERPT_LINES + 1
            excerpt = [f"[... {omitted} tracked lines omitted ...]"] + excerpt[
                -(MAX_EXCERPT_LINES - 1) :
            ]
        return excerpt


def _safe_display(value: str) -> str:
    """Remove terminal controls and redact common secret-shaped values."""
    value = _ANSI_ESCAPE.sub("", value)
    value = "".join(character for character in value if _safe_character(character))
    value = _SECRET_VALUE.sub(r"\1\2<redacted>", value)
    return _BEARER_VALUE.sub("Bearer <redacted>", value)


def _safe_character(character: str) -> bool:
    """Allow readable Unicode while removing terminal-spoofing controls."""
    if character == "\t":
        return True
    codepoint = ord(character)
    if codepoint < 32 or 0x7F <= codepoint <= 0x9F:
        return False
    if codepoint in _UNSAFE_FORMAT_CODEPOINTS:
        return False
    if 0x202A <= codepoint <= 0x202E:  # bidi embedding and override marks
        return False
    if 0x2061 <= codepoint <= 0x2069:  # bidi isolates and invisible operators
        return False
    return True


def _command_label(command: Sequence[str]) -> str:
    """Describe a command without echoing its arguments or sensitive paths."""
    executable = command[0].replace("\\", "/").rsplit("/", 1)[-1]
    if not _SAFE_COMMAND_NAMES.fullmatch(executable):
        executable = "<executable hidden>"
    argument_count = max(0, len(command) - 1)
    return f"{executable} ({argument_count} arguments hidden)"


def _is_link_or_reparse(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _validated_log_directory(path: Path) -> Path:
    """Validate every supplied directory component without following links."""
    if ".." in path.parts:
        raise LogDestinationError("log directory traversal is not allowed")
    absolute = path if path.is_absolute() else Path.cwd() / path
    absolute = Path(os.path.abspath(absolute))
    anchor = Path(absolute.anchor)
    current = anchor
    relative_parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    try:
        for part in relative_parts:
            if part in ("", "."):
                continue
            current /= part
            if _is_link_or_reparse(current):
                raise LogDestinationError("log directory contains a link")
        if not absolute.is_dir():
            raise LogDestinationError("log directory is not a directory")
    except FileNotFoundError as error:
        raise LogDestinationError("log directory does not exist") from error
    return absolute


def _open_directory_no_links(path: Path) -> int:
    """Open an absolute POSIX directory one no-follow component at a time."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _expanded_log_name(template: str) -> str:
    if template.count("{run}") > 1:
        raise LogDestinationError("log filename has too many run placeholders")
    if "{run}" in template:
        run_id = f"{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}"
        template = template.replace("{run}", run_id)
    if not _LOG_NAME.fullmatch(template):
        raise LogDestinationError("log filename must be one safe path component")
    return template


def _open_log(log_directory: Path, log_file: str) -> tuple[Path, BinaryIO]:
    """Exclusively create a regular log file below a real directory."""
    try:
        resolved_directory = _validated_log_directory(log_directory)
        name = _expanded_log_name(log_file)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        mode = 0o600
        if os.name == "posix":
            directory_fd = _open_directory_no_links(resolved_directory)
            try:
                file_descriptor = os.open(name, flags, mode, dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
        else:
            file_descriptor = os.open(resolved_directory / name, flags, mode)
        return resolved_directory / name, os.fdopen(file_descriptor, "wb", buffering=0)
    except OSError as error:
        raise LogDestinationError("log destination cannot be created safely") from error


def _terminate_process(proc: subprocess.Popen[bytes], signum: int) -> None:
    """Best-effort signal delivery to the child process tree."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signum)
        elif signum == signal.SIGTERM:
            proc.terminate()
        else:
            proc.send_signal(signum)
    except (OSError, ProcessLookupError, ValueError):
        return


def _kill_process(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort forced termination of the child process tree."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        return


def _format_status(
    return_code: int, *, timed_out: bool, interrupted_signal: int | None
) -> str:
    if timed_out:
        return f"failed (timeout; wrapper exit {TIMEOUT_EXIT})"
    if interrupted_signal is not None:
        name = signal.Signals(interrupted_signal).name
        return f"failed (received {name}/{interrupted_signal})"
    if return_code < 0:
        child_signal = -return_code
        try:
            name = signal.Signals(child_signal).name
        except ValueError:
            name = "signal"
        return f"failed ({name}/{child_signal})"
    if return_code == 0:
        return "passed (exit code 0)"
    return f"failed (exit code {return_code})"


def _print_report(
    *,
    command: Sequence[str],
    return_code: int,
    duration: float,
    log_path: Path,
    tracker: OutputTracker,
    timed_out: bool,
    interrupted_signal: int | None,
) -> None:
    success = return_code == 0 and not timed_out and interrupted_signal is None
    lines = [
        "Tobkiri compact test runner",
        f"command: {_command_label(command)}",
        f"status: {_format_status(return_code, timed_out=timed_out, interrupted_signal=interrupted_signal)}",
        f"duration: {duration:.2f}s",
        f"summary: {tracker.summary()}",
        f"full log: {log_path}",
    ]
    if not success:
        excerpt = tracker.failure_excerpt()
        if excerpt:
            lines.append("failure excerpt:")
            lines.extend(excerpt[: MAX_FAILURE_LINES - len(lines)])
    limit = MAX_SUCCESS_LINES if success else MAX_FAILURE_LINES
    print("\n".join(lines[:limit]), flush=True)


def _parse_arguments(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" not in argv:
        raise ValueError("the child command must follow --")
    separator = argv.index("--")
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--timeout-seconds", type=float)
    arguments = parser.parse_args(argv[:separator])
    command = list(argv[separator + 1 :])
    if not command:
        raise ValueError("the child command is empty")
    if arguments.timeout_seconds is not None and arguments.timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    return arguments, command


def run(arguments: argparse.Namespace, command: list[str]) -> int:
    """Run one child command, persist its full log, and return its status."""
    log_path, log_handle = _open_log(arguments.log_dir, arguments.log_file)
    tracker = OutputTracker()
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            cwd=arguments.cwd,
            env=_child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        diagnostic = f"runner: child could not be started ({type(error).__name__})\n"
        encoded = diagnostic.encode("utf-8")
        log_handle.write(encoded)
        tracker.feed(encoded)
        tracker.finish()
        log_handle.close()
        return_code = 127 if isinstance(error, FileNotFoundError) else 126
        _print_report(
            command=command,
            return_code=return_code,
            duration=time.monotonic() - started,
            log_path=log_path,
            tracker=tracker,
            timed_out=False,
            interrupted_signal=None,
        )
        return return_code

    assert proc.stdout is not None
    reader_error: list[BaseException] = []

    def copy_output() -> None:
        try:
            while True:
                chunk = proc.stdout.read1(65_536)
                if not chunk:
                    break
                log_handle.write(chunk)
                tracker.feed(chunk)
        except BaseException as error:  # fail closed on storage/read failures
            reader_error.append(error)
        finally:
            try:
                tracker.finish()
            except BaseException as error:
                reader_error.append(error)

    reader = threading.Thread(
        target=copy_output, name="compact-log-writer", daemon=True
    )
    reader.start()
    pending_signal: list[int] = []
    previous_handlers: dict[int, signal.Handlers] = {}

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        if not pending_signal:
            pending_signal.append(signum)
            _terminate_process(proc, signum)

    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    for handled_signal in handled_signals:
        try:
            previous_handlers[handled_signal] = signal.getsignal(handled_signal)
            signal.signal(handled_signal, handle_signal)
        except (OSError, ValueError):
            continue

    timed_out = False
    termination_started: float | None = None
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if reader_error and termination_started is None:
                termination_started = now
                _terminate_process(proc, signal.SIGTERM)
            if pending_signal and termination_started is None:
                termination_started = now
            if (
                arguments.timeout_seconds is not None
                and now - started >= arguments.timeout_seconds
                and termination_started is None
            ):
                timed_out = True
                termination_started = now
                _terminate_process(proc, signal.SIGTERM)
            if termination_started is not None and now - termination_started >= 2.0:
                _kill_process(proc)
            time.sleep(0.02)
    except KeyboardInterrupt:
        if not pending_signal:
            pending_signal.append(signal.SIGINT)
        _terminate_process(proc, signal.SIGINT)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process(proc)
    finally:
        for handled_signal, previous in previous_handlers.items():
            signal.signal(handled_signal, previous)

    return_code = proc.wait()
    reader.join(timeout=5)
    if reader.is_alive():
        _kill_process(proc)
        reader_error.append(RuntimeError("log writer did not finish"))
    try:
        log_handle.close()
    except OSError as error:
        reader_error.append(error)

    duration = time.monotonic() - started
    interrupted_signal = pending_signal[0] if pending_signal else None
    if reader_error:
        return_code = RUNNER_ERROR_EXIT
    _print_report(
        command=command,
        return_code=return_code,
        duration=duration,
        log_path=log_path,
        tracker=tracker,
        timed_out=timed_out,
        interrupted_signal=interrupted_signal,
    )

    if interrupted_signal is not None and os.name == "posix":
        sys.stdout.flush()
        signal.signal(interrupted_signal, signal.SIG_DFL)
        os.kill(os.getpid(), interrupted_signal)
    if timed_out:
        return TIMEOUT_EXIT
    if return_code < 0 and os.name == "posix":
        child_signal = -return_code
        sys.stdout.flush()
        signal.signal(child_signal, signal.SIG_DFL)
        os.kill(os.getpid(), child_signal)
    if interrupted_signal is not None:
        return 128 + interrupted_signal
    return return_code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    _configure_standard_stream(sys.stdout)
    _configure_standard_stream(sys.stderr)
    try:
        arguments, command = _parse_arguments(sys.argv[1:] if argv is None else argv)
        return run(arguments, command)
    except (LogDestinationError, ValueError):
        print(
            "compact runner configuration error: unsafe or invalid request",
            file=sys.stderr,
        )
        return RUNNER_ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
