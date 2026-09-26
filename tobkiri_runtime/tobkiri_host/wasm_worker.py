"""One-request process supervision; not a production Wasm backend.

The caller owns verified launch configuration and admission. This supervisor
does not prove physical memory containment or establish artifact/Authority
identity, and must not make those backend gates true. Only import-free workers
are in scope; their no-descendants boundary is enforced worker-side by
``RLIMIT_NPROC=0`` on macOS and by the production cgroup ``pids.max`` on
Linux, with ``close()`` stopping the whole session group as a second layer.
On macOS a ``MemoryGuardConfig`` may additionally inject the sealed
``worker_memory_guard`` dylib: a userspace committed-bytes cap enforced at
every mediated commit call site (see ``worker_memory_guard.c`` for its honest
boundary — it is not a kernel physical-footprint limit).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json, strict_loads

from .bounded_child_io import communicate_bounded
from .errors import ProviderExecutionError
from .resource_controller import ResourceControllerLease, WorkerResourceController

_INPUT_LIMIT = 45 * 1024 * 1024
_OUTPUT_LIMIT = 2 * 1024 * 1024
_DEFAULT_RSS_LIMIT = 512 * 1024 * 1024
_RSS_LIMIT_ENV = "TOBKIRI_WASM_WORKER_RSS_LIMIT_BYTES"
_GUARD_CAP_ENV = "TOBKIRI_WASM_GUARD_CAP_BYTES"
_GUARD_HEADROOM_ENV = "TOBKIRI_WASM_GUARD_HEADROOM_BYTES"
_GUARD_MAX_BYTES = 16 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class MemoryGuardConfig:
    """Trusted launch-time committed-bytes guard for one worker process.

    ``library_path`` must be a sealed build of ``worker_memory_guard.c``
    (trusted supervisor configuration, never request data). ``cap_bytes``
    bounds every mediated memory commit — interpreter, Wasmtime compiler and
    guest linear-memory commits — for the worker's whole lifetime; a commit
    past it fails at the call site. ``headroom_bytes`` optionally lets the
    worker tighten the cap to ``used + headroom`` after compilation, so the
    guest phase cannot grow committed bytes beyond its budget.
    """

    library_path: str
    cap_bytes: int
    headroom_bytes: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.library_path, str)
            or not os.path.isabs(self.library_path)
            or ":" in self.library_path
        ):
            raise ValueError("memory guard library path is invalid")
        candidate = Path(self.library_path)
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("memory guard library is unavailable")
        if (
            type(self.cap_bytes) is not int
            or not 0 < self.cap_bytes <= _GUARD_MAX_BYTES
        ):
            raise ValueError("memory guard committed cap is invalid")
        if self.headroom_bytes is not None and (
            type(self.headroom_bytes) is not int
            or not 0 < self.headroom_bytes <= _GUARD_MAX_BYTES
        ):
            raise ValueError("memory guard headroom is invalid")


class ComponentWorker:
    """Own one child until reaped, including after a failed termination attempt.

    ``command`` is trusted supervisor configuration, never guest request data.
    The caller signals cancellation with an Event. After invoke returns or
    raises, close may be retried; it must succeed before releasing admission.
    Calling close concurrently with invoke is not supported.
    """

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        rss_limit: int = _DEFAULT_RSS_LIMIT,
        resource_controller: WorkerResourceController | None = None,
        memory_guard: MemoryGuardConfig | None = None,
    ) -> None:
        if not command or not all(isinstance(arg, str) and arg for arg in command):
            raise ValueError("Wasm worker command is invalid")
        if not os.path.isabs(command[0]):
            raise ValueError("Wasm worker interpreter must be absolute")
        if type(rss_limit) is not int or not 0 < rss_limit <= 2 * 1024 * 1024 * 1024:
            raise ValueError("Wasm worker resident memory limit is invalid")
        if memory_guard is not None and type(memory_guard) is not MemoryGuardConfig:
            raise ValueError("Wasm worker memory guard config is invalid")
        self._command = tuple(command)
        self._rss_limit = rss_limit
        self._resource_controller = resource_controller
        self._memory_guard = memory_guard
        self._controller_lease: ResourceControllerLease | None = None
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
            raise ProviderExecutionError(
                "Wasm worker request has already been consumed"
            )
        deadline = time.monotonic() + timeout
        if cancelled.is_set():
            raise ProviderExecutionError("Wasm worker request was cancelled")
        try:
            controller_lease = (
                None
                if self._resource_controller is None
                else self._resource_controller.prepare(self._rss_limit)
            )
            self._controller_lease = controller_lease
            env = {_RSS_LIMIT_ENV: str(self._rss_limit)}
            if self._memory_guard is not None:
                guard = self._memory_guard
                # Trusted supervisor configuration, not request or ambient
                # state: dyld injects the sealed guard dylib at exec and the
                # guard reads its committed cap at load. worker_main verifies
                # the marker before trusting the cap and arms the headroom
                # after compilation, so an injection failure fails closed.
                env["DYLD_INSERT_LIBRARIES"] = guard.library_path
                env[_GUARD_CAP_ENV] = str(guard.cap_bytes)
                if guard.headroom_bytes is not None:
                    env[_GUARD_HEADROOM_ENV] = str(guard.headroom_bytes)
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # This is trusted supervisor configuration, not request or
                # ambient process state. worker_main consumes it before
                # clearing its environment and checks the OS high-water RSS.
                env=env,
                close_fds=True,
                cwd="/",
                start_new_session=True,
                preexec_fn=(
                    None if controller_lease is None else controller_lease.child_setup
                ),
            )
            output = communicate_bounded(
                self._process,
                encoded,
                stdout_limit=_OUTPUT_LIMIT,
                stderr_limit=_OUTPUT_LIMIT,
                timeout=timeout,
                deadline=deadline,
                cancelled=cancelled,
                rss_limit=self._rss_limit,
            )
            if self._process.returncode == 3:
                raise MemoryError("Wasm worker reported a peak RSS violation")
            if self._process.returncode != 0:
                raise ProviderExecutionError("Wasm worker exited unsuccessfully")
        except InterruptedError:
            raise ProviderExecutionError("Wasm worker request was cancelled") from None
        except TimeoutError:
            raise ProviderExecutionError("Wasm worker deadline exceeded") from None
        except MemoryError:
            raise ProviderExecutionError("Wasm worker memory limit exceeded") from None
        except ValueError:
            raise ProviderExecutionError(
                "Wasm worker output exceeds the limit"
            ) from None
        except (OSError, subprocess.SubprocessError):
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
        if time.monotonic() >= deadline:
            raise ProviderExecutionError("Wasm worker deadline exceeded")
        return response["data"]

    def close(self) -> None:
        """Kill/reap this child; retain its handle if exit is not confirmed.

        A failed close is not permission to release the caller's reservation.
        The caller must retain this object and retry, not reconstruct a PID.
        """
        process = self._process
        if process is not None:
            try:
                if process.poll() is None:
                    _kill_worker_group(process)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                raise ProviderExecutionError(
                    "Wasm worker termination is unconfirmed"
                ) from None
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            self._process = None
        controller_lease = self._controller_lease
        if controller_lease is not None:
            try:
                controller_lease.close()
            except OSError:
                raise ProviderExecutionError(
                    "Wasm worker resource-controller release is unconfirmed"
                ) from None
            self._controller_lease = None


def _kill_worker_group(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the worker's whole session group, not only its leader PID.

    The child is launched with ``start_new_session=True``, so while it is
    alive ``process.pid`` is still allocated and is a live process-group id:
    ``killpg`` cannot hit a recycled group here. This bounds a stop to every
    process the worker may have created before the worker-side
    ``RLIMIT_NPROC=0`` boundary (or Linux ``pids.max``) took effect, falling
    back to the leader alone where ``killpg`` is unavailable or the group is
    already gone. A group kill is a *stop* mechanism; it does not by itself
    prevent a worker from creating descendants.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        process.kill()
