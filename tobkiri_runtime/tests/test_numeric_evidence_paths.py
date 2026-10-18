"""Real Workflow evidence/registry tests while fixture consumer dispatch is live.

Provisional overlay validation only; no sealed production capture/backend claim.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from core_runtime.invocation_evidence import (
    EvidenceBinding, EvidenceDenied, InvocationEvidenceRegistry,
)
from core_runtime.workflow_v4.evidence import EVIDENCE_KIND, WorkflowAttemptEvidence
from tobkiri_protocol.data_codec import digest_payload
from tests.test_numeric_workflow_paths import (
    Runtime, VALUES, assert_float_bits, execute, runtime as runtime, start,
)


def binding_for(runtime: Runtime, request: Any) -> EvidenceBinding:
    """Bind evidence to the actual live consumer request and authenticated run."""
    run = runtime.store.get_run("numeric-run")
    return EvidenceBinding(
        issuer_principal="fixture.workflow.issuer",
        target_principal=request["function_principal_id"],
        contract_id=request["contract_id"], contract_version="1.0.0",
        operation_id=request["operation_id"],
        payload_digest=digest_payload(request["input"]),
        idempotency_key=request["idempotency_key"],
        profile_id="test-profile", plan_digest="sha256:" + "9" * 64,
        activation_id=run["activation_id"],
        activation_digest=run["activation_digest"],
        security_epoch=run["security_epoch"],
        presentation_owner_principal="fixture.owner",
        presentation_owner_session="fixture.session",
    )


def during_consumer(runtime: Runtime, callback: Any) -> None:
    """Inspect evidence before the real engine commits the consumer outcome."""
    original = runtime.invoker.invoke

    def invoke(request: Any, **kwargs: Any) -> Any:
        outcome = original(request, **kwargs)
        if request["operation_id"] == "consume":
            attempt = runtime.store.get_attempt_for_request(request["request_id"])
            assert attempt["state"] == "running"
            assert attempt["dispatch_admission"] == "admitted"
            callback(request)
        return outcome

    runtime.invoker.invoke = invoke


@pytest.mark.parametrize("value", VALUES, ids=["fraction", "negative-zero", "subnormal"])
def test_verified_numeric_value_and_lineage_survive_restart_and_scope_close(runtime: Runtime, value: float) -> None:
    start(runtime, value)
    produced = execute(runtime, "produce")
    runtime.restart()
    observed: list[Any] = []

    def inspect(request: Any) -> None:
        binding = binding_for(runtime, request)
        registry = InvocationEvidenceRegistry()
        registry.register(binding.issuer_principal, EVIDENCE_KIND, lambda reference, identity: WorkflowAttemptEvidence(runtime.store, reference, identity), data_fields=("value",))
        calls = {"issuer": 0, "receiver": 0}

        def issuer_guard() -> None:
            calls["issuer"] += 1

        def receiver_guard() -> None:
            calls["receiver"] += 1

        with registry.dispatch(kind=EVIDENCE_KIND, reference=request["request_id"], binding=binding, guard=issuer_guard):
            receiver = object()
            evidence = registry.receive(kind=EVIDENCE_KIND, binding=binding, receiver=receiver, guard=receiver_guard)
            current = evidence.read("current")
            result = evidence.read("input:/value")
            assert result["run_id"] == "numeric-run"
            assert result["attempt_id"] == "numeric-run:consume:1"
            assert result["request_digest"] == current["request_digest"]
            assert result["run_inputs_digest"] == digest_payload({"value": value})
            assert result["source_step"] == "produce"
            assert result["source_path"] == "value"
            assert_float_bits(result["value"], value)
            assert digest_payload({"value": result["value"]}) == binding.payload_digest
            assert result["lineage"] == [{
                "step_id": "produce", "attempt_id": produced["attempt_id"],
                "operation": {key: produced["request"][key] for key in ("contract_id", "contract_revision_digest", "operation_id", "function_principal_id")},
                "input_digest": digest_payload(produced["request"]["input"]),
                "outcome_digest": produced["outcome_digest"],
                "completion_status": None, "has_tool_intents": False,
            }]
            # Returned metadata is a defensive snapshot, not shared authority state.
            result["lineage"][0]["step_id"] = "modified-copy"
            assert evidence.read("input:/value")["lineage"][0]["step_id"] == "produce"
            with pytest.raises(EvidenceDenied, match="another invocation"):
                registry.receive(kind=EVIDENCE_KIND, binding=binding, receiver=object(), guard=receiver_guard)
            with pytest.raises(EvidenceDenied, match="binding"):
                registry.receive(kind=EVIDENCE_KIND, binding=replace(binding, payload_digest="sha256:" + "0" * 64), receiver=receiver, guard=receiver_guard)
            observed.append(result)
        before = dict(calls)
        with pytest.raises(EvidenceDenied, match="no longer live"):
            evidence.read("input:/value")
        assert calls == before, "closed scope must not reenter expired guards"
        registry.close()

    during_consumer(runtime, inspect)
    consumed = execute(runtime, "consume")
    assert consumed["state"] == "succeeded"
    assert len(observed) == 1


@pytest.mark.parametrize("guard_role", ["issuer", "receiver"])
@pytest.mark.parametrize("failure", ["cancelled", "timed out"])
def test_numeric_evidence_is_fenced_if_guard_expires_during_read(runtime: Runtime, guard_role: str, failure: str) -> None:
    start(runtime, 0.375)
    execute(runtime, "produce")
    checked: list[bool] = []

    def inspect(request: Any) -> None:
        binding = binding_for(runtime, request)
        registry = InvocationEvidenceRegistry()
        live = {"issuer": True, "receiver": True}

        class ExpiringEvidence(WorkflowAttemptEvidence):
            def read(self, selector: str) -> Any:
                result = super().read(selector)
                live[guard_role] = False
                return result

        def guard(role: str) -> None:
            if not live[role]:
                raise EvidenceDenied(f"fixture {role} {failure}")

        registry.register(binding.issuer_principal, EVIDENCE_KIND, lambda reference, identity: ExpiringEvidence(runtime.store, reference, identity), data_fields=("value",))
        with registry.dispatch(kind=EVIDENCE_KIND, reference=request["request_id"], binding=binding, guard=lambda: guard("issuer")):
            evidence = registry.receive(kind=EVIDENCE_KIND, binding=binding, receiver=object(), guard=lambda: guard("receiver"))
            with pytest.raises(EvidenceDenied, match=failure):
                evidence.read("input:/value")
            checked.append(True)
        registry.close()

    during_consumer(runtime, inspect)
    assert execute(runtime, "consume")["state"] == "succeeded"
    assert checked == [True]


def test_registry_close_revokes_live_fractional_evidence(runtime: Runtime) -> None:
    start(runtime, 0.375)
    execute(runtime, "produce")
    checked: list[bool] = []

    def inspect(request: Any) -> None:
        binding = binding_for(runtime, request)
        registry = InvocationEvidenceRegistry()
        registry.register(binding.issuer_principal, EVIDENCE_KIND, lambda reference, identity: WorkflowAttemptEvidence(runtime.store, reference, identity), data_fields=("value",))
        with registry.dispatch(kind=EVIDENCE_KIND, reference=request["request_id"], binding=binding, guard=lambda: None):
            evidence = registry.receive(kind=EVIDENCE_KIND, binding=binding, receiver=object(), guard=lambda: None)
            assert_float_bits(evidence.read("input:/value")["value"], 0.375)
            registry.close()
            with pytest.raises(EvidenceDenied, match="no longer live"):
                evidence.read("input:/value")
            checked.append(True)

    during_consumer(runtime, inspect)
    assert execute(runtime, "consume")["state"] == "succeeded"
    assert checked == [True]


@pytest.mark.parametrize("kind,data_fields,authority_float,accepted", [
    (EVIDENCE_KIND, (), False, False),
    ("fixture.other.evidence.v1", (), False, False),
    (EVIDENCE_KIND, ("value",), True, False),
    ("fixture.other.evidence.v1", ("value",), False, True),
])
def test_evidence_float_policy_requires_explicit_captured_field_declaration(runtime: Runtime, kind: str, data_fields: tuple[str, ...], authority_float: bool, accepted: bool) -> None:
    start(runtime, 0.375)
    execute(runtime, "produce")
    checked: list[bool] = []

    def inspect(request: Any) -> None:
        binding = binding_for(runtime, request)
        registry = InvocationEvidenceRegistry()

        class ExtraEvidence(WorkflowAttemptEvidence):
            def read(self, selector: str) -> Any:
                result = dict(super().read(selector))
                if authority_float:
                    result["security_epoch"] = 7.5
                return result

        registry.register(binding.issuer_principal, kind, lambda reference, identity: ExtraEvidence(runtime.store, reference, identity), data_fields=data_fields)
        with registry.dispatch(kind=kind, reference=request["request_id"], binding=binding, guard=lambda: None):
            evidence = registry.receive(kind=kind, binding=binding, receiver=object(), guard=lambda: None)
            if accepted:
                assert_float_bits(evidence.read("input:/value")["value"], 0.375)
            else:
                with pytest.raises(ValueError, match="floating point"):
                    evidence.read("input:/value")
            checked.append(True)
        registry.close()

    during_consumer(runtime, inspect)
    assert execute(runtime, "consume")["state"] == "succeeded"
    assert checked == [True]
