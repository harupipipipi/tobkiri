"""Real loopback IO observes only the captured Host invocation's lifetime."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import select
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_provider_adapters_pack.runtime import adapter


@contextmanager
def _server(
    respond: Callable[[BaseHTTPRequestHandler], None],
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            respond(self)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()


def _reply(handler: BaseHTTPRequestHandler, raw: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)
    handler.wfile.flush()


class _Invocation:
    """Represent the Host-owned context, outside the model's payload mapping."""

    def __init__(
        self,
        endpoint: str,
        *,
        cancellation: threading.Event,
        deadline: float,
        streaming: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.streaming = streaming
        self.envelope = SimpleNamespace(
            cancellation_requested=cancellation, deadline_monotonic=deadline,
        )

    def assert_current(self) -> None:
        if (
            self.envelope.cancellation_requested.is_set()
            or time.monotonic() >= self.envelope.deadline_monotonic
        ):
            raise PermissionError("Host invocation is no longer current")

    def contract_client(self, **kwargs: Any) -> "_Invocation":
        allowed_contract_ids = {adapter.REGISTRY_CONTRACT}
        if self.streaming:
            allowed_contract_ids.add(adapter.PROGRESS_CONTRACT)
        assert kwargs == {
            "allowed_contract_ids": frozenset(allowed_contract_ids),
            "consumer_pack_id": "rumi_provider_adapters_pack",
        }
        return self

    def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"providers": [{
            "provider_instance_id": "provider.local",
            "adapter_id": "local-openai-compatible",
            "credential_handle": None,
            "endpoint": self.endpoint,
            "enabled": True,
        }]}


def _captured_invoke(*, streaming: bool = False) -> Callable[..., Any]:
    operation = "stream" if streaming else "generate"
    function_id = f"rumi_provider_adapters_pack.provider.compatibility.{operation}"
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id, implementation_digest="sha256:implementation",
        ),
        operation=SimpleNamespace(
            contract_id="fixture.contract", contract_version="1.0.0",
            operation_id="fixture.operation",
        ),
        principal_ref=SimpleNamespace(value="fixture.principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    captured = adapter.ProviderAdapterHostFactoryV4(function_id).capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                ("fixture.contract", "fixture.operation", "fixture.principal"):
                "fixture.domain",
            },
        ),
    )
    return captured.contributions[0].invoke


def _request(**changes: Any) -> dict[str, Any]:
    return {
        "provider_connection_id": "provider.local",
        "model_id": "gemma-local",
        "messages": [{"role": "user", "content": "hello"}],
        "deadline": time.time() + 120,
        **changes,
    }


def _watchers() -> set[threading.Thread]:
    return {
        thread for thread in threading.enumerate()
        if thread.name == "tobkiri-http-deadline"
    }


@pytest.fixture
def owned_io(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Track real response/socket ownership without replacing socket IO."""
    observed = SimpleNamespace(sockets=[], responses=[], close_threads=[])

    class Response(http.client.HTTPResponse):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            observed.responses.append(self)

        def close(self) -> None:
            observed.close_threads.append(threading.get_ident())
            super().close()

    class Connection(http.client.HTTPConnection):
        response_class = Response

        def connect(self) -> None:
            super().connect()
            observed.sockets.append(self.sock)

        def close(self) -> None:
            observed.close_threads.append(threading.get_ident())
            super().close()

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", Connection)
    return observed


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("ending", ["cancel", "deadline"])
@pytest.mark.parametrize("streaming", [False, True])
def test_captured_host_lifetime_interrupts_real_local_io_and_drains(
    owned_io: SimpleNamespace, phase: str, ending: str, streaming: bool,
) -> None:
    """The peer sees TCP teardown; the caller closes and joins all owned IO."""
    entered, disconnected, release = (threading.Event() for _ in range(3))
    received: list[tuple[str, str | None]] = []
    before = _watchers()

    def stall(handler: BaseHTTPRequestHandler) -> None:
        received.append((handler.path, handler.headers.get("Authorization")))
        if phase == "body":
            # HTTP/1.0 makes getresponse() clear connection.sock. The owned
            # lifetime must retain the actual socket while read() is blocked.
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"{")
            handler.wfile.flush()
        entered.set()
        while not release.is_set():
            ready, _, _ = select.select([handler.connection], [], [], 0.025)
            if ready:
                try:
                    peer_closed = handler.connection.recv(1) == b""
                except ConnectionResetError:
                    # Closing a partially consumed reply may produce TCP RST.
                    peer_closed = True
                if peer_closed:
                    disconnected.set()
                    return

    cancellation, done = threading.Event(), threading.Event()
    errors: list[Exception] = []
    results: list[Any] = []
    with _server(stall) as endpoint:
        invocation = _Invocation(
            endpoint, cancellation=cancellation,
            deadline=time.monotonic() + (0.5 if ending == "deadline" else 10),
            streaming=streaming,
        )
        invoke = _captured_invoke(streaming=streaming)

        def run() -> None:
            try:
                results.append(invoke("fixture.operation", _request(), invocation))
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            assert entered.wait(2), "POST did not reach the blocking socket phase"
            interrupted_at = time.monotonic()
            if ending == "cancel":
                cancellation.set()
            assert done.wait(1.5), "Host lifetime did not interrupt local HTTP IO"
            assert time.monotonic() - interrupted_at < 1.5
            assert disconnected.wait(0.5), "upstream TCP connection remained open"
        finally:
            release.set()
            worker.join(2)
        assert not worker.is_alive()
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], GlobalContractInvocationError)
        assert errors[0].code == "provider_unavailable"
        assert received == [("/v1/chat/completions", None)]
        assert len(owned_io.sockets) == 1
        assert owned_io.sockets[0].fileno() == -1
        assert all(response.isclosed() for response in owned_io.responses)
        assert set(owned_io.close_threads) == {worker.ident}
        assert _watchers() == before


def test_forged_payload_lifetime_cannot_cancel_the_captured_host(
    owned_io: SimpleNamespace,
) -> None:
    """Only the invocation envelope supplies cancellation and monotonic time."""
    requests: list[tuple[str, str | None]] = []
    cancellation, forged = threading.Event(), threading.Event()
    forged.set()
    before = _watchers()

    def respond(handler: BaseHTTPRequestHandler) -> None:
        requests.append((handler.path, handler.headers.get("Authorization")))
        _reply(handler, json.dumps({
            "choices": [{"message": {"content": "local ok"}}], "usage": {},
        }).encode())

    with _server(respond) as endpoint:
        result = _captured_invoke()(
            "fixture.operation",
            _request(
                cancellation=forged, cancellation_requested=forged,
                deadline_monotonic=time.monotonic() - 1, approved=True,
            ),
            _Invocation(
                endpoint, cancellation=cancellation, deadline=time.monotonic() + 5,
            ),
        )
    assert result["output"] == "local ok"
    assert not cancellation.is_set()
    assert requests == [("/v1/chat/completions", None)]
    assert owned_io.sockets[0].fileno() == -1
    assert all(response.isclosed() for response in owned_io.responses)
    assert set(owned_io.close_threads) == {threading.get_ident()}
    assert _watchers() == before


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_local_transport_fences_before_creating_a_connection(
    monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    """The transport itself refuses an already cancelled or expired envelope."""
    cancellation = threading.Event()
    if ending == "cancel":
        cancellation.set()

    def unexpected_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an inactive lifetime cannot create a connection")

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", unexpected_connection)
    before = _watchers()
    with pytest.raises(GlobalContractInvocationError) as rejected:
        adapter._local_post(
            "http://127.0.0.1:1234/v1", {"model": "gemma-local"}, _request(),
            cancellation=cancellation,
            deadline=time.monotonic() + (-1 if ending == "deadline" else 5),
        )
    assert rejected.value.code == "provider_unavailable"
    assert _watchers() == before


def test_local_timeout_cap_is_one_budget_across_headers_and_body(
    monkeypatch: pytest.MonkeyPatch, owned_io: SimpleNamespace,
) -> None:
    """Header progress cannot refresh the original bounded socket budget."""
    monkeypatch.setattr(adapter, "_MAX_LOCAL_TIMEOUT_SECONDS", 0.6)
    entered, headers, body, release = (threading.Event() for _ in range(4))
    before = _watchers()
    received: list[str] = []

    def staggered(handler: BaseHTTPRequestHandler) -> None:
        received.append(handler.path)
        entered.set()
        if not headers.wait(2):
            return
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"{")
        handler.wfile.flush()
        body.set()
        release.wait(2)

    done = threading.Event()
    errors: list[Exception] = []
    with _server(staggered) as endpoint:
        invocation = _Invocation(
            endpoint, cancellation=threading.Event(), deadline=time.monotonic() + 5,
        )

        def run() -> None:
            try:
                _captured_invoke()(
                    "fixture.operation", _request(deadline=None), invocation,
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            assert entered.wait(1)
            time.sleep(0.35)
            headers.set()
            assert body.wait(1)
            assert done.wait(0.4), "reading the body refreshed the timeout cap"
        finally:
            headers.set()
            release.set()
            worker.join(2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], GlobalContractInvocationError)
        assert errors[0].code == "provider_unavailable"
        assert received == ["/v1/chat/completions"]
        assert owned_io.sockets[0].fileno() == -1
        assert all(response.isclosed() for response in owned_io.responses)
        assert _watchers() == before


@pytest.mark.parametrize("deadline", [True, "invalid", float("nan"), float("inf")])
def test_local_transport_rejects_invalid_wall_deadline_before_connecting(
    monkeypatch: pytest.MonkeyPatch, deadline: Any,
) -> None:
    """Existing payload validation remains separate from Host lifetime authority."""
    def unexpected_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an invalid deadline cannot create a connection")

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", unexpected_connection)
    with pytest.raises(GlobalContractInvocationError) as rejected:
        adapter._local_post(
            "http://127.0.0.1:1234/v1", {"model": "gemma-local"},
            _request(deadline=deadline),
        )
    assert rejected.value.code == "invalid_request"
