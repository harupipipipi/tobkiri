"""Host-only, retryable completion of exact request resource ownership."""

from __future__ import annotations

from collections.abc import Callable
import threading


class RequestResourceDrain:
    """Run each successful cleanup stage once, retaining failed stages for retry.

    The Broker marks this object ready only after the exact submitted Future
    exits. A callback must be idempotent if it can fail after a partial change.
    Neither this handle nor its callbacks are part of a Pack request payload.
    """

    def __init__(self, completed: Callable[[], None] | None) -> None:
        self._lock = threading.RLock()
        self._completed = completed
        self._cleanup: Callable[[], None] | None = None
        self._owned = False
        self._ready = False
        self._cleaned = False
        self._finished = False
        self._error: Exception | None = None

    def handoff(self, cleanup: Callable[[], None]) -> None:
        """Transfer the exact admitted resources to their actual-drain gate."""
        with self._lock:
            if self._owned or self._finished:
                raise RuntimeError("request drain ownership was already transferred")
            self._owned = True
            self._cleanup = cleanup

    @property
    def ready(self) -> bool:
        """Return whether retry cannot race a still-running Provider."""
        with self._lock:
            return self._ready and not self._finished

    @property
    def finished(self) -> bool:
        """Return whether all resources and the owner reference were released."""
        with self._lock:
            return self._finished

    def finish_unadmitted(self) -> None:
        """Complete failures before resources were handed to the Broker."""
        with self._lock:
            if not self._owned:
                self.run()

    def run(self) -> None:
        """Mark verified drain and finish, or retain a visible retryable failure."""
        with self._lock:
            self._ready = True
            if self._finished:
                return
            try:
                if not self._cleaned:
                    if self._cleanup is not None:
                        self._cleanup()
                    self._cleaned = True
                if self._completed is not None:
                    self._completed()
                self._finished = True
                self._error = None
            except Exception as error:
                self._error = error
                raise
