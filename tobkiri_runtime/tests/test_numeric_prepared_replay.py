"""Real Broker/controller + encrypted store regressions, fake approval/backend.

This is provisional overlay validation, never sealed/native production proof.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.workflow_v4.attempt_store import WorkflowAttemptStoreV4
from core_runtime.workflow_v4.models import WorkflowDenied
from tests import test_tobkiri_host_execution_integration as support
from tests.test_interactive_effects import _Approvals, _controller, _scope
from tobkiri_host.broker import PreparedInvocationSnapshot
from tobkiri_host.contracts import (
    AdapterPlanner, OperationCatalog, StructuralAdapter, schema_digest,
)
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.interactive_effects import PendingEffectState
from tests.test_numeric_workflow_paths import assert_float_bits

NUMBER_SCHEMA = {
    "type": "object", "properties": {"value": {"type": "number"}},
    "required": ["value"], "additionalProperties": False,
}


class HalveAdapter:
    """Test transport for one real structural-adapter plan."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, adapter: Any, payload: Any) -> dict[str, Any]:
        self.calls += 1
        return {"value": payload["value"] / 2}


@pytest.fixture
def broker_case(monkeypatch: pytest.MonkeyPatch):
    """Make a real Broker with exact numeric schema and a counted adapter."""
    monkeypatch.setattr(support, "INPUT_SCHEMA", NUMBER_SCHEMA)
    fixture = support.make_broker(timeout_ms=1000)
    binding = fixture.broker._catalog.resolve(support.frame().contract_id, "send", ">=1,<2")
    adapter = StructuralAdapter(
        adapter_id="numeric.halve", artifact_digest=support.digest("numeric-adapter"),
        source_schema_digest=schema_digest(NUMBER_SCHEMA),
        target_schema_digest=schema_digest(NUMBER_SCHEMA),
        source_schema=NUMBER_SCHEMA, target_schema=NUMBER_SCHEMA,
    )
    fixture.broker._catalog = OperationCatalog(
        (binding.artifact,),
        (replace(binding.route, adapter_ids=(adapter.adapter_id,)),),
    )
    executor = HalveAdapter()
    fixture.broker._adapters = AdapterPlanner((adapter,))
    fixture.broker._adapter_executor = executor
    try:
        yield fixture, executor
    finally:
        fixture.broker.close()


def prepare_fraction(fixture: Any):
    """Prepare through schema validation and the counted normalizing adapter."""
    return fixture.broker.prepare(
        replace(support.frame(), payload={"value": 0.75}), support.context(),
    )


def test_fractional_normalized_snapshot_approval_restart_resume_replay_once(broker_case: Any, tmp_path: Path) -> None:
    fixture, executor = broker_case
    path = tmp_path / "private" / "attempts.enc"
    persistence = WorkflowAttemptStoreV4(path)
    approvals = _Approvals()
    controller = _controller(persistence, approvals)
    prepared = prepare_fraction(fixture)
    expected_digest = prepared.request_digest
    pending = controller.prepare(
        prepared=prepared, context=support.context(),
        effect_scope=_scope(expected_digest, plan_digest=support.context().plan_digest),
        invocation_owner_id="owner-1",
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        presentation_metadata={"summary": "Synthetic numeric test", "confirmation_phrase": "SEND"},
        expires_at=1000.0, typed_confirmation_phrase="SEND",
    )
    assert executor.calls == 1
    assert fixture.backend.invocations == 0
    assert fixture.events == []
    assert b"normalized_payload" not in path.read_bytes()
    assert b"0.375" not in path.read_bytes()

    reopened = WorkflowAttemptStoreV4(path)
    restored = _controller(reopened, approvals)
    stored = reopened.get_host_pending_effect(pending.effect_id)
    assert stored is not None
    snapshot = PreparedInvocationSnapshot.from_dict(stored[1]["prepared"])
    assert_float_bits(snapshot.normalized_payload["value"], 0.375)
    assert snapshot.request_digest == expected_digest
    assert executor.calls == 1
    assert fixture.broker.validate_prepared_snapshot(snapshot, support.context()).request_digest == expected_digest

    observations: list[float] = []
    original_invoke = fixture.backend.invoke

    def observe(envelope: Any) -> Any:
        assert restored.status(pending.effect_id).state is PendingEffectState.DISPATCHED
        observations.append(envelope.payload["value"])
        return original_invoke(envelope)

    fixture.backend.invoke = observe
    approvals.approve(pending.approval_request_id)
    succeeded = restored.resume(
        pending.effect_id, fixture.broker,
        wall_clock=lambda: 100.0, synchronous_dispatch=True,
    )
    assert succeeded.state is PendingEffectState.SUCCEEDED
    assert fixture.backend.invocations == 1
    assert executor.calls == 1
    assert_float_bits(observations[0], 0.375)
    again = _controller(WorkflowAttemptStoreV4(path), approvals)
    replay = again.resume_for_presentation(
        effect_id=pending.effect_id,
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        broker=fixture.broker,
    )
    assert replay.state is PendingEffectState.SUCCEEDED
    assert fixture.backend.invocations == 1
    assert executor.calls == 1


@pytest.mark.parametrize("field", ["normalized_payload", "request_digest", "binding_fingerprint", "context_fingerprint"])
def test_changed_fractional_snapshot_fails_before_broker_admission(broker_case: Any, field: str) -> None:
    fixture, executor = broker_case
    payload = prepare_fraction(fixture).to_snapshot().to_dict()
    if field == "normalized_payload":
        payload[field] = {"value": 0.5}
    elif field == "request_digest":
        payload[field] = support.digest("wrong-request")
    else:
        payload[field] = {**payload[field], "unexpected": "tamper"}
    snapshot = PreparedInvocationSnapshot.from_dict(payload)
    with pytest.raises(AuthorizationError):
        fixture.broker.invoke_prepared(snapshot, support.context(), {}, execute_not_after_wall=200.0, wall_clock=lambda: 100.0)
    assert fixture.events == []
    assert fixture.backend.invocations == 0
    assert executor.calls == 1


@pytest.mark.parametrize("field", ["binding_fingerprint", "context_fingerprint"])
def test_fractional_authority_fingerprint_stays_forbidden(broker_case: Any, field: str) -> None:
    fixture, executor = broker_case
    payload = prepare_fraction(fixture).to_snapshot().to_dict()
    payload[field]["numeric_injection"] = 0.375
    with pytest.raises(ValueError):
        PreparedInvocationSnapshot.from_dict(payload)
    assert fixture.backend.invocations == 0
    assert executor.calls == 1


def test_encrypted_snapshot_tamper_fails_before_decode_or_dispatch(broker_case: Any, tmp_path: Path) -> None:
    fixture, executor = broker_case
    path = tmp_path / "private" / "attempts.enc"
    persistence = WorkflowAttemptStoreV4(path)
    prepared = prepare_fraction(fixture)
    controller = _controller(persistence, _Approvals())
    controller.prepare(
        prepared=prepared, context=support.context(),
        effect_scope=_scope(prepared.request_digest, plan_digest=support.context().plan_digest),
        invocation_owner_id="owner-1",
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        presentation_metadata={"summary": "Synthetic numeric tamper test"},
        expires_at=1000.0,
    )
    ciphertext = bytearray(path.read_bytes())
    ciphertext[len(ciphertext) // 2] ^= 1
    path.write_bytes(ciphertext)
    with pytest.raises(WorkflowDenied, match="unavailable"):
        WorkflowAttemptStoreV4(path)
    assert fixture.backend.invocations == 0
    assert fixture.events == []
    assert executor.calls == 1
