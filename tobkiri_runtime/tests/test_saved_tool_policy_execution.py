"""Elevated preference fails closed without formal native settlement ports."""

from types import SimpleNamespace
import pytest
import tobkiri_host.saved_tool_policy_execution as module


@pytest.mark.parametrize("mode", ["agent", "full"])
def test_preference_cannot_supply_missing_host_policy(mode, monkeypatch) -> None:
    monkeypatch.setattr(module, "saved_tool_owner", lambda invocation: object())
    invocation = SimpleNamespace(
        parent_invocation=SimpleNamespace(
            parent=None,
            assert_current=lambda: None,
            envelope=SimpleNamespace(
                contract_id="conversation.saved-turn.v1",
                operation_id="saved_complete",
                payload={"request": {"turn_id": "real-turn", "action_approval_mode": mode}},
            ),
        )
    )
    gate = module.SavedToolApprovalExecution(
        ask=None,
        broker=None,
        authority=None,
        bind_nested=None,
        entry_guard_registry=None,
    )
    with pytest.raises(PermissionError, match="settlement unavailable"):
        gate(invocation, {}, {})
