"""Bounded POSIX pipe exchange; process ownership and termination stay external."""

from __future__ import annotations

import math
import os
import selectors
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path


_SUPERVISION_INTERVAL_SECONDS = 0.001


def communicate_bounded(
    process: subprocess.Popen[bytes],
    payload: bytes,
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    deadline: float | None = None,
    cancelled: threading.Event | None = None,
    rss_limit: int | None = None,
) -> bytes:
    """Drain both pipes within one deadline without buffering unbounded output.

    The caller must terminate and reap the owned process on failure. Stderr is
    counted but never retained or returned, since it is untrusted diagnostic data.
    Pipes are closed on every exit, including timeout and over-limit failures.
    """
    streams = (process.stdin, process.stdout, process.stderr)
    try:
        if (
            any(stream is None for stream in streams)
            or type(payload) is not bytes
            or any(type(limit) is not int or limit < 0 for limit in (stdout_limit, stderr_limit))
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or (rss_limit is not None and (type(rss_limit) is not int or rss_limit <= 0))
        ):
            raise ValueError("child pipe exchange configuration is invalid")
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError("child pipe deadline is invalid")
        step_deadline = time.monotonic() + timeout
        deadline = step_deadline if deadline is None else min(deadline, step_deadline)
        output = bytearray()
        stderr_bytes = 0
        sent = 0
        with selectors.DefaultSelector() as selector:
            for stream, name in zip(streams, ("stdin", "stdout", "stderr")):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                if name == "stdin" and not payload:
                    stream.close()
                    continue
                selector.register(
                    stream, selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ, name
                )
            while selector.get_map():
                _enforce_rss_limit(process, rss_limit)
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("child pipe exchange cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("child pipe exchange timed out")
                interval = remaining
                if cancelled is not None or rss_limit is not None:
                    interval = min(_SUPERVISION_INTERVAL_SECONDS, remaining)
                for key, _ in selector.select(interval):
                    if key.data == "stdin":
                        try:
                            sent += os.write(key.fd, payload[sent : sent + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent == len(payload):
                            selector.unregister(key.fd)
                            assert process.stdin is not None
                            process.stdin.close()
                        continue
                    budget = (
                        stdout_limit - len(output)
                        if key.data == "stdout"
                        else stderr_limit - stderr_bytes
                    )
                    try:
                        chunk = os.read(key.fd, min(65536, budget + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    if len(chunk) > budget:
                        raise ValueError("child output exceeds size limit")
                    if key.data == "stdout":
                        output.extend(chunk)
                    else:
                        stderr_bytes += len(chunk)
        while True:
            _enforce_rss_limit(process, rss_limit)
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("child pipe exchange cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("child pipe exchange timed out")
            try:
                interval = remaining
                if cancelled is not None or rss_limit is not None:
                    interval = min(_SUPERVISION_INTERVAL_SECONDS, remaining)
                process.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                if cancelled is None and rss_limit is None:
                    raise TimeoutError("child pipe exchange timed out") from None
        if time.monotonic() >= deadline:
            raise TimeoutError("child pipe exchange timed out")
        return bytes(output)
    finally:
        for stream in streams:
            if stream is not None:
                stream.close()


def _enforce_rss_limit(
    process: subprocess.Popen[bytes],
    rss_limit: int | None,
) -> None:
    """Fail closed when the supervised direct child exceeds resident memory."""
    if rss_limit is None:
        return
    try:
        resident = _resident_bytes(process.pid)
    except ProcessLookupError:
        # The OS may report that a short-lived child is gone just before
        # Popen.poll() reaps and records its status. There is no live resident
        # allocation left to supervise; pipe and exit-status validation below
        # still decide whether the exchange succeeded.
        return
    if resident > rss_limit:
        raise MemoryError("child resident memory exceeds size limit")


def _resident_bytes(pid: int) -> int:
    """Read current direct-child RSS without spawning an unbounded helper."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("child process identity is invalid")
    if sys.platform == "darwin":
        import ctypes

        # PROC_PIDTASKINFO starts with virtual_size and resident_size. Allocate
        # a full, forward-compatible buffer and consume only those stable fields.
        buffer = ctypes.create_string_buffer(256)
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = library.proc_pidinfo
        proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        proc_pidinfo.restype = ctypes.c_int
        received = proc_pidinfo(pid, 4, 0, buffer, len(buffer))
        if received < 16:
            error = ctypes.get_errno()
            if error == 3:
                raise ProcessLookupError(pid)
            raise OSError(error, "could not read child resident memory")
        _, resident = struct.unpack_from("=QQ", buffer.raw)
        return resident
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
        except FileNotFoundError:
            raise ProcessLookupError(pid) from None
        if len(fields) < 2 or not fields[1].isdigit():
            raise OSError("could not read child resident memory")
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    raise OSError("resident memory supervision is unavailable")
