"""Regressions for removing approved Pack roots with signed dependencies."""

from __future__ import annotations

import json

import pytest

import core_runtime.pack_control_v4 as pack_control
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_active_profile,
    capture_default_profile,
)
from tests.test_pack_control_v4 import (
    _capture_control_session,
    _invoke,
)
from tests.test_pack_control_v4 import (
    captured_session as captured_session,  # noqa: PLC0414 - pytest fixture export
)

SCHEDULER_PACK = "rumi_scheduler_tool_adapter_pack"
SCHEDULER_SURFACE = "rumi_scheduler_surface_pack"
SCHEDULER_RUNTIME = "rumi_scheduler_runtime_pack"
SCHEDULE_STORE = "rumi_schedule_store_pack"


def _approve(session, pack_id: str) -> None:
    """Install and approve one independently selected Pack root."""
    _invoke(session, "pack.install", {"pack_id": pack_id})
    candidate = _invoke(session, "approval.candidate", {"pack_id": pack_id})
    _invoke(
        session,
        "approval.approve",
        {"pack_id": pack_id, "candidate_id": candidate["candidate_id"]},
    )


def _enabled_pack_ids() -> set[str]:
    """Read the actual active composition, not only its approval projection."""
    return {
        str(item["pack_id"])
        for item in capture_active_profile().resolved.profile["packs"]
    }


def test_revoke_root_removes_dependency_only_packs(captured_session) -> None:
    """Revoke must not promote the removed root's dependencies to new roots."""
    session, _state_path, user_data = captured_session
    baseline = _enabled_pack_ids()
    _approve(session, SCHEDULER_PACK)
    _invoke(session, "pack.enable", {"pack_id": SCHEDULER_PACK})
    assert {SCHEDULER_RUNTIME, SCHEDULE_STORE} <= _enabled_pack_ids()
    assert SCHEDULER_RUNTIME not in pack_control._read_control_state("defaults")

    result = _invoke(session, "approval.revoke", {"pack_id": SCHEDULER_PACK})

    assert result["approved"] is False
    assert result["enabled"] is False
    assert result["approval_status"] == "revoked"
    assert _enabled_pack_ids() == baseline
    receipt_path = (
        user_data / "pack_control" / "approvals" / "defaults"
        / f"{SCHEDULER_PACK}.json"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["revoked"] is True
    restarted = _capture_control_session()
    status = _invoke(restarted, "pack.status", {"pack_id": SCHEDULER_PACK})
    assert status["approval_reason"] == "approval_revoked"
    assert status["enabled"] is False
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        assert authority.is_revoked("approval", result["approval_revision"])
        events = [
            event for event in authority.audit_events()
            if event["event_type"] == "pack_approval_revoked"
        ]
    assert len(events) == 1
    assert events[0]["event_state"] == "committed"
    assert events[0]["payload"]["pack_id"] == SCHEDULER_PACK


@pytest.mark.parametrize("operation", ["pack.disable", "approval.revoke"])
def test_removing_root_preserves_shared_dependency_closure(
    captured_session, operation: str
) -> None:
    """A surviving approved surface continues to own its shared dependencies."""
    session, _state_path, _user_data = captured_session
    for pack_id in (SCHEDULER_PACK, SCHEDULER_SURFACE):
        _approve(session, pack_id)
        _invoke(session, "pack.enable", {"pack_id": pack_id})
    _invoke(session, operation, {"pack_id": SCHEDULER_PACK})

    enabled = _enabled_pack_ids()
    assert SCHEDULER_PACK not in enabled
    assert {SCHEDULER_SURFACE, SCHEDULER_RUNTIME, SCHEDULE_STORE} <= enabled
    status = _invoke(
        _capture_control_session(), "pack.status", {"pack_id": SCHEDULER_SURFACE}
    )
    assert status["enabled"] is True
    assert status["approved"] is True
    # Removing the final covering root then drops the dependency-only Packs.
    _invoke(session, "pack.disable", {"pack_id": SCHEDULER_SURFACE})
    assert not {SCHEDULER_SURFACE, SCHEDULER_RUNTIME, SCHEDULE_STORE} & (
        _enabled_pack_ids()
    )


def test_revoke_root_preserves_independently_approved_dependency(
    captured_session,
) -> None:
    """A dependency explicitly installed and enabled has an independent root."""
    session, _state_path, _user_data = captured_session
    for pack_id in (SCHEDULER_RUNTIME, SCHEDULER_PACK):
        _approve(session, pack_id)
        _invoke(session, "pack.enable", {"pack_id": pack_id})

    _invoke(session, "approval.revoke", {"pack_id": SCHEDULER_PACK})

    enabled = _enabled_pack_ids()
    assert SCHEDULER_PACK not in enabled
    assert {SCHEDULER_RUNTIME, SCHEDULE_STORE} <= enabled
    assert _invoke(
        _capture_control_session(), "pack.status", {"pack_id": SCHEDULER_RUNTIME}
    )["enabled"] is True


def test_revoke_covered_dependency_denies_before_authority_commit(
    captured_session,
) -> None:
    """Reject removal of a still-required dependency before revoking its grants."""
    session, _state_path, user_data = captured_session
    for pack_id in (SCHEDULER_RUNTIME, SCHEDULER_PACK):
        _approve(session, pack_id)
        _invoke(session, "pack.enable", {"pack_id": pack_id})
    receipt_path = (
        user_data / "pack_control" / "approvals" / "defaults"
        / f"{SCHEDULER_RUNTIME}.json"
    )
    receipt_before = receipt_path.read_bytes()
    revision = json.loads(receipt_before)["approval_revision"]
    activation_before = capture_default_profile().activation["activation_id"]

    with pytest.raises(pack_control.PackControlConflict, match="required by"):
        _invoke(session, "approval.revoke", {"pack_id": SCHEDULER_RUNTIME})

    assert receipt_path.read_bytes() == receipt_before
    assert capture_default_profile().activation["activation_id"] == activation_before
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        assert not authority.is_revoked("approval", revision)
        assert not [
            event for event in authority.audit_events()
            if event["event_type"] == "pack_approval_revoked"
        ]


def test_post_commit_revoke_failure_is_truthful_and_restart_can_disable(
    captured_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted Profile update cannot restore a durably revoked approval."""
    session, _state_path, user_data = captured_session
    baseline = _enabled_pack_ids()
    _approve(session, SCHEDULER_PACK)
    _invoke(session, "pack.enable", {"pack_id": SCHEDULER_PACK})
    receipt_path = (
        user_data / "pack_control" / "approvals" / "defaults"
        / f"{SCHEDULER_PACK}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    def fail_activation(*_args, **_kwargs) -> None:
        raise OSError("injected activation failure after authority commit")

    with monkeypatch.context() as patch:
        patch.setattr(pack_control, "_activate_pack_set", fail_activation)
        with pytest.raises(pack_control.PackControlOutcomeUnknown):
            _invoke(session, "approval.revoke", {"pack_id": SCHEDULER_PACK})

    assert SCHEDULER_PACK in _enabled_pack_ids()
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        assert authority.is_revoked("approval", receipt["approval_revision"])
    with pytest.raises(pack_control.PackControlConflict, match="approval_revoked"):
        capture_default_profile()
    restarted = _capture_control_session()
    status = _invoke(restarted, "pack.status", {"pack_id": SCHEDULER_PACK})
    assert status["approval_reason"] == "approval_revoked"
    assert status["enabled"] is False
    assert status["approved"] is False
    assert all(not operation["invokable"] for operation in status["operations"])
    with pytest.raises(pack_control.PackControlConflict, match="approval_revoked"):
        _invoke(restarted, "approval.revoke", {"pack_id": SCHEDULER_PACK})
    _invoke(restarted, "pack.disable", {"pack_id": SCHEDULER_PACK})
    assert _enabled_pack_ids() == baseline
    assert SCHEDULER_PACK not in {
        str(item["pack_id"])
        for item in capture_default_profile().resolved.profile["packs"]
    }
