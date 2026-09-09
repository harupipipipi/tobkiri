"""MCP stdio child ownership must survive unsuccessful shutdown."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core_runtime.mcp.transport import McpConnections, _StdioTransport
from core_runtime.mcp import transport as mcp_client


def _child(source: str) -> _StdioTransport:
    return _StdioTransport([sys.executable, "-I", "-u", "-c", source], env={})


def _assert_collected(transport: _StdioTransport, process: subprocess.Popen) -> None:
    assert process.poll() is not None
    assert transport._proc is None
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
    assert not transport._reader_thread.is_alive()
    assert not transport._stderr_thread.is_alive()
    assert not transport._writer_thread.is_alive()


def test_stderr_larger_than_pipe_does_not_block_the_reply() -> None:
    """A real child can emit diagnostics before reading and answering a request."""
    transport = _child(
        "import json, sys\n"
        "sys.stderr.buffer.write(b'diagnostic' * 500_000)\n"
        "request = json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'id': request['id'], 'result': 'ok'}))\n"
    )
    transport.start()
    process = transport._proc
    try:
        transport.send(b'{"id": 1}')
        assert transport.recv(timeout=5) == {"id": 1, "result": "ok"}
        # A queued reply remains readable even if stdout closes immediately after it.
        with pytest.raises(RuntimeError, match="closed"):
            transport.recv(timeout=1)
    finally:
        transport.stop()
    _assert_collected(transport, process)


def test_shutdown_unblocks_a_writer_when_child_does_not_read_stdin() -> None:
    """Closing buffered stdin must not deadlock on the blocked writer's lock."""
    transport = _child("import time; time.sleep(60)")
    transport.start()
    process = transport._proc
    writing, written, stopped = threading.Event(), threading.Event(), threading.Event()
    failures: list[Exception] = []

    def write() -> None:
        writing.set()
        try:
            transport.send(b'x' * (mcp_client._STDIO_FRAME_LIMIT - 1))
        except (BrokenPipeError, ValueError, RuntimeError):
            pass
        finally:
            written.set()

    def stop() -> None:
        try:
            transport.stop()
        except Exception as error:
            failures.append(error)
        finally:
            stopped.set()

    writer = threading.Thread(target=write, daemon=True)
    stopper = threading.Thread(target=stop, daemon=True)
    try:
        writer.start()
        assert writing.wait(2)
        assert not written.wait(0.1)
        stopper.start()
        assert stopped.wait(2), "shutdown waited on buffered stdin before stopping child"
    finally:
        # Also release both threads when this regression is run against the old
        # implementation, so a failed assertion cannot hang the test process.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        writer.join(timeout=2)
        if stopper.ident is not None:
            stopper.join(timeout=2)
        transport.stop()
    assert not failures
    assert not writer.is_alive() and not stopper.is_alive()
    _assert_collected(transport, process)


def test_request_deadline_stops_a_real_child_that_never_reads_stdin(monkeypatch) -> None:
    """A request cannot spend unbounded time writing before waiting for a reply."""
    transport = _child("import time; time.sleep(60)")
    connection = mcp_client._ServerConnection("owned", {})
    connection._transport = transport
    transport.start()
    process = transport._proc
    monkeypatch.setattr(mcp_client, "_DEFAULT_TIMEOUT", 0.2)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(connection.call_tool, "blocked", {"text": "x" * 1_000_000})
        try:
            with pytest.raises(TimeoutError, match="write deadline exceeded"):
                pending.result(timeout=2)
            assert time.monotonic() - started < 2
            process.wait(timeout=2)
            # A partial frame must never be replayed on the same or a new child.
            assert transport._proc is process
            assert connection._transport is transport
            with pytest.raises(RuntimeError, match="stdio transport"):
                connection.call_tool("not-replayed", {})
        finally:
            connection.disconnect()
    _assert_collected(transport, process)


def test_expired_and_oversized_writes_leave_the_connection_usable() -> None:
    """Rejected unsent frames cannot contaminate the next complete request."""
    transport = _child(
        "import sys\n"
        "for line in sys.stdin.buffer:\n"
        " sys.stdout.buffer.write(line); sys.stdout.buffer.flush()\n"
    )
    transport.start()
    process = transport._proc
    writer = transport._writer_thread
    try:
        with pytest.raises(TimeoutError, match="waiting for the MCP writer"):
            transport.send(b'{"id": 100}', deadline=time.monotonic() - 1)
        with pytest.raises(ValueError, match="request exceeds limit"):
            transport.send(b'x' * mcp_client._STDIO_FRAME_LIMIT)
        for request_id in (1, 2):
            transport.send(json.dumps({"id": request_id}).encode())
            assert transport.recv(timeout=2) == {"id": request_id}
            assert transport._writer_thread is writer
            assert writer.is_alive()
    finally:
        transport.stop()
    _assert_collected(transport, process)


def test_writer_cleanup_failure_retains_handles_for_retry() -> None:
    """Shutdown cannot discard the child while an owned pipe writer is alive."""
    transport = _StdioTransport(["unused"])
    process, writer = Mock(), Mock()
    transport._proc = process
    transport._writer_thread = writer
    writer.is_alive.return_value = True
    with pytest.raises(RuntimeError, match="writer cleanup is incomplete"):
        transport.stop()
    assert transport._proc is process
    process.stdin.close.assert_not_called()
    writer.is_alive.return_value = False
    transport.stop()
    assert transport._proc is None
    process.stdin.close.assert_called_once_with()


@pytest.mark.parametrize("output,reason", [
    (f"b'x' * {mcp_client._STDIO_FRAME_LIMIT + 1}", "frame exceeds limit"),
    (f"b'{{}}\\n' * {mcp_client._STDIO_QUEUE_LIMIT + 1}", "queue exceeds limit"),
    ("b'[]\\n'", "message is invalid"),
    ("b'{invalid-json}\\n'", "message is invalid"),
    ("b'\\xff\\n'", "message is invalid"),
])
def test_invalid_or_excessive_stdout_stops_child_without_unbounded_buffering(
    output: str, reason: str,
) -> None:
    """No-newline floods and unsolicited replies have finite receive budgets."""
    transport = _child(
        "import sys, time\n"
        f"sys.stdout.buffer.write({output})\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(60)\n"
    )
    transport.start()
    process = transport._proc
    try:
        assert transport._stdout_closed.wait(5)
        assert transport._queue.qsize() <= mcp_client._STDIO_QUEUE_LIMIT
        with pytest.raises(RuntimeError, match=reason):
            transport.recv(timeout=1)
        with pytest.raises(RuntimeError, match=reason):
            transport.send(b'{"id": 1}')
        # Rejecting the stream requests real child death but preserves ownership.
        process.wait(timeout=2)
        assert transport._proc is process
    finally:
        transport.stop()
    _assert_collected(transport, process)


def test_reader_cleanup_failure_retains_handles_for_retry() -> None:
    """A reaped child alone does not establish that its pipe readers finished."""
    transport = _StdioTransport(["unused"])
    process, reader = Mock(), Mock()
    transport._proc = process
    transport._reader_thread = reader
    reader.is_alive.return_value = True
    with pytest.raises(RuntimeError, match="reader cleanup is incomplete"):
        transport.stop()
    assert transport._proc is process
    process.stdout.close.assert_not_called()
    reader.is_alive.return_value = False
    transport.stop()
    assert transport._proc is None
    process.stdout.close.assert_called_once_with()


@pytest.mark.parametrize("thread_name", ["_stderr_thread", "_writer_thread"])
def test_io_start_failure_does_not_prevent_child_cleanup(monkeypatch, thread_name) -> None:
    """Partially started transports must also close readers that never started."""
    transport = _child("import time; time.sleep(60)")
    start = threading.Thread.start

    def fail_stderr_start(reader: threading.Thread) -> None:
        if reader is getattr(transport, thread_name):
            raise RuntimeError("cannot start diagnostic reader")
        start(reader)

    monkeypatch.setattr(threading.Thread, "start", fail_stderr_start)
    try:
        with pytest.raises(RuntimeError, match="cannot start diagnostic reader"):
            transport.start()
    finally:
        process = transport._proc
        transport.stop()
    _assert_collected(transport, process)


class _BlockingReplies:
    """Hold the first reply while another caller attempts the same connection."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent: list[dict] = []
        self.replies: queue.Queue = queue.Queue()

    def send(self, data: bytes, *, deadline: float | None = None) -> None:
        request = json.loads(data)
        self.sent.append(request)
        self.replies.put({
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": request["params"]["name"]}]},
        })

    def recv(self, timeout: float) -> dict | None:
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(5)
        try:
            return self.replies.get(timeout=timeout)
        except queue.Empty:
            return None


def test_concurrent_calls_keep_each_response_with_its_request() -> None:
    """Two callers cannot consume and discard each other's shared-queue reply."""
    connection = mcp_client._ServerConnection("owned", {})
    transport = _BlockingReplies()
    connection._transport = transport
    attempted = threading.Event()

    def second_call() -> dict:
        attempted.set()
        return connection.call_tool("second", {})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(connection.call_tool, "first", {})
        try:
            assert transport.entered.wait(2)
            second = pool.submit(second_call)
            assert attempted.wait(2)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
            assert len(transport.sent) == 1
        finally:
            transport.release.set()
        assert first.result(timeout=2)["result"] == "first"
        assert second.result(timeout=2)["result"] == "second"
    assert [request["params"]["name"] for request in transport.sent] == ["first", "second"]


def test_busy_connection_does_not_serialize_another_connection() -> None:
    """An unrelated owner's transport continues while the first reply waits."""
    first = mcp_client._ServerConnection("first", {})
    second = mcp_client._ServerConnection("second", {})
    first._transport, second._transport = _BlockingReplies(), _BlockingReplies()
    second._transport.release.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(first.call_tool, "first", {})
        try:
            assert first._transport.entered.wait(2)
            independent = pool.submit(second.call_tool, "second", {})
            assert independent.result(timeout=2)["result"] == "second"
        finally:
            first._transport.release.set()
        assert pending.result(timeout=2)["result"] == "first"


def test_queued_call_expires_without_sending_or_consuming_reply(monkeypatch) -> None:
    """Waiting for the connection uses the request budget without executing."""
    connection = mcp_client._ServerConnection("owned", {})
    connection._transport = Mock()
    monkeypatch.setattr(mcp_client, "_DEFAULT_TIMEOUT", 0.01)
    with connection._request_lock:
        with pytest.raises(TimeoutError, match="waiting for the MCP connection"):
            connection.call_tool("not-sent", {})
    connection._transport.send.assert_not_called()
    connection._transport.recv.assert_not_called()


def test_send_shares_request_deadline_and_late_reply_cannot_succeed(monkeypatch) -> None:
    """Transport IO cannot reset the budget or return success after it expires."""
    connection = mcp_client._ServerConnection("owned", {})
    transport = Mock()
    connection._transport = transport
    clock = [10.0]
    monkeypatch.setattr(mcp_client, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(mcp_client, "_DEFAULT_TIMEOUT", 30)

    def send(data: bytes, *, deadline: float) -> None:
        assert deadline == 40
        assert json.loads(data)["id"] == 1
        clock[0] = 39.9

    def recv(*, timeout: float) -> dict:
        assert timeout == pytest.approx(0.1)
        clock[0] = 40
        return {"id": 1, "result": {"content": [{"type": "text", "text": "late"}]}}

    transport.send.side_effect = send
    transport.recv.side_effect = recv
    with pytest.raises(TimeoutError, match="waiting for the MCP response"):
        connection.call_tool("late", {})
    assert transport.send.call_count == 1
    assert transport.recv.call_count == 1


@pytest.mark.parametrize("next_action", ["connect", "disconnect", "reconnect"])
def test_startup_keeps_ownership_until_lifecycle_operation_finishes(monkeypatch, next_action) -> None:
    """A concurrent mutation must not orphan a connection still starting."""
    owner = McpConnections()
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    connections = []

    class Connection:
        def __init__(self, name, config, *, inherit_environment=True):
            self.alive = False
            connections.append(self)

        def connect(self, *, deadline=None, cancellation=None):
            self.alive = True
            if len(connections) == 1:
                entered.set()
                assert release.wait(5)
            return 1

        def disconnect(self):
            self.alive = False

        def reconnect(self):
            self.disconnect()
            return self.connect()

    monkeypatch.setattr(mcp_client, "_ServerConnection", Connection)

    def next_operation():
        attempted.set()
        if next_action == "connect":
            return owner.connect("owned", {})
        return getattr(owner, next_action)("owned")

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(owner.connect, "owned", {})
        try:
            assert entered.wait(2)
            followup = pool.submit(next_operation)
            assert attempted.wait(2)
            with pytest.raises(TimeoutError):
                followup.result(timeout=0.1)
        finally:
            release.set()
        assert initial.result(timeout=2) == 1
        followup.result(timeout=2)
    alive = [connection for connection in connections if connection.alive]
    assert alive == ([] if next_action == "disconnect" else [owner._servers["owned"]])


def test_failed_startup_retains_the_new_connection_for_cleanup(monkeypatch) -> None:
    """Initialization failure does not imply that a spawned process exited."""
    owner = McpConnections()
    connection = Mock()
    connection.connect.side_effect = OSError("handshake failed after spawn")
    monkeypatch.setattr(mcp_client, "_ServerConnection", Mock(return_value=connection))
    with pytest.raises(RuntimeError, match="Failed to connect"):
        owner.connect("owned", {})
    assert owner._servers["owned"] is connection
    owner.disconnect("owned")
    connection.disconnect.assert_called_once_with()
    assert not owner._servers


def test_owner_close_fences_work_and_retries_only_failed_cleanup(monkeypatch) -> None:
    """One failed child cleanup must not skip the owner's other children."""
    owner = McpConnections()
    failed, healthy = Mock(), Mock()
    failed.disconnect.side_effect = OSError("still running")
    owner._servers.update({"failed": failed, "healthy": healthy})
    factory = Mock()
    monkeypatch.setattr(mcp_client, "_ServerConnection", factory)
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        owner.close()
    assert owner._servers == {"failed": failed}
    healthy.disconnect.assert_called_once_with()
    assert owner.invoke("failed", "tool", {})["is_error"] is True
    failed.call_tool.assert_not_called()
    with pytest.raises(RuntimeError, match="owner is closed"):
        owner.connect("new", {})
    with pytest.raises(RuntimeError, match="owner is closed"):
        owner.reconnect("failed")
    factory.assert_not_called()
    failed.reconnect.assert_not_called()
    failed.disconnect.side_effect = None
    owner.close()
    owner.close()
    assert not owner._servers
    assert failed.disconnect.call_count == 2
    healthy.disconnect.assert_called_once_with()


def test_shutdown_reaps_child_after_termination_timeout() -> None:
    """SIGKILL alone is not proof that a child was collected."""
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("mcp", 5), 0]
    transport = _StdioTransport(["unused"])
    transport._proc = process

    transport.stop()

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2
    assert transport._proc is None


def test_connection_owner_retains_failed_disconnect_for_retry() -> None:
    """Transport ownership must also survive at the server registry boundary."""
    owner = McpConnections()
    connection = Mock()
    connection.disconnect.side_effect = OSError("still running")
    owner._servers["owned"] = connection

    with pytest.raises(OSError, match="still running"):
        owner.disconnect("owned")
    assert owner._servers["owned"] is connection

    connection.disconnect.side_effect = None
    owner.disconnect("owned")
    assert "owned" not in owner._servers


@pytest.mark.parametrize("failure_stage", ["kill", "wait"])
def test_failed_shutdown_retains_child_for_retry(failure_stage: str) -> None:
    """Cleanup failure must not orphan the process handle or claim completion."""
    process = Mock()
    timeout = subprocess.TimeoutExpired("mcp", 5)
    process.wait.side_effect = [timeout, timeout] if failure_stage == "wait" else [timeout]
    if failure_stage == "kill":
        process.kill.side_effect = OSError("kill failed")
    transport = _StdioTransport(["unused"])
    transport._proc = process

    with pytest.raises((OSError, subprocess.TimeoutExpired)):
        transport.stop()
    assert transport._proc is process

    process.wait.side_effect = None
    process.wait.return_value = 0
    process.kill.side_effect = None
    transport.stop()
    assert transport._proc is None
