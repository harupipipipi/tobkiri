"""Own MCP HTTP IO through cancellation, including requests without headers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import socket
import ssl
import threading
import time
from typing import Callable, Iterator
import urllib.error
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from core_runtime.mcp import transport as mcp
from core_runtime.mcp.sse_policy import SseEndpointPolicy


@contextmanager
def _server(
    respond: Callable[[BaseHTTPRequestHandler], None],
    tls: ssl.SSLContext | None = None,
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
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def _reply(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def _call(action: Callable[[], None]) -> tuple[threading.Thread, threading.Event, list[Exception]]:
    done = threading.Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            action()
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, done, errors


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("ending", ["cancel", "deadline", "stop"])
def test_live_post_is_interrupted_and_never_replayed(phase: str, ending: str) -> None:
    entered, release = threading.Event(), threading.Event()
    observed: list[str] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        observed.append(handler.command)
        if handler.command == "GET":
            _reply(handler, b"event: endpoint\ndata: /messages\n\n")
            return
        if phase == "body":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"a")
            handler.wfile.flush()
        entered.set()
        release.wait(10)

    cancellation = threading.Event()
    with _server(respond) as origin:
        transport = mcp._SseTransport(f"{origin}/events")
        transport.start()
        sender, done, errors = _call(lambda: transport.send(
            b'{"id":1}', deadline=time.monotonic() + (1 if ending == "deadline" else 20),
            cancellation=cancellation,
        ))
        try:
            assert entered.wait(2), "POST did not reach the controlled blocking point"
            post = transport._post_thread
            if ending == "cancel":
                cancellation.set()
            elif ending == "stop":
                transport.stop()
            assert done.wait(2), "POST ignored its original deadline/cancellation"
            assert len(errors) == 1
            if ending == "cancel":
                assert isinstance(errors[0], InterruptedError)
            elif ending == "deadline":
                assert isinstance(errors[0], TimeoutError)
            transport.stop()
            assert not post.is_alive()
            with pytest.raises(RuntimeError):
                transport.send(b'{"id":2}')
        finally:
            release.set()
            sender.join(timeout=3)
            transport.stop()
        assert not sender.is_alive()
    assert observed == ["GET", "POST"]


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_connection_startup_obeys_host_lifetime_before_get_headers(ending: str) -> None:
    entered, release = threading.Event(), threading.Event()
    observed: list[str] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        observed.append(handler.command)
        entered.set()
        release.wait(10)

    cancellation = threading.Event()
    with _server(respond) as origin:
        connection = mcp._ServerConnection("fixture", {"transport": "sse", "url": origin})
        caller, done, errors = _call(lambda: connection.connect(
            deadline=time.monotonic() + (1 if ending == "deadline" else 20),
            cancellation=cancellation,
        ))
        try:
            assert entered.wait(2)
            reader = connection._transport._reader_thread
            if ending == "cancel":
                cancellation.set()
            assert done.wait(2), "startup waited for the default 30-second timeout"
            assert len(errors) == 1
            assert isinstance(errors[0], InterruptedError if ending == "cancel" else TimeoutError)
            assert connection._transport is None
            assert connection.status == "error"
            assert not reader.is_alive()
        finally:
            release.set()
            caller.join(timeout=3)
            connection.disconnect()
        assert not caller.is_alive()
    assert observed == ["GET"]


def test_cancelled_writer_wait_does_not_interrupt_another_post_or_send_itself() -> None:
    entered, release = threading.Event(), threading.Event()
    posts: list[str] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        if handler.command == "GET":
            _reply(handler, b"event: endpoint\ndata: /messages\n\n")
            return
        posts.append(handler.path)
        if len(posts) == 1:
            entered.set()
            release.wait(10)
        _reply(handler, b"ack")

    with _server(respond) as origin:
        transport = mcp._SseTransport(f"{origin}/events")
        transport.start()
        first, first_done, first_errors = _call(lambda: transport.send(b'{"id":1}'))
        cancellation = threading.Event()
        second: threading.Thread | None = None
        try:
            assert entered.wait(2)
            assert transport._write_lock.locked()
            second, second_done, second_errors = _call(lambda: transport.send(
                b'{"id":2}', cancellation=cancellation,
            ))
            assert not second_done.wait(0.1)
            cancellation.set()
            assert second_done.wait(1)
            assert len(second_errors) == 1 and isinstance(second_errors[0], InterruptedError)
            assert transport._failure is None
            assert not first_done.is_set()
            release.set()
            assert first_done.wait(2)
            assert first_errors == []
            transport.send(b'{"id":3}')
            assert len(posts) == 2
        finally:
            release.set()
            first.join(timeout=3)
            if second is not None:
                second.join(timeout=3)
                assert not second.is_alive()
            transport.stop()
        assert not first.is_alive()


def test_delayed_dns_retains_ownership_and_cannot_send_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    observed: list[str] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        observed.append(handler.command)
        _reply(handler, b"event: endpoint\ndata: /messages\n\n")

    resolve = socket.getaddrinfo

    def delayed(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(10)
        return resolve(*args, **kwargs)

    with _server(respond) as origin:
        transport = mcp._SseTransport(f"{origin}/events")
        transport.start()
        monkeypatch.setattr(socket, "getaddrinfo", delayed)
        cancellation = threading.Event()
        sender, done, errors = _call(lambda: transport.send(b'{"id":1}', cancellation=cancellation))
        try:
            assert entered.wait(2)
            post = transport._post_thread
            join = post.join
            cancellation.set()
            assert done.wait(1)
            assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
            monkeypatch.setattr(post, "join", lambda timeout: join(timeout=0))
            with pytest.raises(RuntimeError, match="POST worker did not stop"):
                transport.stop()
            assert transport._post_thread is post and post.is_alive()
            with pytest.raises(RuntimeError):
                transport.send(b'{"id":2}')
            release.set()
            join(timeout=3)
            assert not post.is_alive()
            transport.stop()
            assert transport._post_thread is None
        finally:
            release.set()
            sender.join(timeout=3)
            transport.stop()
        assert not sender.is_alive()
    assert observed == ["GET"]


def test_expired_unsent_post_keeps_connection_usable() -> None:
    calls: list[str] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        calls.append(handler.command)
        _reply(handler, b"event: endpoint\ndata: /messages\n\n" if handler.command == "GET" else b"ok")

    with _server(respond) as origin:
        transport = mcp._SseTransport(origin)
        transport.start()
        try:
            with pytest.raises(TimeoutError):
                transport.send(b'{"id":1}', deadline=time.monotonic() - 1)
            assert transport._failure is None
            transport.send(b'{"id":2}')
        finally:
            transport.stop()
    assert calls == ["GET", "POST"]


def test_post_thread_start_failure_is_retained_until_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = mcp._SseTransport("http://unused.invalid/events")
    transport._handle_event("endpoint", "/messages")

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("POST start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="POST start failed"):
        transport.send(b'{"id":1}')
    assert transport._post_thread is not None
    assert transport._post_thread.ident is None
    transport.stop()
    assert transport._post_thread is None
    with pytest.raises(RuntimeError):
        transport.send(b'{"id":2}')


@pytest.fixture
def tls_server(tmp_path: Path) -> tuple[ssl.SSLContext, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCP isolated fixture")])
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2100, 1, 1, tzinfo=timezone.utc))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=None, decipher_only=None,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "fixture.pem", tmp_path / "fixture-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    return server, cert_path


def test_owned_https_keeps_certificate_verification_and_can_interrupt_body(
    tls_server: tuple[ssl.SSLContext, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    tls, cert_path = tls_server
    entered, release = threading.Event(), threading.Event()
    observed: list[str] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        observed.append(handler.command)
        if handler.command == "GET":
            _reply(handler, b"event: endpoint\ndata: /messages\n\n")
        else:
            handler.send_response(200)
            handler.send_header("Content-Length", "10")
            handler.end_headers()
            handler.wfile.write(b"a")
            handler.wfile.flush()
            entered.set()
            release.wait(10)

    with _server(respond, tls) as origin:
        policy = SseEndpointPolicy(origin)
        with pytest.raises(urllib.error.URLError) as rejected:
            policy.open(urllib.request.Request(origin), timeout=2)
        assert isinstance(rejected.value.reason, ssl.SSLCertVerificationError)
        assert observed == []
        context = ssl.create_default_context(cafile=str(cert_path))
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        monkeypatch.setattr(ssl, "_create_default_https_context", lambda **_kwargs: context)
        transport = mcp._SseTransport(origin)
        transport.start()
        cancellation = threading.Event()
        sender, done, errors = _call(lambda: transport.send(b'{"id":1}', cancellation=cancellation))
        try:
            assert entered.wait(2)
            post = transport._post_thread
            cancellation.set()
            assert done.wait(2)
            assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
            transport.stop()
            assert not post.is_alive()
        finally:
            release.set()
            sender.join(timeout=3)
            transport.stop()
        assert not sender.is_alive()
    assert observed == ["GET", "POST"]
