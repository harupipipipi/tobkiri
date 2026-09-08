"""Bounded POSIX pipe exchange; process ownership and termination stay external."""

from __future__ import annotations

import math
import os
import selectors
import subprocess
import time


def communicate_bounded(
    process: subprocess.Popen[bytes],
    payload: bytes,
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    deadline: float | None = None,
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
            or any(type(limit) is not int or limit < 0
                   for limit in (stdout_limit, stderr_limit))
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
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
                selector.register(stream, selectors.EVENT_WRITE if name == "stdin"
                                  else selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("child pipe exchange timed out")
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        try:
                            sent += os.write(key.fd, payload[sent:sent + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent == len(payload):
                            selector.unregister(key.fd)
                            assert process.stdin is not None
                            process.stdin.close()
                        continue
                    budget = (stdout_limit - len(output) if key.data == "stdout"
                              else stderr_limit - stderr_bytes)
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("child pipe exchange timed out")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TimeoutError("child pipe exchange timed out") from None
        if time.monotonic() >= deadline:
            raise TimeoutError("child pipe exchange timed out")
        return bytes(output)
    finally:
        for stream in streams:
            if stream is not None:
                stream.close()
