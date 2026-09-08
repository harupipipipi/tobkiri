"""Atomic selected approval decisions retain the single-request boundaries."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.authority.v4_store import AuditUnavailable, AuthorityStoreError
from core_runtime.host_contract import bind_host_contract
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_interactive_approval_v4 import (
    _CONTRACT,
    _adapter,
    _decision_command,
    _request_command,
)
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.ports import InteractiveApprovalDecisionCommand


def _pending(
    harness: _Harness, adapter: AuthorityV4Adapter,
) -> tuple[InteractiveApprovalDecisionCommand, ...]:
    for request_id in ("batch-first", "batch-second"):
        adapter.request_interactive_approval(
            _request_command(harness, request_id=request_id)
        )
    return tuple(
        _decision_command(harness, request_id, nonce=request_id)
        for request_id in ("batch-first", "batch-second")
    )


def _assert_unsettled(harness: _Harness) -> None:
    for request_id in ("batch-first", "batch-second"):
        assert harness.store.get_interactive_approval_decision(request_id) is None
    assert not [
        g for g in harness.store.list_grants()
        if g.grant_id.startswith("interactive-")
    ]


def test_batch_commits_each_exact_one_shot_and_rejects_replay(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        commands = _pending(harness, adapter)
        results = adapter.approve_interactive_approvals(commands)
        assert [r.state for r in results] == ["approved", "approved"]
        for command in commands:
            harness.kernel.assert_interactive_approval_grant(command.request_id)
        with pytest.raises(AuthorityDenied):
            adapter.approve_interactive_approvals(commands)
    assert len([
        g for g in harness.store.list_grants()
        if g.grant_id.startswith("interactive-")
    ]) == 2


@pytest.mark.parametrize(
    "invalid", ["signature", "snapshot", "phrase", "profile", "session", "duplicate"]
)
def test_bad_selected_item_never_commits_good_item(
    tmp_path: Path, invalid: str,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, second = _pending(harness, adapter)
        if invalid == "signature":
            second = replace(
                second, ui_operator={**second.ui_operator, "signature": "forged"}
            )
        elif invalid == "snapshot":
            second = _decision_command(
                harness, second.request_id, request_snapshot_digest="0" * 64
            )
        elif invalid == "phrase":
            second = replace(second, confirmation_text="wrong")
        elif invalid == "profile":
            second = replace(second, context=replace(second.context, profile_id="other"))
        elif invalid == "session":
            second = replace(
                second, context=replace(second.context, caller_session_id="other")
            )
        else:
            second = first
        with pytest.raises(AuthorityDenied):
            adapter.approve_interactive_approvals((first, second))
        _assert_unsettled(harness)


@pytest.mark.parametrize("failure", ["db", "audit"])
def test_second_insert_failure_rolls_back_decisions_grants_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        commands = _pending(harness, adapter)
        audit_before = harness.store.audit_events()
        original = harness.store._insert_interactive_settlement

        def failing(connection, item):
            original(connection, item)
            if item.decision.request_id == "batch-second":
                if failure == "db":
                    raise sqlite3.OperationalError("injected disk failure")
                raise AuditUnavailable("injected audit failure")

        monkeypatch.setattr(harness.store, "_insert_interactive_settlement", failing)
        with pytest.raises(AuthorityStoreError):
            adapter.approve_interactive_approvals(commands)
        _assert_unsettled(harness)
        assert harness.store.audit_events() == audit_before
        monkeypatch.setattr(harness.store, "_insert_interactive_settlement", original)
        assert len(adapter.approve_interactive_approvals(commands)) == 2


def test_unselected_request_stays_pending(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, _second = _pending(harness, adapter)
        adapter.approve_interactive_approvals((first,))
    assert harness.store.get_interactive_approval_decision("batch-second") is None
