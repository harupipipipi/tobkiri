"""Import-free component execution inside a supervised Wasm worker.

This engine is not a production backend registration. Its caller must isolate
compilation in a worker, reserve resources, enforce a wall deadline, and bind
the artifact and request through Authority before invoking guest code.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json, strict_loads

from .errors import InvalidArtifactError, ProviderExecutionError


class PureComponent:
    """One request's private engine, with no WASI or Host capability imports."""

    def __init__(self, binary: bytes, expected_digest: str) -> None:
        from wasmtime import Config, Engine, WasmtimeError
        from wasmtime.component import Component

        if not binary or len(binary) > 32 * 1024 * 1024:
            raise InvalidArtifactError("Wasm artifact size is outside the worker limit")
        actual = "sha256:" + hashlib.sha256(binary).hexdigest()
        if actual != expected_digest:
            raise InvalidArtifactError("Wasm artifact digest mismatch")
        config = Config()
        # Pulley interprets guest instructions without guest native JIT code.
        config.target = "pulley64"
        config.consume_fuel = True
        config.epoch_interruption = True
        self._engine = Engine(config)
        try:
            self._component = Component(self._engine, binary)
        except WasmtimeError:
            raise InvalidArtifactError("Wasm component is invalid") from None
        if self._component.type.imports(self._engine):
            raise InvalidArtifactError("pure Wasm components cannot import capabilities")
        self._cancelled = threading.Event()
        self._claimed = threading.Lock()

    def cancel(self) -> None:
        """Permanently fence this request and interrupt running guest execution."""
        self._cancelled.set()
        self._engine.increment_epoch()

    def invoke(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        fuel: int = 100_000_000,
        memory_bytes: int = 128 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Invoke once with bounded guest memory, instructions, and JSON traffic.

        Limits may be reduced by the supervisor, never increased by a Pack.
        Guest limits do not bound compiler memory or replace worker accounting.
        """
        from wasmtime import Store, Trap, WasmtimeError
        from wasmtime.component import Linker, Variant

        if (
            type(fuel) is not int
            or type(memory_bytes) is not int
            or not 0 < fuel <= 100_000_000
            or not 0 < memory_bytes <= 128 * 1024 * 1024
        ):
            raise ValueError("Wasm invocation limits are invalid")
        if not isinstance(operation_id, str) or not 0 < len(operation_id) <= 1024:
            raise ValueError("Wasm operation identity is invalid")
        encoded_bytes = canonical_json(dict(payload))
        if len(encoded_bytes) > 1024 * 1024:
            raise ValueError("Wasm input exceeds the transport limit")
        encoded = encoded_bytes.decode("utf-8")
        if not self._claimed.acquire(blocking=False):
            raise ProviderExecutionError("Wasm request has already been consumed")
        # The lock is intentionally never released: a guest cannot retain state
        # across requests, and late cancellation cannot interrupt another call.
        store = Store(self._engine)
        store.set_limits(
            memory_size=memory_bytes,
            table_elements=100_000,
            instances=64,
            tables=64,
            memories=1,
        )
        store.set_fuel(fuel)
        store.set_epoch_deadline(1)
        if self._cancelled.is_set():
            raise ProviderExecutionError("Wasm request was cancelled")
        try:
            instance = Linker(self._engine).instantiate(store, self._component)
            function = instance.get_func(store, "invoke")
            if function is None:
                raise InvalidArtifactError("Wasm invoke export is missing")
            result = function(store, operation_id, encoded)
            function.post_return(store)
        except (Trap, WasmtimeError):
            raise ProviderExecutionError("Wasm execution failed or exceeded its limits") from None
        if self._cancelled.is_set():
            raise ProviderExecutionError("Wasm request was cancelled")
        if not isinstance(result, Variant) or result.tag != "ok":
            raise ProviderExecutionError("Wasm component rejected the request")
        if not isinstance(result.payload, str):
            raise ProviderExecutionError("Wasm output exceeds the transport limit")
        try:
            output = strict_loads(result.payload, max_bytes=1024 * 1024)
        except (ValueError, RecursionError):
            raise ProviderExecutionError("Wasm output is not a JSON object") from None
        if not isinstance(output, dict):
            raise ProviderExecutionError("Wasm output is not a JSON object")
        return output


def worker_main() -> int:
    """Execute one framed request in an isolated, non-production worker.

    The supervisor must launch a verified interpreter with isolation enabled,
    empty environment, closed unrelated descriptors and a wall deadline. This
    entry point does not establish artifact authority or a physical memory
    boundary; it must not be registered as a production backend on its own.
    """
    import base64
    import os
    import resource
    import sys

    # Refuse ordinary Python startup: -I prevents cwd/PYTHONPATH/user-site loads.
    # A supervisor supplies verified import roots, never a request-supplied path.
    if not sys.flags.isolated:
        return 2
    raw_rss_limit = os.environ.get("TOBKIRI_WASM_WORKER_RSS_LIMIT_BYTES")
    os.environ.clear()
    try:
        rss_limit = _parse_worker_rss_limit(raw_rss_limit)
        _install_worker_resource_limits(resource, sys.platform)
        request = strict_loads(
            sys.stdin.buffer.read(45 * 1024 * 1024 + 1),
            max_bytes=45 * 1024 * 1024,
        )
        if not isinstance(request, dict) or set(request) != {
            "artifact", "digest", "operation_id", "payload"
        }:
            raise ValueError("invalid worker request")
        if not isinstance(request["artifact"], str):
            raise ValueError("invalid artifact encoding")
        if not isinstance(request["payload"], dict):
            raise ValueError("invalid payload")
        binary = base64.b64decode(request["artifact"], validate=True)
        engine = PureComponent(binary, request["digest"])
        result = engine.invoke(request["operation_id"], request["payload"])
        response = {"status": "ok", "data": result}
        encoded_response = canonical_json(response)
        _enforce_worker_peak_rss(resource, sys.platform, rss_limit)
        exit_code = 0
    except MemoryError:
        encoded_response = canonical_json(
            {"status": "error", "code": "wasm_worker_memory_limit"}
        )
        exit_code = 3
    except (
        ValueError, TypeError, RecursionError, OSError, ImportError,
        InvalidArtifactError, ProviderExecutionError,
    ):
        encoded_response = canonical_json(
            {"status": "error", "code": "wasm_worker_failed"}
        )
        exit_code = 1
    # No guest exception, payload, engine traceback or secret enters diagnostics.
    sys.stdout.buffer.write(encoded_response)
    sys.stdout.buffer.flush()
    return exit_code


def _install_worker_resource_limits(resource: Any, platform: str) -> None:
    """Apply the worker's OS-enforced ceilings before any engine import.

    These limits are installed before the engine is imported or the guest is
    compiled, so they cover the compiler as well as guest execution. Failing
    to install any of them fails closed: the caller reports the worker as
    failed rather than running an unbounded workload.

    Honest boundary per knob:

    - ``RLIMIT_CORE`` 0, ``RLIMIT_CPU`` 10 s, ``RLIMIT_NOFILE`` 64 and
      ``RLIMIT_FSIZE`` 2 MiB are enforced by the kernel on every relevant
      operation: CPU overage dies by SIGXCPU, descriptor exhaustion fails with
      EMFILE, and regular-file writes past the size cap fail with EFBIG
      (accompanied by SIGXFSZ on platforms that deliver it).
    - ``RLIMIT_NPROC`` 0 is installed on macOS only, where it is settable and
      enforced per process: every ``fork``/``posix_spawn`` from this worker
      fails with EAGAIN, so the supervisor's "the worker cannot create
      descendants" scope is real rather than assumed. It is omitted on Linux
      because NPROC there counts threads as well as processes and a zero cap
      would break engine-internal compilation threads; the production cgroup
      v2 ``pids.max`` boundary covers the process tree there instead.
    - No resident-memory rlimit is attempted. On macOS ``setrlimit`` rejects
      ``RLIMIT_AS``, ``RLIMIT_DATA`` and ``RLIMIT_RSS`` for any value (EINVAL),
      so there is no OS hard allocation cap for this process. Physical memory
      is bounded only by the supervisor's sampled supervision (which stops a
      sustained overage but cannot prevent a transient overshoot inside one
      sampling interval) plus the post-completion peak check in
      ``_enforce_worker_peak_rss``; a hard cap requires the Linux cgroup
      controller or a PackVM boundary.
    """
    ceilings = [
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, 10),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_FSIZE, 2 * 1024 * 1024),
    ]
    if platform == "darwin":
        ceilings.append((resource.RLIMIT_NPROC, 0))
    for kind, ceiling in ceilings:
        _, hard = resource.getrlimit(kind)
        limit = ceiling if hard == resource.RLIM_INFINITY else min(ceiling, hard)
        resource.setrlimit(kind, (limit, limit))


def _parse_worker_rss_limit(raw: str | None) -> int | None:
    """Parse the trusted supervisor's optional resident-memory ceiling."""
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal() or raw.startswith("0"):
        raise ValueError("invalid worker resident memory limit")
    limit = int(raw)
    if limit > 2 * 1024 * 1024 * 1024:
        raise ValueError("invalid worker resident memory limit")
    return limit


def _enforce_worker_peak_rss(resource: Any, platform: str, limit: int | None) -> None:
    """Reject a success whose process high-water RSS exceeded its ceiling.

    This is the post-completion complement to the supervisor's live sampling:
    a peak that crossed the ceiling between two samples and subsided before
    the next one is still caught here because the kernel's ``ru_maxrss``
    high-water mark cannot regress. It rejects the result rather than
    interrupting the workload; stopping a live overage is the supervisor's
    sampled-kill path in ``bounded_child_io``.
    """
    if limit is None:
        return
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.startswith("linux"):
        peak *= 1024
    elif platform != "darwin":
        raise OSError("worker peak resident memory is unavailable")
    if peak > limit:
        raise MemoryError("worker peak resident memory exceeds size limit")


if __name__ == "__main__":
    raise SystemExit(worker_main())
