"""Exercise the bounded, credential-free local model HTTP transport itself."""

from __future__ import annotations

import errno
import json
import socket
import time
from collections.abc import Callable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from typing import Any

import core_runtime.local_model_transport as transport_module
import pytest
from core_runtime.local_model_transport import LocalModelBinding, LocalModelTransport
from typing_extensions import Self


def _binding(port: int = 8080) -> LocalModelBinding:
    return LocalModelBinding(
        "defaults",
        "provider.local",
        f"http://127.0.0.1:{port}/v1",
        ("lfm-local",),
        1,
    )


def _body() -> dict[str, Any]:
    return {
        "model": "lfm-local",
        "messages": [{"role": "user", "content": "17+25"}],
    }


def _transport(
    *,
    resolve: Callable[[str], LocalModelBinding] | None = None,
    guard: Callable[[], None] | None = None,
    cancellation: Event | None = None,
) -> LocalModelTransport:
    return LocalModelTransport(
        profile_id="defaults",
        resolve_binding=resolve or (lambda _: _binding()),
        assert_current=guard or (lambda: None),
        deadline_monotonic=time.monotonic() + 5,
        cancellation=cancellation or Event(),
    )


def _post(instance: LocalModelTransport) -> dict[str, Any]:
    return instance.post_chat(
        provider_instance_id="provider.local",
        body=_body(),
        deadline=time.time() + 5,
    )


class _FakeSocket:
    def __init__(self) -> None:
        self.shutdown_calls: list[int] = []
        self.interrupted = Event()

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)
        self.interrupted.set()


class _FakeResponse:
    def __init__(
        self,
        data: bytes = b'{"choices":[{"message":{"content":"42"}}]}',
        *,
        status: int = 200,
        on_read: Callable[[], None] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.data = data
        self.status = status
        self.on_read = on_read
        self.read_error = read_error
        self.read_sizes: list[int] = []
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def read(self, amount: int) -> bytes:
        self.read_sizes.append(amount)
        if self.on_read is not None:
            self.on_read()
        if self.read_error is not None:
            raise self.read_error
        return self.data[:amount]


class _FakeConnection:
    def __init__(self, factory: _ConnectionFactory) -> None:
        self.factory = factory
        self.owned_socket = _FakeSocket()
        self.sock: _FakeSocket | None = None
        self.connect_calls = 0
        self.response_calls = 0
        self.close_calls = 0
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    def _fail(self, stage: str) -> None:
        if self.factory.fail_at == stage:
            raise OSError(f"fake {stage} failure")

    def connect(self) -> None:
        self.connect_calls += 1
        self._fail("connect")
        if self.factory.has_socket:
            self.sock = self.owned_socket
        if self.factory.on_connect is not None:
            self.factory.on_connect()

    def request(
        self, method: str, url: str, body: bytes, headers: dict[str, str]
    ) -> None:
        self.requests.append((method, url, body, headers))
        self._fail("request")

    def getresponse(self) -> _FakeResponse:
        self.response_calls += 1
        self._fail("getresponse")
        return self.factory.response

    def close(self) -> None:
        self.close_calls += 1
        self.sock = None


class _ConnectionFactory:
    def __init__(self) -> None:
        self.response = _FakeResponse()
        self.fail_at: str | None = None
        self.has_socket = True
        self.on_connect: Callable[[], None] | None = None
        self.calls: list[tuple[str, int, float]] = []
        self.connections: list[_FakeConnection] = []

    def __call__(
        self, host: str, port: int, *, timeout: float
    ) -> _FakeConnection:
        self.calls.append((host, port, timeout))
        connection = _FakeConnection(self)
        self.connections.append(connection)
        return connection


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> _ConnectionFactory:
    """Replace only the connection boundary, retaining the real IO lifetime."""
    factory = _ConnectionFactory()
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", factory)
    return factory


def test_success_uses_exact_loopback_route_and_closes_resources(
    fake_http: _ConnectionFactory,
) -> None:
    """Successful finite chat uses the numeric IPv4 binding and bounded read."""
    result = _post(_transport())

    assert result == {"choices": [{"message": {"content": "42"}}]}
    assert len(fake_http.calls) == 1
    host, port, timeout = fake_http.calls[0]
    assert (host, port) == ("127.0.0.1", 8080)
    assert 0 < timeout <= 5
    connection = fake_http.connections[0]
    assert connection.connect_calls == connection.response_calls == 1
    assert len(connection.requests) == 1
    method, route, payload, headers = connection.requests[0]
    assert (method, route) == ("POST", "/v1/chat/completions")
    assert json.loads(payload) == {**_body(), "max_tokens": 512, "stream": False}
    assert headers == {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "close",
    }
    assert fake_http.response.read_sizes == [transport_module._MAX_BYTES + 1]
    assert fake_http.response.closed
    assert connection.close_calls == 1


def test_proxy_and_credential_environment_cannot_change_connection_or_headers(
    fake_http: _ConnectionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient proxy configuration and cloud keys never enter the local request."""
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://user:secret@proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-secret-must-not-be-sent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-secret-must-not-be-sent")

    _post(_transport())

    assert fake_http.calls[0][:2] == ("127.0.0.1", 8080)
    _, _, payload, headers = fake_http.connections[0].requests[0]
    assert {name.lower() for name in headers} == {
        "content-type", "accept", "connection"
    }
    assert b"secret" not in payload
    assert all("secret" not in value for value in headers.values())


@pytest.mark.parametrize(
    "key", ["headers", "api_key", "token", "endpoint", "base_url", "proxy"]
)
def test_caller_cannot_inject_credentials_or_connection_options(
    fake_http: _ConnectionFactory, key: str
) -> None:
    """Unrecognized request fields fail before a connection is even allocated."""
    instance = _transport()
    with pytest.raises(PermissionError, match="outside configured scope"):
        instance.post_chat(
            provider_instance_id="provider.local",
            body={**_body(), key: "caller-controlled"},
            deadline=time.time() + 5,
        )
    assert fake_http.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"temperature": True},
        {"temperature": "1"},
        {"temperature": -0.01},
        {"temperature": 2.01},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"top_p": -0.01},
        {"top_p": 1.01},
        {"top_p": False},
        {"repeat_penalty": -0.01},
        {"repeat_penalty": 2.01},
        {"repetition_penalty": float("-inf")},
        {"repetition_penalty": None},
        {"top_k": True},
        {"top_k": 1.0},
        {"top_k": -1},
        {"top_k": 1001},
        {"seed": False},
        {"seed": 1.0},
        {"seed": -2},
        {"seed": 2**32},
        {"stop": ""},
        {"stop": "x" * 257},
        {"stop": []},
        {"stop": ["x"] * 17},
        {"stop": ["good", ""]},
        {"stop": ["good", 1]},
        {"stop": ("unsupported-tuple",)},
        {"stop": {"unsupported": "object"}},
        {"stop": None},
    ],
)
def test_invalid_sampling_and_stop_options_fail_before_connection(
    fake_http: _ConnectionFactory, change: dict[str, Any]
) -> None:
    """Sampling knobs are bounded scalars and stop sequences remain finite text."""
    with pytest.raises(ValueError, match="sampling value|stop sequences"):
        _transport().post_chat(
            provider_instance_id="provider.local",
            body={**_body(), **change},
            deadline=time.time() + 5,
        )
    assert fake_http.calls == []


@pytest.mark.parametrize("stop", ["done", ["done", "finished"]])
def test_valid_sampling_boundaries_are_preserved_in_payload(
    fake_http: _ConnectionFactory, stop: str | list[str]
) -> None:
    """Supported explicit generation settings reach the local provider intact."""
    parameters = {
        "temperature": 2,
        "top_p": 0,
        "repeat_penalty": 0.5,
        "repetition_penalty": 2,
        "top_k": 1000,
        "seed": 2**32 - 1,
        "max_tokens": 4096,
        "stop": stop,
    }
    _transport().post_chat(
        provider_instance_id="provider.local",
        body={**_body(), **parameters},
        deadline=time.time() + 5,
    )
    assert json.loads(fake_http.connections[0].requests[0][2]) == {
        **_body(), **parameters, "stream": False
    }


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 503])
def test_redirects_and_unsuccessful_statuses_never_trigger_another_request(
    fake_http: _ConnectionFactory, status: int
) -> None:
    """The transport refuses redirection and errors without reading or retrying."""
    fake_http.response = _FakeResponse(status=status)
    with pytest.raises(RuntimeError, match="unsuccessful response"):
        _post(_transport())

    assert len(fake_http.connections) == 1
    assert len(fake_http.connections[0].requests) == 1
    assert fake_http.response.read_sizes == []
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


def test_oversized_response_is_bounded_and_rejected(
    fake_http: _ConnectionFactory,
) -> None:
    """A response cannot exceed the finite read budget even with more data ready."""
    fake_http.response = _FakeResponse(b"x" * (transport_module._MAX_BYTES + 100))
    with pytest.raises(ValueError, match="response exceeds limit"):
        _post(_transport())
    assert fake_http.response.read_sizes == [transport_module._MAX_BYTES + 1]
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


@pytest.mark.parametrize("data", [b"[]", b"null", b"42", b'"text"'])
def test_nonobject_json_response_is_rejected_and_closed(
    fake_http: _ConnectionFactory, data: bytes
) -> None:
    """Valid JSON scalars and arrays do not satisfy the chat object contract."""
    fake_http.response = _FakeResponse(data)
    with pytest.raises(TypeError, match="response must be an object"):
        _post(_transport())
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


@pytest.mark.parametrize("data", [b"{", b"not-json", b"\xff"])
def test_invalid_json_response_is_rejected_and_closed(
    fake_http: _ConnectionFactory, data: bytes
) -> None:
    """JSON syntax and encoding failures still release the response/socket owner."""
    fake_http.response = _FakeResponse(data)
    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
        _post(_transport())
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b'{"value":NaN}', "nonfinite"),
        (b'{"value":Infinity}', "nonfinite"),
        (b'{"value":-Infinity}', "nonfinite"),
        (b'{"value":1e309}', "nonfinite"),
        (b'{"value":1,"value":2}', "duplicate"),
        (b'{"nested":{"value":1,"value":2}}', "duplicate"),
        (b'{"value":' + b"[" * 33 + b"0" + b"]" * 33 + b"}", "nesting"),
        (b'{"value":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}", "nesting"),
    ],
)
def test_ambiguous_nonfinite_or_deep_json_is_rejected_and_closed(
    fake_http: _ConnectionFactory, data: bytes, message: str
) -> None:
    """Strict JSON rejects ambiguous keys and unbounded numeric or nesting data."""
    fake_http.response = _FakeResponse(data)
    with pytest.raises(ValueError, match=message):
        _post(_transport())
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


def test_finite_provider_timing_floats_are_preserved(
    fake_http: _ConnectionFactory,
) -> None:
    """Strict JSON validation retains ordinary provider timing measurements."""
    expected = {
        "choices": [{"message": {"content": "42"}}],
        "timings": {"prompt_per_second": 123.25, "predicted_per_second": 45.5},
    }
    fake_http.response = _FakeResponse(json.dumps(expected).encode("utf-8"))
    assert _post(_transport()) == expected
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


@pytest.mark.parametrize(
    "change",
    [
        {"record_revision": 2},
        {"endpoint": "http://127.0.0.1:8081/v1"},
        {"model_ids": ("replacement-model",)},
        {"allowlist_digest": "changed-owner-allowlist"},
    ],
)
def test_binding_revoked_during_response_cannot_release_result(
    fake_http: _ConnectionFactory, change: dict[str, Any]
) -> None:
    """Returning a response also requires the original owner binding to be current."""
    current = [_binding()]
    fake_http.response.on_read = lambda: current.__setitem__(
        0, replace(current[0], **change)
    )
    with pytest.raises(PermissionError, match="configuration changed"):
        _post(_transport(resolve=lambda _: current[0]))
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


def test_invocation_revoked_during_response_cannot_release_result(
    fake_http: _ConnectionFactory,
) -> None:
    """The invocation guard is rechecked after receiving the complete response."""
    revoked = Event()

    def guard() -> None:
        if revoked.is_set():
            raise PermissionError("invocation revoked")

    fake_http.response.on_read = revoked.set
    with pytest.raises(PermissionError, match="invocation revoked"):
        _post(_transport(guard=guard))
    assert fake_http.response.closed
    assert fake_http.connections[0].close_calls == 1


def test_binding_changed_during_connect_prevents_sending_request(
    fake_http: _ConnectionFactory,
) -> None:
    """A changed owner configuration is caught before writing to the socket."""
    current = [_binding()]
    fake_http.on_connect = lambda: current.__setitem__(
        0, replace(current[0], record_revision=2)
    )
    with pytest.raises(PermissionError, match="configuration changed"):
        _post(_transport(resolve=lambda _: current[0]))
    connection = fake_http.connections[0]
    assert connection.connect_calls == 1
    assert connection.requests == []
    assert connection.close_calls == 1


def test_precancelled_request_never_allocates_connection(
    fake_http: _ConnectionFactory,
) -> None:
    """Cancellation is enforced before any HTTP connection or request exists."""
    cancellation = Event()
    cancellation.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        _post(_transport(cancellation=cancellation))
    assert fake_http.calls == []


def test_cancellation_during_read_interrupts_socket_and_denies_result(
    fake_http: _ConnectionFactory,
) -> None:
    """The real lifetime watchdog interrupts in-flight IO and rejects its result."""
    cancellation = Event()

    def cancel_while_reading() -> None:
        cancellation.set()
        assert fake_http.connections[0].owned_socket.interrupted.wait(2)

    fake_http.response.on_read = cancel_while_reading
    with pytest.raises(InterruptedError, match="cancelled"):
        _post(_transport(cancellation=cancellation))
    connection = fake_http.connections[0]
    assert connection.owned_socket.shutdown_calls == [socket.SHUT_RDWR]
    assert fake_http.response.closed
    assert connection.close_calls == 1


@pytest.mark.parametrize("stage", ["connect", "request", "getresponse", "read"])
def test_io_failure_closes_connection_and_consumes_transport(
    fake_http: _ConnectionFactory, stage: str
) -> None:
    """Every network-stage failure closes its connection and cannot be replayed."""
    fake_http.fail_at = stage
    if stage == "read":
        fake_http.response.read_error = OSError("fake read failure")
    instance = _transport()

    with pytest.raises(OSError, match=f"fake {stage} failure"):
        _post(instance)
    assert fake_http.connections[0].close_calls == 1
    if stage == "read":
        assert fake_http.response.closed
    with pytest.raises(PermissionError, match="already consumed"):
        _post(instance)
    assert len(fake_http.connections) == 1


def test_connect_without_socket_fails_closed(fake_http: _ConnectionFactory) -> None:
    """A connection lacking an owned socket cannot be used to send inference."""
    fake_http.has_socket = False
    with pytest.raises(OSError, match="connection is unavailable"):
        _post(_transport())
    connection = fake_http.connections[0]
    assert connection.requests == []
    assert connection.close_calls == 1


def test_successful_transport_is_single_use(fake_http: _ConnectionFactory) -> None:
    """One authorized invocation yields at most one HTTP request, even on success."""
    instance = _transport()
    _post(instance)
    with pytest.raises(PermissionError, match="already consumed"):
        _post(instance)
    assert len(fake_http.connections) == 1
    assert len(fake_http.connections[0].requests) == 1


def test_real_ipv4_loopback_request_has_no_credentials_or_proxy_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the actual stdlib HTTP path against a local-only recording server."""
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            """Record one finite text request and return a complete JSON response."""
            received.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(int(self.headers["Content-Length"])),
                }
            )
            data = b'{"choices":[{"message":{"content":"42"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            """Keep the deterministic integration test silent."""

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            pytest.skip(f"environment denies IPv4 loopback bind: {exc}")
        raise
    worker = Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        name="local-model-test-server",
        daemon=True,
    )
    worker.start()
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://user:secret@127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-secret-must-not-be-sent")
    binding = _binding(server.server_port)
    try:
        result = _post(_transport(resolve=lambda _: binding))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == {"choices": [{"message": {"content": "42"}}]}
    assert len(received) == 1
    assert received[0]["path"] == "/v1/chat/completions"
    assert json.loads(received[0]["body"]) == {
        **_body(), "max_tokens": 512, "stream": False
    }
    headers = {name.lower(): value for name, value in received[0]["headers"].items()}
    assert headers["host"] == f"127.0.0.1:{server.server_port}"
    assert headers["connection"] == "close"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert not {"authorization", "proxy-authorization", "cookie", "x-api-key"} & set(
        headers
    )
    assert all("secret" not in value for value in headers.values())
