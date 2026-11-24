"""Single-use loopback handoff for a descriptor-pinned PackVM image."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import selectors
import socket
import stat
import threading
import time
from collections import deque
from typing import Callable


_MAX_HEADER_BYTES = 16 * 1024
_MAX_PENDING_CONNECTIONS = 64
_HEADER_TIMEOUT_SECONDS = 0.5
_ACCEPT_QUOTA = 8
_HEADER_READ_QUOTA = 16


class PackVMImageHandoffError(RuntimeError):
    """The local image consumer did not receive the exact pinned bytes."""


class PackVMLoopbackImageHandoff:
    """Serve one exact GET through a bounded, selector-driven loopback endpoint."""

    def __init__(
        self,
        descriptor: int,
        *,
        size_bytes: int,
        digest: str,
        cancelled: Callable[[], bool] | None = None,
        overall_timeout_seconds: float = 900.0,
        inactivity_timeout_seconds: float = 30.0,
    ) -> None:
        if descriptor < 0 or size_bytes <= 0:
            raise ValueError("PackVM image handoff descriptor is invalid")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError("PackVM image handoff digest is invalid")
        self._descriptor = os.dup(descriptor)
        metadata = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 0
            or metadata.st_size != size_bytes
        ):
            os.close(self._descriptor)
            raise ValueError("PackVM image handoff inode is not sealed")
        self._size = size_bytes
        self._digest = digest
        self._cancelled = cancelled
        self._overall_timeout = overall_timeout_seconds
        self._inactivity_timeout = inactivity_timeout_seconds
        self._token = secrets.token_hex(32)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._clients: set[socket.socket] = set()
        self._active: socket.socket | None = None
        self._claimed = False
        self._consumed = False
        self._error: BaseException | None = None
        self._deadline = 0.0

    @property
    def url(self) -> str:
        """Return the active loopback URL containing the single-use token."""

        listener = self._listener
        if listener is None:
            raise PackVMImageHandoffError("PackVM image handoff is not active")
        return f"http://127.0.0.1:{listener.getsockname()[1]}{self._expected_path}"

    @property
    def sensitive_values(self) -> tuple[str, str]:
        """Return exact ephemeral values that must never enter durable diagnostics."""

        return self.url, self._token

    @property
    def _expected_path(self) -> str:
        return f"/packvm-image/{self._token}"

    def __enter__(self) -> PackVMLoopbackImageHandoff:
        """Bind loopback and start the bounded header/event loop."""

        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind(("127.0.0.1", 0))
            listener.listen(_MAX_PENDING_CONNECTIONS)
            listener.setblocking(False)
            self._listener = listener
            self._deadline = time.monotonic() + self._overall_timeout
            self._thread = threading.Thread(
                target=self._serve,
                name="packvm-image-handoff",
                daemon=True,
            )
            self._thread.start()
            return self
        except Exception:
            self._stop.set()
            if listener is not None:
                listener.close()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
            self._listener = None
            self._thread = None
            os.close(self._descriptor)
            raise

    def __exit__(self, *_exc: object) -> None:
        """Close every socket and return within a fixed bound on every path."""

        self._stop.set()
        listener = self._listener
        if listener is not None:
            listener.close()
        with self._lock:
            sockets = tuple(self._clients) + ((self._active,) if self._active is not None else ())
        for client in sockets:
            self._close_socket(client)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        alive = thread is not None and thread.is_alive()
        os.close(self._descriptor)
        self._listener = None
        self._thread = None
        if alive:
            raise PackVMImageHandoffError(
                "PackVM image handoff did not stop within its close bound"
            )

    def require_consumed(self) -> None:
        """Fail unless exactly one complete, digest-matching GET finished."""

        self._finished.wait(timeout=1.0)
        with self._lock:
            error = self._error
            consumed = self._consumed
        if error is not None:
            raise PackVMImageHandoffError("PackVM local image handoff failed") from error
        if not consumed:
            raise PackVMImageHandoffError("Lima did not consume the complete PackVM local image")

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        selector = selectors.DefaultSelector()
        pending: dict[socket.socket, tuple[bytearray, float]] = {}
        ready_clients: deque[socket.socket] = deque()
        queued_clients: set[socket.socket] = set()
        try:
            selector.register(listener, selectors.EVENT_READ)
            while not self._stop.is_set():
                if self._cancelled is not None and self._cancelled():
                    self._record_error(InterruptedError("PackVM local image handoff was cancelled"))
                    return
                now = time.monotonic()
                if now >= self._deadline:
                    self._record_error(TimeoutError("PackVM image handoff timed out"))
                    return
                timeout = min(0.1, self._deadline - now)
                listener_ready = False
                for key, _events in selector.select(timeout):
                    if key.fileobj is listener:
                        listener_ready = True
                    else:
                        client = key.fileobj
                        if (
                            isinstance(client, socket.socket)
                            and client in pending
                            and client not in queued_clients
                        ):
                            ready_clients.append(client)
                            queued_clients.add(client)
                for _ in range(min(_HEADER_READ_QUOTA, len(ready_clients))):
                    client = ready_clients.popleft()
                    queued_clients.discard(client)
                    if client in pending:
                        self._read_header(selector, pending, client)
                if listener_ready:
                    self._accept_ready(selector, pending)
                now = time.monotonic()
                for client, (_buffer, deadline) in tuple(pending.items()):
                    if now >= deadline:
                        self._drop_pending(selector, pending, client)
        except (OSError, ValueError) as exc:
            if not self._stop.is_set():
                self._record_error(exc)
        finally:
            for client in tuple(pending):
                self._drop_pending(selector, pending, client)
            try:
                selector.unregister(listener)
            except (KeyError, ValueError):
                pass
            selector.close()

    def _accept_ready(
        self,
        selector: selectors.BaseSelector,
        pending: dict[socket.socket, tuple[bytearray, float]],
    ) -> None:
        listener = self._listener
        if listener is None:
            return
        for _ in range(_ACCEPT_QUOTA):
            if len(pending) >= _MAX_PENDING_CONNECTIONS:
                return
            try:
                client, address = listener.accept()
            except BlockingIOError:
                return
            if address[0] != "127.0.0.1":
                self._close_socket(client)
                continue
            # Apply a kernel socket timeout immediately.  The selector deadline
            # below is authoritative and also bounds partial-header trickles.
            client.settimeout(_HEADER_TIMEOUT_SECONDS)
            pending[client] = (bytearray(), time.monotonic() + _HEADER_TIMEOUT_SECONDS)
            with self._lock:
                self._clients.add(client)
            selector.register(client, selectors.EVENT_READ)

    def _read_header(
        self,
        selector: selectors.BaseSelector,
        pending: dict[socket.socket, tuple[bytearray, float]],
        client: socket.socket,
    ) -> None:
        try:
            chunk = client.recv(4096)
        except (BlockingIOError, TimeoutError):
            return
        if not chunk:
            self._drop_pending(selector, pending, client)
            return
        buffer, deadline = pending[client]
        buffer.extend(chunk)
        if len(buffer) > _MAX_HEADER_BYTES:
            self._reject_pending(selector, pending, client, 431)
            return
        if b"\r\n\r\n" not in buffer:
            pending[client] = (buffer, deadline)
            return
        request = bytes(buffer)
        self._detach_pending(selector, pending, client)
        if not self._valid_request(request):
            self._respond_and_close(client, 403)
            return
        with self._lock:
            if self._claimed:
                claimed = True
            else:
                self._claimed = True
                self._active = client
                claimed = False
        if claimed:
            self._respond_and_close(client, 410)
            return
        self._stream(client)
        with self._lock:
            self._active = None
        self._close_socket(client)

    def _valid_request(self, request: bytes) -> bool:
        try:
            head, remainder = request.split(b"\r\n\r\n", 1)
            lines = head.decode("ascii").split("\r\n")
        except (UnicodeDecodeError, ValueError):
            return False
        if remainder or lines[0] != f"GET {self._expected_path} HTTP/1.1":
            return False
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line or line[0] in " \t" or ":" not in line:
                return False
            name, value = line.split(":", 1)
            if not name or not name.replace("-", "").isalnum():
                return False
            headers.setdefault(name.casefold(), []).append(value.strip())
        listener = self._listener
        if listener is None:
            return False
        expected_host = f"127.0.0.1:{listener.getsockname()[1]}"
        return (
            headers.get("host") == [expected_host]
            and "range" not in headers
            and headers.get("content-length", ["0"]) == ["0"]
            and "transfer-encoding" not in headers
        )

    def _stream(self, client: socket.socket) -> None:
        try:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("PackVM local image handoff timed out")
            client.settimeout(min(self._inactivity_timeout, remaining))
            response = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {self._size}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Cache-Control: no-store\r\n"
                "Accept-Ranges: none\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            client.sendall(response)
            if time.monotonic() >= self._deadline:
                raise TimeoutError("PackVM local image handoff timed out")
            hasher = hashlib.sha256()
            offset = 0
            while offset < self._size:
                if self._cancelled is not None and self._cancelled():
                    raise InterruptedError("PackVM local image handoff was cancelled")
                if time.monotonic() >= self._deadline:
                    raise TimeoutError("PackVM local image handoff timed out")
                chunk = os.pread(self._descriptor, min(64 * 1024, self._size - offset), offset)
                if not chunk:
                    raise EOFError("PackVM local image handoff was truncated")
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("PackVM local image handoff timed out")
                client.settimeout(min(self._inactivity_timeout, remaining))
                client.sendall(chunk)
                if time.monotonic() >= self._deadline:
                    raise TimeoutError("PackVM local image handoff timed out")
                hasher.update(chunk)
                offset += len(chunk)
            if os.pread(self._descriptor, 1, offset):
                raise ValueError("PackVM local image handoff overran signed size")
            actual = "sha256:" + hasher.hexdigest()
            if not hmac.compare_digest(actual, self._digest):
                raise ValueError("PackVM local image handoff digest changed")
            if time.monotonic() >= self._deadline:
                raise TimeoutError("PackVM local image handoff timed out")
            with self._lock:
                self._consumed = True
            self._finished.set()
        except Exception as exc:
            self._record_error(exc)

    def _record_error(self, error: BaseException) -> None:
        with self._lock:
            if self._error is None and not self._consumed:
                self._error = error
        self._finished.set()

    def _reject_pending(
        self,
        selector: selectors.BaseSelector,
        pending: dict[socket.socket, tuple[bytearray, float]],
        client: socket.socket,
        status: int,
    ) -> None:
        self._detach_pending(selector, pending, client)
        self._respond_and_close(client, status)

    def _drop_pending(
        self,
        selector: selectors.BaseSelector,
        pending: dict[socket.socket, tuple[bytearray, float]],
        client: socket.socket,
    ) -> None:
        self._detach_pending(selector, pending, client)
        self._close_socket(client)

    def _detach_pending(
        self,
        selector: selectors.BaseSelector,
        pending: dict[socket.socket, tuple[bytearray, float]],
        client: socket.socket,
    ) -> None:
        pending.pop(client, None)
        try:
            selector.unregister(client)
        except (KeyError, ValueError):
            pass
        with self._lock:
            self._clients.discard(client)

    def _respond_and_close(self, client: socket.socket, status: int) -> None:
        reasons = {403: "Forbidden", 410: "Gone", 431: "Request Header Fields Too Large"}
        try:
            client.settimeout(0.2)
            client.sendall(
                (
                    f"HTTP/1.1 {status} {reasons[status]}\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            )
        except OSError:
            pass
        self._close_socket(client)

    @staticmethod
    def _close_socket(client: socket.socket) -> None:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()
