"""Keep MCP SSE requests inside the connection's approved HTTP origin."""

from __future__ import annotations

from http.client import HTTPMessage
import socket
from typing import Any, IO
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def interrupt_sse_response(response: Any) -> None:
    """Interrupt urllib's socket read without taking its buffered-reader lock.

    CPython HTTPResponse owns a BufferedReader over SocketIO for HTTP and HTTPS.
    If that socket is unavailable, the caller must retain the response until its
    reader finishes; closing a buffer concurrently with read can block forever.
    """
    buffer = getattr(response, "fp", None)
    raw = getattr(buffer, "raw", None)
    owned_socket = getattr(raw, "_sock", None)
    if isinstance(owned_socket, socket.socket):
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


class SseEndpointPolicy:
    """Resolve server-provided endpoints without widening approved authority."""

    def __init__(self, approved_url: str) -> None:
        self._origin = _origin(approved_url)
        self._approved_url = approved_url
        self._opener = build_opener(_NoRedirect())

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
        return self._opener.open(request, timeout=timeout)
