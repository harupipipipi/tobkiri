"""Real socket effects under the Host credential transport's lifetime and lease."""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socket
import ssl
import threading
import time
import urllib.request

import pytest

from core_runtime import credential_transport as transport
from tests.test_credential_broker_pack import _https_transport, _pin_test_network
from tests.test_mcp_sse_cancellation import _call, _reply, _server
from tests.test_mcp_sse_cancellation import tls_server as tls_server


def _request(origin: str) -> urllib.request.Request:
    return urllib.request.Request(origin + "/invoke", data=b"{}", method="POST")


def _two_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda _host, port, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
        for address in ("93.184.216.34", "93.184.216.35")
    ])


def test_post_is_never_replayed_after_the_peer_received_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost reply does not authorize another POST to the next DNS address."""
    received: list[str] = []

    def drop_reply(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.path)
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)

    _, connections = _pin_test_network(monkeypatch)
    _two_addresses(monkeypatch)
    with _server(drop_reply) as origin:
        with pytest.raises(OSError):
            transport._open_pinned_request(_request(origin), timeout=2)
    assert received == ["/invoke"]
    assert len(connections) == 1


def test_address_failover_before_http_send_keeps_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused TCP connection can fall through without duplicating HTTP."""
    received: list[str] = []

    def reply(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.path)
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"{}")

    _, connections = _pin_test_network(monkeypatch)
    _two_addresses(monkeypatch)
    connect = socket.create_connection

    def refuse_first(address, *args, **kwargs):
        if address[0] == "93.184.216.34":
            raise ConnectionRefusedError("fixture connection refused before send")
        return connect(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", refuse_first)
    with _server(reply) as origin:
        with transport._open_pinned_request(_request(origin), timeout=2) as response:
            assert response.read() == b"{}"
    assert received == ["/invoke"]
    assert connections[0][0] == "93.184.216.35"


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_late_dns_result_cannot_start_a_request(
    monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    """DNS remains owned until it returns, then must respect the original fence."""
    entered, release, cancelled = (threading.Event() for _ in range(3))
    _, connections = _pin_test_network(monkeypatch)
    resolve = socket.getaddrinfo

    def delayed_resolve(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", delayed_resolve)
    worker, done, errors = _call(lambda: transport._open_pinned_request(
        _request("http://provider.example:1234"),
        timeout=0.1 if ending == "deadline" else 3,
        cancellation=cancelled,
    ))
    try:
        assert entered.wait(1)
        if ending == "cancel":
            cancelled.set()
        else:
            time.sleep(0.15)
        assert not done.is_set()  # No abandoned DNS worker or fabricated stop ACK.
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], (TimeoutError, InterruptedError))
    assert connections == []


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_host_credential_lease_interrupts_actual_tls_io_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tls_server: tuple[ssl.SSLContext, Path], phase: str, ending: str,
) -> None:
    """Use real Authority/credential storage and TLS; redirect only test routing."""
    tls, certificate = tls_server
    entered, release = threading.Event(), threading.Event()
    received: list[str | None] = []

    def stall(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.headers.get("Authorization"))
        if phase == "body":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"{")
            handler.wfile.flush()
        entered.set()
        release.wait(5)

    # Only this test CA is added. Hostname and certificate checks remain enabled.
    context = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _pin_test_network(monkeypatch)
    with _server(stall, tls) as origin:
        owner, arguments = _https_transport(
            tmp_path, secret="http-lifetime-canary", endpoint_origin=origin,
        )
        owner._envelope = replace(
            owner._envelope,
            deadline_monotonic=time.monotonic() + (0.5 if ending == "deadline" else 4),
        )
        audits = []
        owner._audit_sink = audits.append
        worker, done, errors = _call(lambda: owner.post_json(**arguments))
        try:
            assert entered.wait(2)
            if ending == "cancel":
                owner._envelope.cancellation_requested.set()
            assert done.wait(2)
            assert len(errors) == 1
            assert isinstance(errors[0], transport.CredentialTransportDenied)
            assert "http-lifetime-canary" not in str(errors[0])
            assert all(item["status"] != "completed" for item in audits)
            with pytest.raises(transport.CredentialTransportDenied):
                owner.post_json(**arguments)
            assert received == ["Bearer http-lifetime-canary"]
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive()
    assert not any(
        thread.name == "tobkiri-http-deadline" for thread in threading.enumerate()
    )


@pytest.mark.parametrize("failure", ["untrusted_ca", "wrong_hostname"])
def test_tls_identity_failure_sends_no_credentialed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tls_server: tuple[ssl.SSLContext, Path], failure: str,
) -> None:
    """The interruptible handshake retains CA and original-host verification."""
    tls, certificate = tls_server
    received: list[str] = []
    if failure == "wrong_hostname":
        context = ssl.create_default_context(cafile=str(certificate))
        monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _pin_test_network(monkeypatch)
    with _server(lambda handler: received.append(handler.path), tls) as origin:
        if failure == "wrong_hostname":
            origin = origin.replace("127.0.0.1", "wrong.example")
        owner, arguments = _https_transport(tmp_path, endpoint_origin=origin)
        with pytest.raises(transport.CredentialTransportDenied):
            owner.post_json(**arguments)
    assert received == []
    assert not any(
        thread.name == "tobkiri-http-deadline" for thread in threading.enumerate()
    )


def test_host_credential_lease_completes_one_verified_tls_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tls_server: tuple[ssl.SSLContext, Path],
) -> None:
    """The same production IO path accepts a valid reply and consumes one lease."""
    tls, certificate = tls_server
    received: list[str | None] = []

    def reply(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.headers.get("Authorization"))
        _reply(handler, b'{"message":"accepted"}')

    context = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _pin_test_network(monkeypatch)
    with _server(reply, tls) as origin:
        owner, arguments = _https_transport(tmp_path, endpoint_origin=origin)
        audits = []
        owner._audit_sink = audits.append
        assert owner.post_json(**arguments) == {"message": "accepted"}
        assert audits[-1]["status"] == "completed"
        with pytest.raises(transport.CredentialTransportDenied):
            owner.post_json(**arguments)
    assert len(received) == 1
    assert not any(
        thread.name == "tobkiri-http-deadline" for thread in threading.enumerate()
    )


@pytest.mark.parametrize("phase", ["dns", "tls"])
@pytest.mark.parametrize("change", ["revoke", "epoch"])
def test_authority_changed_during_connection_cannot_send_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    tls_server: tuple[ssl.SSLContext, Path], phase: str, change: str,
) -> None:
    """A completed DNS/TLS wait cannot revive the prior credential authority."""
    tls, certificate = tls_server
    received: list[str] = []

    def reply(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.command)
        _reply(handler, b'{"message":"unapproved effect"}')

    context = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _, connections = _pin_test_network(monkeypatch)
    with _server(reply, tls) as origin:
        owner, arguments = _https_transport(tmp_path, endpoint_origin=origin)
        audits = []
        owner._audit_sink = audits.append
        changed = False

        def invalidate() -> None:
            nonlocal changed
            assert not changed
            changed = True
            if change == "epoch":
                owner._authority_store.advance_security_epoch("TLS fixture fence")
            else:
                owner._authority_store.revoke(
                    target_kind="function_principal",
                    target_id=owner._binding.provider_principal_id,
                    reason="TLS fixture revoke",
                )
            assert not owner._authority_still_active()

        if phase == "dns":
            resolve = socket.getaddrinfo

            def resolve_then_revoke(*args, **kwargs):
                result = resolve(*args, **kwargs)
                invalidate()
                return result

            monkeypatch.setattr(socket, "getaddrinfo", resolve_then_revoke)
        else:
            connect = transport._PinnedHTTPSConnection.connect

            def connect_then_revoke(connection):
                connect(connection)
                invalidate()

            monkeypatch.setattr(
                transport._PinnedHTTPSConnection, "connect", connect_then_revoke,
            )
        with pytest.raises(transport.CredentialTransportDenied):
            owner.post_json(**arguments)
        assert changed
        assert all(item["status"] != "completed" for item in audits)
        with pytest.raises(transport.CredentialTransportDenied):
            owner.post_json(**arguments)
    assert received == []
    assert len(connections) == (0 if phase == "dns" else 1)
    assert not any(
        thread.name == "tobkiri-http-deadline" for thread in threading.enumerate()
    )
