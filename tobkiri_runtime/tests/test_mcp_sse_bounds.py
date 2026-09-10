"""SSE wire and queue limits must fail before unbounded response retention."""

from __future__ import annotations

import io
import json
import threading
from unittest.mock import Mock

import pytest

from core_runtime.mcp import transport as mcp_client, sse_events as mcp_sse_events
from core_runtime.mcp.sse_events import read_sse_events


def test_sse_multiline_framing_preserves_data_whitespace_and_byte_order() -> None:
    stream = io.BytesIO(
        b'\xef\xbb\xbfevent: endpoint\r\ndata: /messages\r\n\r\n'
        b': ignored comment\nevent: message\ndata: {"id": 1,\n'
        b'data:   "result": "  text  "}\n\n'
        b'event: message\ndata: {"id": 2}\n'  # Incomplete event is not delivered.
    )
    assert list(read_sse_events(stream)) == [
        ("endpoint", "/messages"),
        ("message", '{"id": 1,\n  "result": "  text  "}'),
    ]


@pytest.mark.parametrize("wire", [
    b"data: " + b"x" * 65,
    (b"data: small\n" * 6) + b"\n",
    (b": ignored comment\n" * 4) + b"\n",
    b"data: " + ("大" * 20).encode() + b"\n\n",
])
def test_event_limit_counts_wire_bytes_across_all_lines(
    wire: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_sse_events, "SSE_EVENT_LIMIT", 64)
    stream = io.BytesIO(wire)
    with pytest.raises(ValueError, match="byte limit"):
        list(read_sse_events(stream))
    assert stream.tell() <= 65 or b"\n" in wire[:65]


def test_each_complete_event_gets_a_fresh_bounded_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = b'data: {"id": 1}\n\n'
    monkeypatch.setattr(mcp_sse_events, "SSE_EVENT_LIMIT", len(wire))
    assert list(read_sse_events(io.BytesIO(wire * 100))) == [("", '{"id": 1}')] * 100


@pytest.mark.parametrize("wire", [b"data: \xff\n\n", b"data: \xc3\n\n"])
def test_invalid_utf8_is_not_replaced_with_another_response(wire: bytes) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        list(read_sse_events(io.BytesIO(wire)))


@pytest.mark.parametrize("payload", ["[]", "null", '"text"', "42", "{bad", "[" * 2000])
def test_invalid_rpc_events_fail_the_transport_and_fence_later_sends(payload: str) -> None:
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    response = io.BytesIO(
        f"event: endpoint\ndata: /messages\n\nevent: message\ndata: {payload}\n\n".encode(),
    )
    transport._endpoint_policy.open = Mock(return_value=response)
    transport._sse_loop()
    assert response.closed
    assert transport._failure == "MCP SSE connection failed"
    with pytest.raises(RuntimeError, match="connection failed"):
        transport.recv(timeout=0)
    with pytest.raises(RuntimeError, match="connection failed"):
        transport.send(b'{"id":1}')
    assert transport._endpoint_policy.open.call_count == 1


def test_flood_exits_reader_and_never_queues_more_than_the_owned_budget() -> None:
    limit = mcp_sse_events.SSE_QUEUE_LIMIT
    wire = "event: endpoint\ndata: /messages\n\n" + "".join(
        f"data: {json.dumps({'id': i})}\n\n" for i in range(limit + 10)
    )
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    response = io.BytesIO(wire.encode())
    transport._endpoint_policy.open = Mock(return_value=response)
    try:
        transport.start()
    except RuntimeError:
        # The finite fixture can overflow either before or after start observes
        # the endpoint. Both interleavings must fence every received result.
        pass
    transport._reader_thread.join(timeout=3)
    assert not transport._reader_thread.is_alive(), "SSE reader blocked on a full queue"
    assert response.closed
    assert transport._queue.qsize() == limit
    with pytest.raises(RuntimeError, match="connection failed"):
        transport.recv(timeout=0)
    transport.stop()


def test_valid_queued_message_survives_clean_eof() -> None:
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    response = io.BytesIO(b"event: endpoint\ndata: /messages\n\ndata: {\"id\": 1}\n\n")
    transport._endpoint_policy.open = Mock(return_value=response)
    transport.start()
    transport._reader_thread.join(timeout=3)
    assert transport.recv(timeout=0) == {"id": 1}
    with pytest.raises(RuntimeError, match="connection closed"):
        transport.recv(timeout=0)
    transport.stop()


def test_post_cannot_succeed_when_reading_or_closing_passes_the_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10.0]
    monkeypatch.setattr(mcp_client.time, "monotonic", lambda: clock[0])
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    transport._handle_event("endpoint", "/messages")

    class Response(io.BytesIO):
        def close(self) -> None:
            clock[0] = 11.1
            super().close()

    response = Response(b"ack")
    transport._endpoint_policy.open = Mock(return_value=response)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        transport.send(b'{"id":1}', deadline=11.0)
    assert response.closed
    assert transport._endpoint_policy.open.call_count == 1
    assert transport._endpoint_policy.open.call_args.kwargs == {"timeout": 1.0}


def test_failed_reader_join_retains_response_and_connection_for_cleanup_retry() -> None:
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    response = io.BytesIO(b"pending")
    reader = Mock(ident=1)
    reader.is_alive.return_value = True
    transport._reader_thread = reader
    transport._response = response
    connection = mcp_client._ServerConnection("owned", {})
    connection._transport = transport
    with pytest.raises(RuntimeError, match="reader did not stop"):
        connection.disconnect()
    assert connection._transport is transport
    assert transport._response is response
    assert not response.closed
    reader.join.assert_called_once()
    assert 0 < reader.join.call_args.kwargs["timeout"] <= 5
    reader.is_alive.return_value = False
    connection.disconnect()
    assert response.closed
    assert connection._transport is None
    assert transport._response is None


def test_opener_finishing_after_stop_does_not_enter_a_new_read() -> None:
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    opening, release = threading.Event(), threading.Event()
    response = Mock()

    def open_response(*_args: object, **_kwargs: object) -> Mock:
        opening.set()
        assert release.wait(3)
        return response

    transport._endpoint_policy.open = open_response
    reader = threading.Thread(target=transport._sse_loop, daemon=True)
    transport._reader_thread = reader
    reader.start()
    try:
        assert opening.wait(2)
        transport._stop_event.set()
        release.set()
        transport.stop()
        response.readline.assert_not_called()
        response.close.assert_called()
        assert transport._response is None
        assert not reader.is_alive()
    finally:
        release.set()
        reader.join(timeout=3)


def test_failed_reader_start_is_collectable_and_cannot_restart_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = mcp_client._SseTransport("http://approved.invalid/events")

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("reader start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="reader start failed"):
        transport.start()
    transport.stop()
    assert transport._response is None
    with pytest.raises(RuntimeError, match="already been started"):
        transport.start()


def test_stop_after_endpoint_publication_cannot_report_startup_success() -> None:
    transport = mcp_client._SseTransport("http://approved.invalid/events")
    response = io.BytesIO(b"event: endpoint\ndata: /messages\n\n")
    transport._endpoint_policy.open = Mock(return_value=response)
    wait_for_endpoint = transport._ready_event.wait

    def stop_after_endpoint(timeout: float) -> bool:
        ready = wait_for_endpoint(timeout)
        if ready:
            transport.stop()
        return ready

    transport._ready_event.wait = stop_after_endpoint
    try:
        with pytest.raises(RuntimeError, match="connection closed"):
            transport.start()
        assert response.closed
        assert not transport._reader_thread.is_alive()
    finally:
        transport.stop()
