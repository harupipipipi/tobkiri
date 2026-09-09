"""MCP SSE must not carry approved credentials to an unreviewed destination."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Callable, Iterator

import pytest

from ecosystem.defaultspack.domain.tool import mcp_client
from ecosystem.defaultspack.domain.tool.mcp_sse_policy import SseEndpointPolicy


@contextmanager
def _server(
    respond: Callable[[BaseHTTPRequestHandler], None],
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            respond(self)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            respond(self)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def _reply(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _collect(transport: mcp_client._SseTransport) -> None:
    transport.stop()
    transport._reader_thread.join(timeout=3)
    assert not transport._reader_thread.is_alive()


@pytest.mark.parametrize("endpoint", ["/messages?session=one", "messages?session=one", "absolute"])
def test_relative_and_absolute_endpoints_keep_the_approved_headers(endpoint: str) -> None:
    observed: list[tuple[str, str, str | None]] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        observed.append((handler.command, handler.path, handler.headers.get("Authorization")))
        target = f"{origin}/messages?session=one" if endpoint == "absolute" else endpoint
        _reply(handler, f"event: endpoint\ndata: {target}\n\n".encode()
               if handler.command == "GET" else b"")

    with _server(respond) as origin:
        transport = mcp_client._SseTransport(
            f"{origin}/events", headers={"Authorization": "Bearer fixture-only"},
        )
        try:
            transport.start()
            transport.send(b'{"id":1}')
        finally:
            _collect(transport)
    assert observed == [
        ("GET", "/events", "Bearer fixture-only"),
        ("POST", "/messages?session=one", "Bearer fixture-only"),
    ]


def test_cross_origin_endpoint_cannot_receive_the_approved_credentials() -> None:
    received: list[str | None] = []

    def foreign(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.headers.get("Authorization"))
        _reply(handler)

    with _server(foreign) as foreign_origin:
        def source(handler: BaseHTTPRequestHandler) -> None:
            _reply(handler, f"event: endpoint\ndata: {foreign_origin}/messages\n\n".encode())

        with _server(source) as origin:
            transport = mcp_client._SseTransport(
                f"{origin}/events", headers={"Authorization": "Bearer fixture-only"},
            )
            try:
                with pytest.raises(RuntimeError, match="MCP SSE connection failed"):
                    transport.start()
                assert transport._post_url is None
                with pytest.raises(RuntimeError, match="post URL"):
                    transport.send(b'{"id":1}')
            finally:
                _collect(transport)
    assert received == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_http_redirects_do_not_forward_credentials_or_replay_requests(
    status: int, method: str,
) -> None:
    received: list[tuple[str, str | None]] = []
    source_requests: list[str] = []

    def foreign(handler: BaseHTTPRequestHandler) -> None:
        received.append((handler.command, handler.headers.get("Authorization")))
        _reply(handler, f"event: endpoint\ndata: {foreign_origin}/messages\n\n".encode())

    with _server(foreign) as foreign_origin:
        def source(handler: BaseHTTPRequestHandler) -> None:
            source_requests.append(handler.command)
            if handler.command == method:
                handler.send_response(status)
                handler.send_header("Location", f"{foreign_origin}/redirected")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
            else:
                _reply(handler, b"event: endpoint\ndata: /messages\n\n")

        with _server(source) as origin:
            transport = mcp_client._SseTransport(
                f"{origin}/events", headers={"Authorization": "Bearer fixture-only"},
            )
            try:
                if method == "GET":
                    with pytest.raises(RuntimeError, match="MCP SSE connection failed"):
                        transport.start()
                else:
                    transport.start()
                    with pytest.raises(PermissionError, match="redirects require"):
                        transport.send(b'{"id":1}')
            finally:
                _collect(transport)
    assert received == []
    assert source_requests == (["GET"] if method == "GET" else ["GET", "POST"])


@pytest.mark.parametrize("endpoint", [
    "https://other.invalid/messages", "http://approved.invalid/messages",
    "https://approved.invalid:444/messages", "//other.invalid/messages",
    "https://user:password@approved.invalid/messages", "/messages#fragment",
    "/messages\nignored", "\t/messages", "", "file:///tmp/private",
])
def test_endpoint_cannot_change_scheme_host_port_or_url_interpretation(endpoint: str) -> None:
    policy = SseEndpointPolicy("https://approved.invalid/events")
    with pytest.raises((PermissionError, ValueError)):
        policy.resolve_endpoint(endpoint)


def test_explicit_default_port_and_case_keep_the_same_origin() -> None:
    policy = SseEndpointPolicy("https://approved.invalid/events")
    assert policy.resolve_endpoint("https://APPROVED.invalid:443/messages") == (
        "https://APPROVED.invalid:443/messages"
    )
    with pytest.raises(PermissionError):
        policy.resolve_endpoint("https://approved.invalid:0/messages")


@pytest.mark.parametrize("url", [
    "file:///tmp/private", "https://user:password@approved.invalid/events",
    "https://approved.invalid/events#fragment", "https://approved.invalid:bad/events",
    "https://approved.invalid/\nevents", " https://approved.invalid/events", "",
])
def test_invalid_configured_url_is_rejected_before_transport_start(url: str) -> None:
    with pytest.raises(ValueError, match="MCP SSE URL is invalid"):
        mcp_client._SseTransport(url)
