"""One-request process supervision; not a production Wasm backend.

The caller owns verified launch configuration and admission. This supervisor
does not prove physical memory containment or establish artifact/Authority
identity, and must not make those backend gates true. Only import-free workers
which cannot create descendants are in scope.
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json, strict_loads

from .bounded_child_io import communicate_bounded
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
                stderr=subprocess.PIPE,
                env={},
                close_fds=True,
                cwd="/",
                start_new_session=True,
            )
            output = communicate_bounded(
                self._process,
                encoded,
                stdout_limit=_OUTPUT_LIMIT,
                stderr_limit=_OUTPUT_LIMIT,
                timeout=timeout,
                deadline=deadline,
                cancelled=cancelled,
            )
            if self._process.returncode != 0:
                raise ProviderExecutionError("Wasm worker exited unsuccessfully")
        except InterruptedError:
            raise ProviderExecutionError("Wasm worker request was cancelled") from None
        except TimeoutError:
            raise ProviderExecutionError("Wasm worker deadline exceeded") from None
        except ValueError:
            raise ProviderExecutionError("Wasm worker output exceeds the limit") from None
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
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        self._process = None
