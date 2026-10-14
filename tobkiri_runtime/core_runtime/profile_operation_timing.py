"""Bounded, payload-free timing for Host Profile control operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from functools import wraps
import logging
import threading
import time
from typing import Any, ParamSpec, TypeVar


logger = logging.getLogger(__name__)
MAX_TIMING_LOG_LINES = 32
_MAX_ELAPSED_MS = 86_400_000
_OPERATION_CLASSES = {
    "profile.read": "profile_read",
    "profile.catalog.read": "profile_catalog_read",
    "profile.change.resolve": "profile_resolve",
    "profile.change.review": "profile_review",
    "profile.change.approve": "profile_approve",
    "profile.change.activate": "profile_activate",
    "catalog.read": "pack_catalog_read",
}
_PHASES = frozenset(
    {
        "dispatch",
        "handler",
        "active_snapshot",
        "artifact_catalog",
        "candidate_resolution",
        "approval_reconfirmation",
    }
)
_TIMEOUT_CODES = frozenset({"TIMEOUT", "timed_out", "pack_control_timeout"})
_CURRENT: ContextVar[_Trace | None] = ContextVar("tobkiri_profile_operation_timing", default=None)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _timeout_code(value: object) -> str:
    """Project only fixed timeout codes, never diagnostic source text."""
    try:
        if isinstance(value, TimeoutError):
            return "TIMEOUT"
        code = value.get("code") if isinstance(value, Mapping) else getattr(value, "code", None)
        if isinstance(code, Enum):
            code = code.value
        if type(code) is str and code in _TIMEOUT_CODES:
            return code
    except Exception:
        pass
    return "none"


class _Trace:
    """One operation-local bounded trace shared by copied worker contexts."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.started = time.monotonic_ns()
        self.lock = threading.Lock()
        self.lines = 0
        self.closed = False
        self.timeout_code = "none"

    def emit(self, phase: str, started: int, *, terminal: bool = False) -> None:
        # Reserve the final line even when nested work reaches the fixed cap.
        with self.lock:
            limit = MAX_TIMING_LOG_LINES if terminal else MAX_TIMING_LOG_LINES - 1
            if self.closed or self.lines >= limit:
                return
            self.lines += 1
            if terminal:
                self.closed = True
            elapsed = min(
                _MAX_ELAPSED_MS,
                max(0, (time.monotonic_ns() - started) // 1_000_000),
            )
            try:
                logger.warning(
                    "profile_operation_timing operation=%s phase=%s elapsed_ms=%d timeout_code=%s",
                    self.operation,
                    phase,
                    elapsed,
                    self.timeout_code,
                )
            except Exception:
                # Observability must not change a control operation's outcome.
                pass

    def observe(self, value: object) -> None:
        code = _timeout_code(value)
        if code != "none":
            with self.lock:
                if not self.closed:
                    self.timeout_code = code


def timed_profile_control_operation(
    operation: Callable[_P, _R],
) -> Callable[_P, _R]:
    """Time an invoke(self, contract_id, operation_id, payload) boundary.

    Only the operation ID selects a fixed class. Arguments and returned
    payloads are never retained; only their fixed timeout code is projected.
    """

    @wraps(operation)
    def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        operation_id: Any = args[2] if len(args) > 2 else kwargs.get("operation_id")
        operation_class = (
            _OPERATION_CLASSES.get(operation_id) if type(operation_id) is str else None
        )
        if operation_class is None:
            return operation(*args, **kwargs)
        trace = _Trace(operation_class)
        token = _CURRENT.set(trace)
        trace.emit("dispatch.start", trace.started)
        try:
            result = operation(*args, **kwargs)
            trace.observe(result)
            return result
        except BaseException as error:
            trace.observe(error)
            raise
        finally:
            trace.emit("dispatch.finish", trace.started, terminal=True)
            _CURRENT.reset(token)

    return invoke


@contextmanager
def profile_operation_phase(phase: str) -> Iterator[None]:
    """Measure a fixed phase only inside an active Profile control operation."""
    trace = _CURRENT.get()
    if trace is None or type(phase) is not str or phase not in _PHASES:
        yield
        return
    started = time.monotonic_ns()
    trace.emit(f"{phase}.start", started)
    try:
        yield
    except BaseException as error:
        trace.observe(error)
        raise
    finally:
        trace.emit(f"{phase}.finish", started)


def timed_profile_reader(
    reader: Callable[[], _R] | None,
    phase: str,
) -> Callable[[], _R] | None:
    """Wrap a supplied reader, preserving the default when it is absent."""
    if reader is None:
        return None

    @wraps(reader)
    def read() -> _R:
        with profile_operation_phase(phase):
            return reader()

    return read
