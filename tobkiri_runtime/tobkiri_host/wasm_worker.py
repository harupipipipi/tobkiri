"""One-request process supervision; not a production Wasm backend.

The caller owns verified launch configuration and admission. This supervisor
does not prove physical memory containment or establish artifact/Authority
identity, and must not make those backend gates true. Only import-free workers
which cannot create descendants are in scope.
"""

from __future__ import annotations

import math
import os
import selectors
import subprocess
import threading
import time
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json, strict_loads

from .errors import ProviderExecutionError

_INPUT_LIMIT = 45 * 1024 * 1024
_OUTPUT_LIMIT = 2 * 1024 * 1024


class ComponentWorker:
    """Own one child until reaped, including after a failed termination attempt.

    ``command`` is trusted supervisor configuration, never guest request data.
    The caller signals cancellation with an Event. After invoke returns or
    raises, close may be retried; it must succeed before releasing admission.
    Calling close concurrently with invoke is not supported.
    """

    def __init__(self, command: tuple[str, ...]) -> None:
        if not command or not all(isinstance(arg, str) and arg for arg in command):
            raise ValueError("Wasm worker command is invalid")
        if not os.path.isabs(command[0]):
            raise ValueError("Wasm worker interpreter must be absolute")
        self._command = tuple(command)
        self._claimed = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        cancelled: threading.Event,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Exchange bounded data; certify direct-child exit before returning."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError("Wasm worker deadline is invalid")
        encoded = canonical_json(dict(request))
        if len(encoded) > _INPUT_LIMIT:
            raise ValueError("Wasm worker input exceeds the limit")
        if not self._claimed.acquire(blocking=False):
            raise ProviderExecutionError("Wasm worker request has already been consumed")
        deadline = time.monotonic() + timeout
        if cancelled.is_set():
            raise ProviderExecutionError("Wasm worker request was cancelled")
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={},
                close_fds=True,
                cwd="/",
                start_new_session=True,
            )
            output = self._exchange(encoded, deadline, cancelled)
        except OSError:
            raise ProviderExecutionError("Wasm worker transport failed") from None
        finally:
            self.close()
        try:
            response = strict_loads(output, max_bytes=_OUTPUT_LIMIT)
        except (ValueError, RecursionError):
            raise ProviderExecutionError("Wasm worker response is invalid") from None
        if (
            not isinstance(response, dict)
            or set(response) != {"status", "data"}
            or response["status"] != "ok"
            or not isinstance(response["data"], dict)
        ):
            raise ProviderExecutionError("Wasm worker request failed")
        if cancelled.is_set():
            raise ProviderExecutionError("Wasm worker request was cancelled")
        return response["data"]

    def _exchange(self, encoded: bytes, deadline: float, cancelled: threading.Event) -> bytes:
        process = self._process
        assert process is not None and process.stdin and process.stdout
        output = bytearray()
        remaining = memoryview(encoded)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if cancelled.is_set():
                    raise ProviderExecutionError("Wasm worker request was cancelled")
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise ProviderExecutionError("Wasm worker deadline exceeded")
                for key, mask in selector.select(min(0.05, budget)):
                    try:
                        if mask & selectors.EVENT_WRITE:
                            count = os.write(key.fd, remaining[:65536])
                            remaining = remaining[count:]
                            if not remaining:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                        else:
                            chunk = os.read(key.fd, min(65536, _OUTPUT_LIMIT + 1 - len(output)))
                            if not chunk:
                                selector.unregister(process.stdout)
                                process.stdout.close()
                            output.extend(chunk)
                            if len(output) > _OUTPUT_LIMIT:
                                raise ProviderExecutionError("Wasm worker output exceeds the limit")
                    except BlockingIOError:
                        continue
                if process.poll() is not None:
                    if process.returncode != 0:
                        raise ProviderExecutionError("Wasm worker exited unsuccessfully")
                    if not selector.get_map():
                        return bytes(output)

    def close(self) -> None:
        """Kill/reap this child; retain its handle if exit is not confirmed.

        A failed close is not permission to release the caller's reservation.
        The caller must retain this object and retry, not reconstruct a PID.
        """
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderExecutionError("Wasm worker termination is unconfirmed") from None
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()
        self._process = None
