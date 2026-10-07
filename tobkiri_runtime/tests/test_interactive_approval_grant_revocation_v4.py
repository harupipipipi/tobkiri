"""Real-store regression coverage for interactive approval grant assertions."""

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.host_contract import bind_host_contract
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_interactive_approval_v4 import (
    _CONTRACT,
    _adapter,
    _decision_command,
    _request_command,
)


@pytest.mark.parametrize("phrase", ["APPROVE", None])
def test_durable_native_approval_remains_valid_before_revocation(
    tmp_path: Path, phrase: str | None
) -> None:
    """Both supported confirmation flows produce matching durable records."""
    with bind_host_contract(_CONTRACT):
        harness = _Harness(tmp_path)
        adapter = _adapter(harness)
        command = _request_command(harness, phrase=phrase)
        adapter.request_interactive_approval(command)
        adapter.approve_interactive_approval(
            _decision_command(harness, command.context.request_id, phrase=phrase or "")
        )
        harness.kernel.assert_interactive_approval_grant(command.context.request_id)
        decision = harness.store.get_interactive_approval_decision(command.context.request_id)
        assert harness.store.grant_usage(decision.grant_id) == (0, 0)


def test_explicit_grant_revocation_blocks_interactive_authority_assertion(tmp_path: Path) -> None:
    """A revocation-ledger entry invalidates even an unused approved grant."""
    with bind_host_contract(_CONTRACT):
        harness = _Harness(tmp_path)
        adapter = _adapter(harness)
        command = _request_command(harness)
        adapter.request_interactive_approval(command)
        adapter.approve_interactive_approval(_decision_command(harness, command.context.request_id))
        decision = harness.store.get_interactive_approval_decision(command.context.request_id)
        harness.kernel.assert_interactive_approval_grant(command.context.request_id)
        harness.kernel.revoke(
            target_kind="grant",
            target_id=decision.grant_id,
            reason="operator revoked the exact selection",
        )
        assert harness.store.is_revoked("grant", decision.grant_id)
        # Revocation is represented by the ledger, not mutation of GrantRecord.
        assert harness.store.get_grant(decision.grant_id).revoked is False
        with pytest.raises(AuthorityDenied, match="interactive approval"):
            harness.kernel.assert_interactive_approval_grant(command.context.request_id)


def test_missing_durable_approval_blocks_interactive_authority_assertion(tmp_path: Path) -> None:
    """A decision and grant cannot stand in for the required approval record."""
    with bind_host_contract(_CONTRACT):
        harness = _Harness(tmp_path)
        adapter = _adapter(harness)
        command = _request_command(harness)
        adapter.request_interactive_approval(command)
        adapter.approve_interactive_approval(_decision_command(harness, command.context.request_id))
        decision = harness.store.get_interactive_approval_decision(command.context.request_id)
        # Isolated corruption/recovery fixture, not a production write API.
        with harness.store._connection() as connection:
            connection.execute(
                "DELETE FROM authority_records WHERE record_type=? AND record_id=?",
                ("approval", decision.approval_id),
            )
            connection.commit()
        assert harness.store.get_approval(decision.approval_id) is None
        with pytest.raises(AuthorityDenied, match="interactive approval"):
            harness.kernel.assert_interactive_approval_grant(command.context.request_id)
