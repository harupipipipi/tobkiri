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
    raw_guard_cap = os.environ.get("TOBKIRI_WASM_GUARD_CAP_BYTES")
    raw_guard_headroom = os.environ.get("TOBKIRI_WASM_GUARD_HEADROOM_BYTES")
    os.environ.clear()
    try:
        rss_limit = _parse_worker_rss_limit(raw_rss_limit)
        guard_headroom = _verify_worker_memory_guard(
            raw_guard_cap, raw_guard_headroom
        )
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
        # Compilation is covered by the guard's lifetime committed cap; the
        # optional headroom tightens the same cap for the guest phase.
        _arm_worker_memory_guard(guard_headroom)
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
    - ``RLIMIT_CPU`` keeps a one-second soft/hard gap. The kernel raises
      SIGXCPU at the soft bound and SIGKILL only at the hard bound, so an
      equal pair would queue both signals in the same check and SIGKILL
      dequeues first on Linux — the worker would die by SIGKILL instead
      of the SIGXCPU stop documented above. The hard bound one second
      later still guarantees a SIGKILL stop if SIGXCPU is caught,
      blocked, or ignored.
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
    # Entries are (kind, soft ceiling, hard ceiling); only RLIMIT_CPU
    # carries distinct bounds, for the SIGXCPU-before-SIGKILL ordering
    # documented above.
    ceilings = [
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_CPU, 10, 11),
        (resource.RLIMIT_NOFILE, 64, 64),
        (resource.RLIMIT_FSIZE, 2 * 1024 * 1024, 2 * 1024 * 1024),
    ]
    if platform == "darwin":
        ceilings.append((resource.RLIMIT_NPROC, 0, 0))
    for kind, soft_ceiling, hard_ceiling in ceilings:
        _, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY:
            # An external hard cap is never raised; squeeze the soft
            # bound under it so a designed soft/hard gap still holds.
            gap = hard_ceiling - soft_ceiling
            hard_ceiling = min(hard_ceiling, hard)
            soft_ceiling = min(soft_ceiling, max(hard_ceiling - gap, 0))
        resource.setrlimit(kind, (soft_ceiling, hard_ceiling))


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


_GUARD_MARKER = 0x54424731
_GUARD_MAX_BYTES = 16 * 1024 * 1024 * 1024


def _parse_guard_bytes(raw: str | None, label: str) -> int | None:
    """Parse one trusted supervisor guard knob as strict decimal bytes."""
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal() or raw.startswith("0"):
        raise ValueError(f"invalid worker memory guard {label}")
    value = int(raw)
    if value > _GUARD_MAX_BYTES:
        raise ValueError(f"invalid worker memory guard {label}")
    return value


def _verify_worker_memory_guard(
    raw_cap: str | None, raw_headroom: str | None
) -> int | None:
    """Validate the supervisor's guard configuration and prove it is loaded.

    The guard is a sealed ``worker_memory_guard`` dylib the supervisor
    injects via ``DYLD_INSERT_LIBRARIES``. If the supervisor asked for a cap
    or headroom but the marker symbol is absent — an unsigned library
    rejected by a hardened interpreter, a missing file, or a mismatched
    binary — the worker must not run unguarded, so this fails closed.
    """
    if raw_cap is None and raw_headroom is None:
        return None
    _parse_guard_bytes(raw_cap, "cap")
    headroom = _parse_guard_bytes(raw_headroom, "headroom")
    import ctypes

    try:
        marker = ctypes.c_uint32.in_dll(
            ctypes.CDLL(None), "tobkiri_wasm_guard_marker"
        ).value
    except (ValueError, OSError):
        marker = None
    if marker != _GUARD_MARKER:
        raise OSError("the worker memory guard library is not loaded")
    return headroom


def _arm_worker_memory_guard(headroom: int | None) -> None:
    """Tighten the loaded guard to ``used + headroom`` for the guest phase.

    Compilation already ran under the lifetime cap; this bound lets the
    guest grow committed bytes by only its declared budget. A missing or
    refusing guard entry point fails closed.
    """
    if headroom is None:
        return
    import ctypes

    try:
        arm = ctypes.CDLL(None).tobkiri_wasm_guard_arm_headroom
        arm.argtypes = (ctypes.c_uint64,)
        arm.restype = ctypes.c_uint64
    except (AttributeError, OSError):
        raise OSError("worker memory guard headroom control is missing") from None
    if arm(headroom) == 0:
        raise OSError("worker memory guard refused the headroom")


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
