"""Captured Workflow locators are native request IDs, never dispatch authority."""
from types import SimpleNamespace

import pytest

from core_runtime.workflow_v4.attempt_service import HostWorkflowAttemptServiceV4
from core_runtime.workflow_v4.models import ApprovalState, digest
from tobkiri_host.interactive_effects import PendingEffectState


@pytest.mark.parametrize("effect_state,expected", [
    (PendingEffectState.APPROVAL_PENDING, "interactive-effect-" + "a" * 36),
    (PendingEffectState.APPROVED, None),
    (PendingEffectState.CANCELLED, None),
])
def test_inspect_projects_actual_controller_locator_only_while_pending(effect_state, expected):
    service = HostWorkflowAttemptServiceV4.__new__(HostWorkflowAttemptServiceV4)
    service._config = SimpleNamespace(clock=lambda: 1, security_epoch=7)
    record = {
        "reservation_id": "workflow-attempt." + "b" * 64,
        "request_digest": "sha256:" + "c" * 64,
        "expires_at": 100, "state": "pending", "authority_mode": "interactive_only",
        "effect_id": "pending-effect-" + "a" * 36,
    }
    invocation = object()
    checked = []

    def owned(actual_invocation, reservation_id):
        assert actual_invocation is invocation
        assert reservation_id == record["reservation_id"]
        checked.append("owned")
        return 1, record

    def observe(effect_id):
        assert checked == ["owned"]
        assert effect_id == record["effect_id"]
        return SimpleNamespace(
            state=effect_state, approval_request_id="interactive-effect-" + "a" * 36,
        )

    service._owned = owned
    service._controller = SimpleNamespace(observe_approval=observe)
    reservation = service.inspect(invocation, record["reservation_id"])
    assert reservation.reservation_id == record["reservation_id"]
    assert reservation.approval_request_id == expected
    assert (reservation.state is ApprovalState.WAITING_APPROVAL) == (expected is not None)
    assert "approval_request_id" not in record


def test_reserve_prepares_exact_captured_contract_version():
    service = HostWorkflowAttemptServiceV4.__new__(HostWorkflowAttemptServiceV4)
    invocation = object()
    context = object()
    request = {"request_id": "request", "input": {}, "timeout_ms": 1000, "idempotency_key": "key"}
    run = {"activation_id": "active", "activation_digest": "digest", "security_epoch": 7}
    operation = SimpleNamespace(contract_id="example.contract", contract_version="1.0.0", operation_id="run")
    route = SimpleNamespace(binding=SimpleNamespace(operation=operation))
    captured = []

    class PreparedBoundary(Exception):
        pass

    def prepare(frame, actual_context):
        captured.append(frame)
        assert actual_context is context
        raise PreparedBoundary

    service._config = SimpleNamespace(
        profile_id="profile", activation_id="active", activation_digest="digest",
        security_epoch=7, broker=SimpleNamespace(prepare=prepare),
    )
    service._guard = lambda actual: None
    service._route = lambda actual: route
    service._context = lambda *args: context
    with pytest.raises(PreparedBoundary):
        service.reserve(invocation, run, {"request": request, "request_digest": digest(request)})
    assert len(captured) == 1
    assert captured[0].version_range == "==1.0.0"
