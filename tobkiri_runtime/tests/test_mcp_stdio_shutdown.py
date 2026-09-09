"""MCP stdio child ownership must survive unsuccessful shutdown."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from ecosystem.defaultspack.domain.tool.mcp_client import _StdioTransport


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
