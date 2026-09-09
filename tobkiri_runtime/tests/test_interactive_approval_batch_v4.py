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

pytestmark = pytest.mark.contract


def _pending(
    harness: _Harness,
    adapter: AuthorityV4Adapter,
) -> tuple[InteractiveApprovalDecisionCommand, ...]:
    for request_id in ("batch-first", "batch-second"):
        adapter.request_interactive_approval(_request_command(harness, request_id=request_id))
    return tuple(
        _decision_command(harness, request_id, nonce=request_id)
        for request_id in ("batch-first", "batch-second")
    )


def _assert_unsettled(harness: _Harness) -> None:
    for request_id in ("batch-first", "batch-second"):
        assert harness.store.get_interactive_approval_decision(request_id) is None
    assert not [g for g in harness.store.list_grants() if g.grant_id.startswith("interactive-")]


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
    assert (
        len([g for g in harness.store.list_grants() if g.grant_id.startswith("interactive-")]) == 2
    )


@pytest.mark.parametrize(
    "invalid", ["signature", "snapshot", "phrase", "profile", "session", "duplicate"]
)
def test_bad_selected_item_never_commits_good_item(
    tmp_path: Path,
    invalid: str,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, second = _pending(harness, adapter)
        if invalid == "signature":
            second = replace(second, ui_operator={**second.ui_operator, "signature": "forged"})
        elif invalid == "snapshot":
            second = _decision_command(harness, second.request_id, request_snapshot_digest="0" * 64)
        elif invalid == "phrase":
            second = replace(second, confirmation_text="wrong")
        elif invalid == "profile":
            second = replace(second, context=replace(second.context, profile_id="other"))
        elif invalid == "session":
            second = replace(second, context=replace(second.context, caller_session_id="other"))
        else:
            second = first
        with pytest.raises(AuthorityDenied):
            adapter.approve_interactive_approvals((first, second))
        _assert_unsettled(harness)


@pytest.mark.parametrize("failure", ["db", "audit"])
def test_second_insert_failure_rolls_back_decisions_grants_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
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


@pytest.mark.parametrize("approved", [True, False])
def test_frozen_selection_uses_one_proof_and_settles_all_or_none(
    tmp_path: Path,
    approved: bool,
) -> None:
    from core_runtime.authority.ui_operator import sign_ui_operator

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, second = _pending(harness, adapter)
        batch = adapter.create_interactive_approval_batch(
            first.context, (first.request_id, second.request_id)
        )
        command = replace(
            first,
            request_id=str(batch["request_id"]),
            ui_operator=sign_ui_operator(
                str(batch["request_id"]),
                decision="approve" if approved else "deny",
                request_snapshot_digest=str(batch["request_snapshot_digest"]),
            ),
        )
        result = adapter.settle_interactive_approval_batch(
            command,
            approved=approved,
            confirmation_texts={first.request_id: "APPROVE", second.request_id: "APPROVE"}
            if approved
            else {},
        )
        assert result["state"] == ("approved" if approved else "denied")
        with pytest.raises(AuthorityDenied):
            adapter.settle_interactive_approval_batch(
                command,
                approved=approved,
                confirmation_texts={first.request_id: "APPROVE", second.request_id: "APPROVE"}
                if approved
                else {},
            )


def test_individual_signature_cannot_approve_frozen_selection(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, second = _pending(harness, adapter)
        batch = adapter.create_interactive_approval_batch(
            first.context, (first.request_id, second.request_id)
        )
        with pytest.raises(AuthorityDenied):
            adapter.settle_interactive_approval_batch(
                replace(first, request_id=str(batch["request_id"])),
                approved=True,
                confirmation_texts={first.request_id: "APPROVE", second.request_id: "APPROVE"},
            )
        _assert_unsettled(harness)


@pytest.mark.parametrize("invalid", ["missing", "extra", "wrong"])
def test_batch_does_not_bypass_individual_typed_confirmation(
    tmp_path: Path,
    invalid: str,
) -> None:
    from core_runtime.authority.ui_operator import sign_ui_operator

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, second = _pending(harness, adapter)
        batch = adapter.create_interactive_approval_batch(
            first.context, (first.request_id, second.request_id)
        )
        confirmations = {first.request_id: "APPROVE", second.request_id: "APPROVE"}
        if invalid == "missing":
            confirmations.pop(second.request_id)
        elif invalid == "extra":
            confirmations["unselected"] = "APPROVE"
        else:
            confirmations[second.request_id] = "WRONG"
        command = replace(
            first,
            request_id=str(batch["request_id"]),
            ui_operator=sign_ui_operator(
                str(batch["request_id"]),
                decision="approve",
                request_snapshot_digest=str(batch["request_snapshot_digest"]),
            ),
        )
        with pytest.raises(AuthorityDenied):
            adapter.settle_interactive_approval_batch(
                command,
                approved=True,
                confirmation_texts=confirmations,
            )
        _assert_unsettled(harness)


def test_batch_rejects_cross_session_read(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        first, second = _pending(harness, adapter)
        batch = adapter.create_interactive_approval_batch(
            first.context, (first.request_id, second.request_id)
        )
        with pytest.raises(AuthorityDenied):
            adapter.get_interactive_approval_batch(
                replace(first.context, caller_session_id="foreign-session"),
                str(batch["request_id"]),
            )


def test_frozen_batch_model_rejects_duplicate_and_keeps_canonical_selection() -> None:
    from core_runtime.authority.v4 import (
        AuthorityValidationError,
        InteractiveApprovalBatch,
        authority_digest,
    )

    digest = authority_digest({"snapshot": "test"})
    batch = InteractiveApprovalBatch(
        request_id="batch-model",
        request_snapshots=(("second", digest), ("first", digest)),
        profile_id="profile",
        presentation_owner_principal_id=digest,
        presentation_owner_session_id="session",
        security_epoch=1,
        created_at=1.0,
        expires_at=2.0,
    )
    assert batch == InteractiveApprovalBatch.from_dict(batch.to_dict())
    assert (
        batch.digest
        == replace(batch, request_snapshots=tuple(reversed(batch.request_snapshots))).digest
    )
    with pytest.raises(AuthorityValidationError):
        replace(batch, request_snapshots=(("first", digest), ("first", digest)))


def _frozen_command(harness, adapter, *, long_expiry=False):
    from core_runtime.authority.ui_operator import sign_ui_operator

    for request_id in ("batch-first", "batch-second"):
        request = _request_command(harness, request_id=request_id)
        if long_expiry:
            request = replace(request, expires_at=harness.clock() + 600)
        adapter.request_interactive_approval(request)
    first = _decision_command(harness, "batch-first")
    batch = adapter.create_interactive_approval_batch(
        first.context, ("batch-first", "batch-second")
    )
    return replace(
        first,
        request_id=str(batch["request_id"]),
        ui_operator=sign_ui_operator(
            str(batch["request_id"]),
            decision="approve",
            request_snapshot_digest=str(batch["request_snapshot_digest"]),
        ),
    ), batch


def test_frozen_batch_expiring_after_final_insert_rolls_back_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        command, batch = _frozen_command(harness, adapter, long_expiry=True)
        original = harness.store._insert_interactive_settlement
        audit_before = harness.store.audit_events()

        def expire_after_last(connection, item):
            original(connection, item)
            if item.decision.request_id == "batch-second":
                harness.clock.value = float(batch["expires_at"])

        monkeypatch.setattr(harness.store, "_insert_interactive_settlement", expire_after_last)
        with pytest.raises(AuthorityDenied, match="batch is expired"):
            adapter.settle_interactive_approval_batch(
                command,
                approved=True,
                confirmation_texts={"batch-first": "APPROVE", "batch-second": "APPROVE"},
            )
        _assert_unsettled(harness)
        assert harness.store.audit_events() == audit_before


@pytest.mark.parametrize("failure", ["db", "audit"])
def test_frozen_batch_second_insert_failure_rolls_back_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        command, _batch = _frozen_command(harness, adapter)
        original = harness.store._insert_interactive_settlement
        audit_before = harness.store.audit_events()

        def fail_after_last(connection, item):
            original(connection, item)
            if item.decision.request_id == "batch-second":
                if failure == "db":
                    raise sqlite3.OperationalError("injected batch DB failure")
                raise AuditUnavailable("injected batch audit failure")

        monkeypatch.setattr(harness.store, "_insert_interactive_settlement", fail_after_last)
        with pytest.raises(AuthorityStoreError):
            adapter.settle_interactive_approval_batch(
                command,
                approved=True,
                confirmation_texts={"batch-first": "APPROVE", "batch-second": "APPROVE"},
            )
        _assert_unsettled(harness)
        assert harness.store.audit_events() == audit_before


def test_frozen_batch_racing_individual_decision_cannot_grant_remainder(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        command, _batch = _frozen_command(harness, adapter)
        adapter.deny_interactive_approval(_decision_command(harness, "batch-second", action="deny"))
        audit_before = harness.store.audit_events()
        with pytest.raises(AuthorityDenied):
            adapter.settle_interactive_approval_batch(
                command,
                approved=True,
                confirmation_texts={"batch-first": "APPROVE", "batch-second": "APPROVE"},
            )
        assert harness.store.get_interactive_approval_decision("batch-first") is None
        assert harness.store.audit_events() == audit_before


@pytest.mark.parametrize("invalid", ["digest", "profile"])
def test_frozen_batch_rejects_forged_snapshot_and_foreign_profile(
    tmp_path: Path,
    invalid: str,
) -> None:
    from core_runtime.authority.ui_operator import sign_ui_operator

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        command, batch = _frozen_command(harness, adapter)
        if invalid == "digest":
            command = replace(
                command,
                ui_operator=sign_ui_operator(
                    str(batch["request_id"]),
                    decision="approve",
                    request_snapshot_digest="f" * 64,
                ),
            )
        else:
            command = replace(command, context=replace(command.context, profile_id="foreign"))
        with pytest.raises(AuthorityDenied):
            adapter.settle_interactive_approval_batch(
                command,
                approved=True,
                confirmation_texts={"batch-first": "APPROVE", "batch-second": "APPROVE"},
            )
        _assert_unsettled(harness)
