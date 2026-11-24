"""Regression tests for the coding approval-token / pack-approval ordering bug.

When a tool call is retried with an ``approval_token`` and the pack itself is
not yet approved, earlier code consumed the one-shot token at the executor
layer *before* the pack-approval gate ran inside the capability executor.
The user then saw "Pack not approved" and, on retry with the same token,
"approval token has already been used" — even though the approved request
was never actually executed.

The fix defers token consume until after the pack-approval gate passes:
* ``pack_not_approved`` ⇒ token preserved (user can retry after approving the pack)
* successful execution     ⇒ token consumed (one-shot replay protection intact)

These tests pin both halves of that contract.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _coding_git_commit_tool_def():
    return {
        "tool_id": "coding_git_commit",
        "name": "coding_git_commit",
        "risk": "high",
        "requires_approval": True,
        "capability_grants": ["git.write"],
        "execution": {
            "type": "rumi_function",
            "qualified_name": "defaultspack:coding_git_commit",
        },
    }


def _pack_not_approved_executor():
    capability_executor = MagicMock()
    capability_executor._approval_manager.is_pack_approved_and_verified.return_value = (
        False,
        "Pack not approved: defaultspack",
    )
    capability_executor.execute.return_value = SimpleNamespace(
        success=False,
        output=None,
        error="Pack not approved: defaultspack",
        error_type="pack_not_approved",
    )
    return capability_executor


def _success_executor():
    capability_executor = MagicMock()
    capability_executor._approval_manager.is_pack_approved_and_verified.return_value = (
        True,
        None,
    )
    capability_executor.execute.return_value = SimpleNamespace(
        success=True,
        output={"status": "ok", "data": {"commit_hash": "abc1234"}},
        error=None,
        error_type=None,
    )
    return capability_executor


def _approve_tool_call(tool_def, args):
    """Drive the executor through the deny path to obtain a real signed token."""
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    capability_executor = _pack_not_approved_executor()
    first = ToolExecutor()._execute_rumi_function(
        tool_def,
        args,
        {"principal_id": "defaultspack", "capability_executor": capability_executor},
    )
    assert first["is_error"] is False
    assert first["widget"]["type"] == "approval_request"
    decision = approval.approve(first["widget"]["approval_request_id"])
    assert decision["approved"] is True
    return decision["token"]


def test_pack_not_approved_does_not_consume_approval_token(monkeypatch):
    """The reported bug: token must survive a pre-execution pack-approval reject."""
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.delenv("RUMI_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RUMI_AUTO_APPROVE_LOCAL", raising=False)
    approval.reset_approval_state_for_tests()

    tool_def = _coding_git_commit_tool_def()
    args = {"message": "partial commit", "paths": ["a.txt", "b.txt"]}
    token = _approve_tool_call(tool_def, args)

    # First retry with a valid token, but the pack is still not approved.
    capability_executor = _pack_not_approved_executor()
    blocked = ToolExecutor()._execute_rumi_function(
        tool_def,
        {**args, "approval_token": token},
        {"principal_id": "defaultspack", "capability_executor": capability_executor},
    )

    # User sees the pack-not-approved denial widget.
    assert blocked["is_error"] is True
    assert blocked["widget"] == {
        "type": "tool_execution_denied",
        "tool_name": "coding_git_commit",
        "reason": "Pack not approved: defaultspack",
    }

    # But the *one-shot* token must NOT have been consumed: the executor
    # never actually ran the function. The verification (without consume)
    # must still come back valid.
    args_hash = approval.hash_arguments(args)
    verification = approval.verify_execution_token(
        token, "tool.coding_git_commit", args_hash, consume=False,
    )
    assert verification.valid is True, (
        "approval token must survive a pre-execution Pack not approved reject "
        "so the user can retry without re-issuing approval"
    )

    # Now the user (or operator) approves the pack, so the executor returns
    # success on retry. The same token must work, then be consumed.
    success_executor = _success_executor()
    accepted = ToolExecutor()._execute_rumi_function(
        tool_def,
        {**args, "approval_token": token},
        {"principal_id": "defaultspack", "capability_executor": success_executor},
    )
    assert accepted["is_error"] is False
    assert success_executor.execute.call_count == 1
    _, request = success_executor.execute.call_args.args
    assert request["context"]["_tool_server_approved"] is True

    # And after a real execution the token is consumed (replay protection).
    replay_executor = _success_executor()
    replayed = ToolExecutor()._execute_rumi_function(
        tool_def,
        {**args, "approval_token": token},
        {"principal_id": "defaultspack", "capability_executor": replay_executor},
    )
    assert replayed["is_error"] is True
    assert "already been used" in replayed["result"]
    replay_executor.execute.assert_not_called()


def test_successful_execution_consumes_approval_token(monkeypatch):
    """Round-trip replay protection: one-shot token must be burnt on success."""
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    monkeypatch.delenv("RUMI_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RUMI_AUTO_APPROVE_LOCAL", raising=False)
    approval.reset_approval_state_for_tests()

    tool_def = _coding_git_commit_tool_def()
    args = {"message": "happy path", "paths": ["a.txt"]}
    token = _approve_tool_call(tool_def, args)

    success_executor = _success_executor()
    first = ToolExecutor()._execute_rumi_function(
        tool_def,
        {**args, "approval_token": token},
        {"principal_id": "defaultspack", "capability_executor": success_executor},
    )
    assert first["is_error"] is False

    # Replay must fail with token-used.
    replay_executor = _success_executor()
    replay = ToolExecutor()._execute_rumi_function(
        tool_def,
        {**args, "approval_token": token},
        {"principal_id": "defaultspack", "capability_executor": replay_executor},
    )
    assert replay["is_error"] is True
    assert "already been used" in replay["result"]
    replay_executor.execute.assert_not_called()


def test_invalid_approval_token_is_not_consumed():
    """A bogus token must be rejected without touching the consume store."""
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    tool_def = _coding_git_commit_tool_def()
    args = {"message": "nope", "paths": ["a.txt"]}

    bogus = "not-a-real.token"
    capability_executor = MagicMock()
    result = ToolExecutor()._execute_rumi_function(
        tool_def,
        {**args, "approval_token": bogus},
        {"principal_id": "defaultspack", "capability_executor": capability_executor},
    )

    assert result["is_error"] is True
    capability_executor.execute.assert_not_called()
