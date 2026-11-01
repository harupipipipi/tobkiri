"""Keep MCP SSE requests inside the connection's approved HTTP origin."""

from __future__ import annotations

from http.client import HTTPConnection, HTTPMessage, HTTPSConnection
import socket
import threading
from typing import Any, IO
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener,
)


def _response_socket(response: Any) -> socket.socket | None:
    buffer = getattr(response, "fp", None)
    raw = getattr(buffer, "raw", None)
    owned_socket = getattr(raw, "_sock", None)
    return owned_socket if isinstance(owned_socket, socket.socket) else None


def allow_idle_sse_response(response: Any) -> None:
    """Keep a ready event stream open after its bounded HTTP startup finishes."""
    owned_socket = _response_socket(response)
    if owned_socket is not None:
        owned_socket.settimeout(None)


def interrupt_sse_response(response: Any) -> None:
    """Interrupt urllib's socket read without taking its buffered-reader lock.

    CPython HTTPResponse owns a BufferedReader over SocketIO for HTTP and HTTPS.
    If that socket is unavailable, the caller must retain the response until its
    reader finishes; closing a buffer concurrently with read can block forever.
    """
    owned_socket = _response_socket(response)
    if owned_socket is not None:
        try:
            owned_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            # EOF or another concurrent shutdown is harmless. Reader termination
            # remains the caller's responsibility before it releases ownership.
            pass


def _origin(url: str) -> tuple[str, str, int]:
    """Validate a complete HTTP URL without silently discarding characters."""
    if (
        not isinstance(url, str)
        or not url
        or any(ord(character) <= 32 or ord(character) == 127 for character in url)
    ):
        raise ValueError("MCP SSE URL is invalid")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
        port = parsed.port
    except ValueError:
        raise ValueError("MCP SSE URL is invalid") from None
    return (
        parsed.scheme,
        parsed.hostname,
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )


class _NoRedirect(HTTPRedirectHandler):
    """Require a newly reviewed URL instead of replaying an MCP HTTP request."""

    def http_error_302(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage,
    ) -> None:
        fp.close()
        raise PermissionError("MCP SSE redirects require a new connection URL")

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


class _OwnedHTTPConnection(HTTPConnection):
    """Check the owner again after a potentially blocking DNS/connect step."""

    def __init__(self, *args: Any, owner: SseEndpointPolicy, **kwargs: Any) -> None:
        self._owner = owner
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self._owner.assert_open()
        super().connect()
        self._owner.assert_open()

    def send(self, data: Any) -> None:
        self._owner.assert_open()
        super().send(data)


class _OwnedHTTPSConnection(_OwnedHTTPConnection, HTTPSConnection):
    pass


class _OwnedHTTPHandler(HTTPHandler):
    def __init__(self, owner: SseEndpointPolicy) -> None:
        super().__init__()
        self._owner = owner

    def http_open(self, req: Request) -> Any:
        return self.do_open(self._owner.http_connection, req)


class _OwnedHTTPSHandler(HTTPSHandler):
    def __init__(self, owner: SseEndpointPolicy) -> None:
        super().__init__()
        self._owner = owner

    def https_open(self, req: Request) -> Any:
        return self.do_open(self._owner.https_connection, req)


class SseEndpointPolicy:
    """Resolve server-provided endpoints without widening approved authority."""

    def __init__(self, approved_url: str) -> None:
        self._origin = _origin(approved_url)
        self._approved_url = approved_url
        self._lock = threading.Lock()
        self._closed = False
        self._opening: dict[HTTPConnection, int] = {}
        self._opener = build_opener(
            _NoRedirect(), _OwnedHTTPHandler(self), _OwnedHTTPSHandler(self),
        )

    def assert_open(self) -> None:
        """Prevent a delayed opener from sending after its owner was stopped."""
        with self._lock:
            if self._closed:
                raise InterruptedError("MCP SSE connection closed")

    def interrupt(self) -> None:
        """Fence future sends and interrupt socket IO, including response headers.

        DNS resolution may still be pending. The transport retains and joins
        that worker, and its connection checks this fence before sending bytes.
        """
        with self._lock:
            self._closed = True
            for connection in self._opening:
                if connection.sock is not None:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def http_connection(self, *args: Any, **kwargs: Any) -> HTTPConnection:
        """Own an HTTP connection before it can perform blocking IO."""
        return self._connection(_OwnedHTTPConnection(*args, owner=self, **kwargs))

    def https_connection(self, *args: Any, **kwargs: Any) -> HTTPConnection:
        """Own an HTTPS connection without replacing default TLS verification."""
        return self._connection(_OwnedHTTPSConnection(*args, owner=self, **kwargs))

    def _connection(self, connection: HTTPConnection) -> HTTPConnection:
        with self._lock:
            if self._closed:
                raise InterruptedError("MCP SSE connection closed")
            self._opening[connection] = threading.get_ident()
        return connection

    def resolve_endpoint(self, endpoint: str) -> str:
        """Accept relative or absolute endpoints only on the approved origin."""
        if (
            not isinstance(endpoint, str)
            or not endpoint
            or any(ord(character) <= 32 or ord(character) == 127 for character in endpoint)
        ):
            raise ValueError("MCP SSE endpoint is invalid")
        url = urljoin(self._approved_url, endpoint)
        if _origin(url) != self._origin:
            raise PermissionError("MCP SSE endpoint is outside the approved origin")
        return url

    def open(self, request: Request, *, timeout: float | None) -> Any:
        """Open one origin-checked request with all automatic redirects disabled."""
        if _origin(request.full_url) != self._origin:
            raise PermissionError("MCP SSE endpoint is outside the approved origin")
        self.assert_open()
        # A policy has at most one reader and one POST worker. Remember only
        # this thread's connections so closing a POST cannot release the GET.
        try:
            response = self._opener.open(request, timeout=timeout)
            try:
                self.assert_open()
            except Exception:
                response.close()
                raise
            return response
        finally:
            with self._lock:
                self._opening = {
                    connection: creator for connection, creator in self._opening.items()
                    if creator != threading.get_ident()
                }
