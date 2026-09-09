"""MCP stdio child ownership must survive unsuccessful shutdown."""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from unittest.mock import Mock

import pytest

from ecosystem.defaultspack.domain.tool.mcp_client import McpConnections, _StdioTransport
from ecosystem.defaultspack.domain.tool import mcp_client


@pytest.mark.parametrize("next_action", ["connect", "disconnect", "reconnect"])
def test_startup_keeps_ownership_until_lifecycle_operation_finishes(monkeypatch, next_action) -> None:
    """A concurrent mutation must not orphan a connection still starting."""
    owner = McpConnections()
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    connections = []

    class Connection:
        def __init__(self, name, config):
            self.alive = False
            connections.append(self)

        def connect(self):
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
