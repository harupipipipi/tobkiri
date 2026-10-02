"""Actual TLS buffered reads stop before a deliberately blocked peer releases."""

from __future__ import annotations

from pathlib import Path
import select
import ssl
import socket
import threading
import time
import urllib.request

import pytest

from core_runtime.credential_transport import _open_pinned_request
from core_runtime.http_request_lifetime import HttpRequestLifetime
from tests.test_credential_broker_pack import _pin_test_network
from tests.test_mcp_sse_cancellation import _call, _server
from tests.test_mcp_sse_cancellation import tls_server as tls_server


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_verified_tls_buffered_io_stops_without_peer_release(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[ssl.SSLContext, Path],
    phase: str, ending: str,
) -> None:
    tls, certificate = tls_server
    entered, release, released, cancellation = (threading.Event() for _ in range(4))
    received = []

    def stall(handler):
        received.append(handler.path)
        if phase == "body":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"{")
            handler.wfile.flush()
        entered.set()
        release.wait(30)
        released.set()

    context = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _pin_test_network(monkeypatch)
    with _server(stall, tls) as origin:
        deadline = time.monotonic() + (5 if ending == "deadline" else 20)

        def send():
            request = urllib.request.Request(origin + "/invoke", data=b"{}", method="POST")
            with _open_pinned_request(
                request, timeout=20, deadline=deadline, cancellation=cancellation,
            ) as response:
                response.read(1024)

        worker, done, errors = _call(send)
        try:
            assert entered.wait(3)
            if ending == "cancel":
                cancellation.set()
            budget = max(0, deadline - time.monotonic()) + 2 if ending == "deadline" else 2
            assert done.wait(budget)
            assert not released.is_set()
            assert len(errors) == 1 and isinstance(errors[0], (OSError, TimeoutError, InterruptedError))
            assert received == ["/invoke"]
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive()
    assert not any(thread.name == "tobkiri-http-deadline" for thread in threading.enumerate())


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_cancelled_tls_closes_only_after_io_and_cannot_consume_reused_handle(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[ssl.SSLContext, Path], phase: str,
) -> None:
    """A native handle reused during cleanup belongs solely to its new session."""
    tls, certificate = tls_server
    entered, release, cancellation, foreign_release = (threading.Event() for _ in range(4))
    captured = {}
    native_reads = 0
    close_violations = []
    candidates = []
    foreign_peer = []
    marker = b"FOREIGN_SESSION_MUST_REMAIN_UNREAD"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(20)

    def foreign_server():
        with listener.accept()[0] as peer:
            peer.sendall(marker)
            foreign_release.wait(30)

    foreign_worker = threading.Thread(target=foreign_server, daemon=True)
    foreign_worker.start()
    attach = HttpRequestLifetime.attach
    ssl_read = ssl.SSLSocket.recv_into
    ssl_close = ssl.SSLSocket._real_close
    native_close = socket.close

    def capture(lifetime, peer):
        attach(lifetime, peer)
        if isinstance(peer, ssl.SSLSocket):
            captured.update(peer=peer, handle=peer.fileno(), owner=threading.get_ident())

    def read(peer, *args, **kwargs):
        nonlocal native_reads
        tracked = peer is captured.get("peer")
        if tracked:
            native_reads += 1
        try:
            return ssl_read(peer, *args, **kwargs)
        finally:
            if tracked:
                native_reads -= 1

    def close_reused_handle(peer):
        tracked = peer is captured.get("peer") and peer.fileno() != -1
        if tracked and (native_reads or threading.get_ident() != captured["owner"]):
            close_violations.append("close overlapped IO or came from another thread")
        ssl_close(peer)
        if tracked:
            # Keep all candidates alive so the just-freed native handle cannot
            # hide behind repeated allocation of a different temporary socket.
            for _ in range(64):
                candidate = socket.socket()
                candidates.append(candidate)
                if candidate.fileno() == captured["handle"]:
                    candidate.connect(listener.getsockname())
                    candidate.settimeout(2)
                    foreign_peer.append(candidate)
                    break

    def forbid_watchdog_native_close(handle):
        if handle == captured.get("handle") and threading.get_ident() != captured.get("owner"):
            close_violations.append("watchdog closed a native handle")
        native_close(handle)

    monkeypatch.setattr(HttpRequestLifetime, "attach", capture)
    monkeypatch.setattr(ssl.SSLSocket, "recv_into", read)
    monkeypatch.setattr(ssl.SSLSocket, "_real_close", close_reused_handle)
    monkeypatch.setattr(socket, "close", forbid_watchdog_native_close)
    context = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _pin_test_network(monkeypatch)

    def stall(handler):
        if phase == "body":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"{")
            handler.wfile.flush()
        entered.set()
        release.wait(30)

    try:
        with _server(stall, tls) as origin:
            def send():
                request = urllib.request.Request(origin + "/invoke", data=b"{}", method="POST")
                with _open_pinned_request(request, timeout=20, cancellation=cancellation) as response:
                    response.read(1024)

            worker, done, errors = _call(send)
            try:
                assert entered.wait(3)
                cancellation.set()
                assert done.wait(2)
                assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
                assert not close_violations
                assert foreign_peer, "could not exercise native handle reuse"
                assert foreign_peer[0].recv(len(marker)) == marker
            finally:
                release.set()
                worker.join(3)
            assert not worker.is_alive()
    finally:
        foreign_release.set()
        for candidate in candidates:
            candidate.close()
        listener.close()
        foreign_worker.join(3)


def test_out_of_band_data_does_not_spin_normal_buffered_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Urgent TCP readiness must not repeatedly wake an ordinary HTTP read."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket()
    lifetime = HttpRequestLifetime(timeout=2)
    read_attempts = 0
    native_read = socket.socket.recv_into

    def read(peer, *args, **kwargs):
        nonlocal read_attempts
        if peer is client:
            read_attempts += 1
        return native_read(peer, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "recv_into", read)
    try:
        lifetime.connect(client, listener.getsockname())
        with listener.accept()[0] as server:
            wrapped = lifetime.http_socket(client)
            with wrapped.makefile() as reader:
                server.send(b"!", socket.MSG_OOB)
                with pytest.raises(TimeoutError):
                    reader.read(1)
                assert read_attempts <= 10
            wrapped.close()
    finally:
        client.close()
        listener.close()
        lifetime.close()


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_numeric_connect_stops_while_loopback_accept_backlog_is_full(ending: str) -> None:
    """A pending connect must stop without the listener accepting any peer."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []
    client = socket.socket()
    lifetime = None
    timer = None
    try:
        # Keep established and pending clients alive so TCP admission remains
        # blocked while the actual request worker polls its own connection.
        for _ in range(16):
            blocker = socket.socket()
            blocker.setblocking(False)
            held.append(blocker)
            blocker.connect_ex(listener.getsockname())
            _, writable, exceptional = select.select([], [blocker], [blocker], 0.025)
            if not (writable or exceptional):
                break
        else:
            pytest.fail("could not fill the numeric loopback accept backlog")
        cancellation = threading.Event()
        deadline = time.monotonic() + (0.5 if ending == "deadline" else 10)
        lifetime = HttpRequestLifetime(timeout=10, deadline=deadline, cancellation=cancellation)
        if ending == "cancel":
            timer = threading.Timer(0.25, cancellation.set)
            timer.start()
        started = time.monotonic()
        with pytest.raises(TimeoutError if ending == "deadline" else InterruptedError):
            lifetime.connect(client, listener.getsockname())
        assert time.monotonic() - started < 1.5
    finally:
        client.close()
        for blocker in held:
            blocker.close()
        listener.close()
        if lifetime is not None:
            lifetime.close()
        if timer is not None:
            timer.cancel()
            timer.join()
