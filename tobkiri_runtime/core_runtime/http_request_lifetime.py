"""Interrupt owned HTTP socket IO without closing a buffered reader concurrently."""

from __future__ import annotations

import errno
import io
import math
import select
import socket
import ssl
import threading
import time
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


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
        authority_check: Callable[[], bool] | None = None,
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
        self._authority_check = authority_check
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
        self._check_signal()
        if not self._authority_active():
            raise InterruptedError("HTTP request cancelled")

    def _check_signal(self) -> None:
        """Check cheap request fences below buffered IO and TLS retries."""
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

    def http_socket(self, owned_socket: socket.socket) -> Any:
        """Keep HTTP's buffered parser above owner-polled, nonblocking IO.

        The returned socket interface defers native close until its reader is
        released, matching socket.makefile ownership. It never exposes polling
        timeouts to a buffered reader or repeats already sent request bytes.
        """
        self.attach(owned_socket)
        owned_socket.setblocking(False)
        return _LifetimeHttpSocket(owned_socket, self)

    def connect(self, owned_socket: socket.socket, address: tuple[str, int]) -> None:
        """Poll one numeric TCP connect in its IO owner's original budget."""
        self.attach(owned_socket)
        owned_socket.setblocking(False)
        result = owned_socket.connect_ex(address)
        if result not in {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR}:
            raise OSError(result, "HTTP connection failed")
        if result:
            self._wait_io(owned_socket, writing=True, connecting=True)
            error = owned_socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise OSError(error, "HTTP connection failed")
        self.check()

    def handshake(self, owned_socket: ssl.SSLSocket) -> None:
        """Complete verified TLS without an uninterruptible buffered wait."""
        self.attach(owned_socket)
        owned_socket.setblocking(False)
        self._socket_call(owned_socket, owned_socket.do_handshake)
        self.check()

    def _socket_call(
        self, peer: socket.socket, operation: Callable[[], _T], *, writing: bool = False,
    ) -> _T:
        while True:
            self._check_signal()
            try:
                return operation()
            except ssl.SSLWantReadError:
                self._wait_io(peer, writing=False)
            except ssl.SSLWantWriteError:
                self._wait_io(peer, writing=True)
            except (BlockingIOError, InterruptedError):
                self._wait_io(peer, writing=writing)

    def _wait_io(
        self, peer: socket.socket, *, writing: bool, connecting: bool = False,
    ) -> None:
        while True:
            self._check_signal()
            remaining = self._deadline - self._clock()
            readable, writable, exceptional = select.select(
                [] if writing else [peer], [peer] if writing else [],
                [peer] if connecting else [],
                min(0.05, max(0.001, remaining)),
            )
            self._check_signal()
            if readable or writable or exceptional:
                return

    def close(self) -> None:
        """Join only the watchdog; the IO caller closes its own response/connection."""
        self._done.set()
        self._watcher.join()
        with self._lock:
            self._socket = None

    def _watch(self) -> None:
        while not self._done.wait(0.025):
            if (
                self._cancellation.is_set()
                or self._clock() >= self._deadline
                or not self._authority_active()
            ):
                with self._lock:
                    self._interrupted.set()
                return

    def _authority_active(self) -> bool:
        if self._authority_check is None:
            return True
        try:
            return self._authority_check() is True
        except Exception:
            return False


class _LifetimeHttpSocket:
    """One IO worker owns the peer, buffered readers and every native close."""

    def __init__(self, peer: socket.socket, lifetime: HttpRequestLifetime) -> None:
        self._peer = peer
        self._lifetime = lifetime
        self._readers = 0
        self._closed = False

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise OSError("HTTP socket is closed")
        remaining = memoryview(data)
        while remaining:
            sent = self._lifetime._socket_call(
                self._peer, lambda: self._peer.send(remaining), writing=True,
            )
            if sent <= 0:
                raise ConnectionError("HTTP socket closed during send")
            remaining = remaining[sent:]

    def makefile(self, mode: str = "rb", buffering: int = -1) -> io.BufferedReader:
        if mode != "rb" or self._closed:
            raise ValueError("HTTP socket reader is unavailable")
        self._readers += 1
        return io.BufferedReader(
            _LifetimeSocketReader(self),
            buffer_size=io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering,
        )

    def recv_into(self, buffer: Any) -> int:
        return self._lifetime._socket_call(self._peer, lambda: self._peer.recv_into(buffer))

    def close(self) -> None:
        self._closed = True
        if self._readers == 0:
            self._peer.close()

    def _release_reader(self) -> None:
        self._readers -= 1
        if self._closed and self._readers == 0:
            self._peer.close()


class _LifetimeSocketReader(io.RawIOBase):
    def __init__(self, owner: _LifetimeHttpSocket) -> None:
        super().__init__()
        self._owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.closed:
            raise ValueError("HTTP socket reader is closed")
        return self._owner.recv_into(buffer)

    def close(self) -> None:
        if not self.closed:
            try:
                super().close()
            finally:
                self._owner._release_reader()
