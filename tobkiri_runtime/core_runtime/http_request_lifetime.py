"""Interrupt owned HTTP socket IO without closing a buffered reader concurrently."""

from __future__ import annotations

import math
import socket
import threading
import time
from typing import Callable


class HttpRequestLifetime:
    """Keep DNS, connect, headers and body within one request's original budget.

    DNS itself can outlive the budget. Its caller retains the executing thread
    and must check again before connecting or sending; this class never detaches
    that work or treats a cancellation request as proof of remote cancellation.
    """

    def __init__(
        self, *, timeout: float, deadline: float | None = None,
        cancellation: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        now = clock()
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("HTTP request timeout is invalid")
        self._deadline = (
            min(now + timeout, deadline) if deadline is not None else now + timeout
        )
        if not math.isfinite(self._deadline):
            raise ValueError("HTTP request deadline is invalid")
        self._clock = clock
        self._cancellation = cancellation or threading.Event()
        self._done = threading.Event()
        self._interrupted = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._watcher = threading.Thread(
            target=self._watch, name="tobkiri-http-deadline", daemon=True,
        )
        self.check()
        self._watcher.start()

    def check(self) -> None:
        """Reject expired or cancelled work before its next external effect."""
        if self._clock() >= self._deadline:
            raise TimeoutError("HTTP request deadline elapsed")
        if (
            self._done.is_set()
            or self._interrupted.is_set()
            or self._cancellation.is_set()
        ):
            raise InterruptedError("HTTP request cancelled")

    def remaining(self) -> float:
        """Return the remaining socket timeout without refreshing the budget."""
        self.check()
        return max(0.001, self._deadline - self._clock())

    def attach(self, owned_socket: socket.socket) -> None:
        """Retain the actual TCP/TLS socket even if HTTPConnection drops it."""
        with self._lock:
            self.check()
            self._socket = owned_socket

    def close(self) -> None:
        """Join only the watchdog; the IO caller closes its own response/connection."""
        self._done.set()
        self._watcher.join()
        with self._lock:
            self._socket = None

    def _watch(self) -> None:
        while not self._done.wait(0.025):
            if self._cancellation.is_set() or self._clock() >= self._deadline:
                with self._lock:
                    self._interrupted.set()
                    if self._socket is not None:
                        try:
                            self._socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                return
