"""Actual canonical gateway/factory, with registered offline fixture transport.

This proves adapter and route enforcement, not production authority composition
or live model behavior. Every test transport is offline and receives no keys.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from core_runtime.authority.v4 import AuthorityDenied, authority_digest
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    CATALOG_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
)
from tests.test_ai_gateway_pack import FakeContractClient, _models
from tests.test_ai_gateway_host_provider_v4 import _captured_provider
from core_runtime.bootstrap.action_review_v4.canonical_reviewer_port import (
    CapturedReviewerBoundary,
    CanonicalReviewerGeneratePort,
    ReviewerGenerateLease,
    GENERATE_TARGET,
)
from core_runtime.bootstrap.action_review_v4.host_operation_reviewer import HostOperationReviewer


class RegisteredFixtureTransport(FakeContractClient):
    """Canonical dependencies and one registered fake provider, no network."""

    def __init__(self):
        super().__init__()
        self.transports = []
        self.mode = "ok"
        self.catalog_revision = "catalog-r1"
        self.on_transport = lambda: None

    def invoke(self, contract_id, operation, payload, *, provider_instance_id=None):
        if contract_id == CATALOG_CONTRACT:
            values = _models()
            values[0]["request_surfaces"] = ["approval-review"]
            values[0]["catalog_revision"] = self.catalog_revision
            return {"models": values}
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            self.transports.append(dict(payload))
            assert payload["tools"] == []
            assert payload["request_surface"] == "approval-review"
            assert payload["model_id"] == "model-a"
            assert provider_instance_id == "adapter-a"
            self.on_transport()
            if self.mode == "unavailable":
                raise RuntimeError("offline fixture unavailable")
            return {
                "status": "ok",
                "output": json.dumps({"decision": "approve", "reason": "Within scope."}),
                "tool_intents": [{"operation": "escape"}] if self.mode == "tools" else [],
                "finish_reason": "stop",
                "usage": {},
            }
        return super().invoke(
            contract_id, operation, payload, provider_instance_id=provider_instance_id
        )


def setup_port(*, evidence_validity_seconds=None, clock=time.time, proof_ttl=None):
    transport = RegisteredFixtureTransport()
    contribution, _ = _captured_provider()
    state = {"current": True, "releases": 0, "proof": True}
    cancellation = threading.Event()
    facts = {
        "profile_id": "defaults",
        "model_reference": "saved-default",
        "caller_principal_id": "host-reviewer-signed-fixture",
        "authority_capture_digest": authority_digest({"authenticated-native-fixture": True}),
        "route_binding": {
            "model_id": "model-a",
            "provider_instance_id": "adapter-a",
            "catalog_provider_instance_id": "catalog-main",
            "catalog_revision": "catalog-r1",
            "pricing_revision": "catalog-r1",
            "pricing": {"input": "1", "output": "2", "currency": "USD"},
        },
    }

    def current():
        if not state["current"]:
            raise AuthorityDenied("fixture capture stale")

    boundary = CapturedReviewerBoundary(facts, authority_digest(facts), current)

    class Dispatch:
        def invoke(self, contract, operation, payload, **kwargs):
            assert (contract, operation) == GENERATE_TARGET
            assert payload["_session_id"].startswith("session.approval-review.")
            assert kwargs["parent_cancellation_proof"] is not None
            kwargs["before_dispatch"]()
            kwargs["execution_guard"]()
            current()
            if cancellation.is_set():
                raise AuthorityDenied("fixture cancellation")
            safe_payload = {k: v for k, v in payload.items() if k != "_session_id"}
            value = contribution.invoke(
                operation, safe_payload, SimpleNamespace(contract_client=lambda **kwargs: transport)
            )
            if transport.mode == "rebound-result":
                value["provider_instance_id"] = "other-provider"
            if transport.mode == "model-rebound-result":
                value["model_id"] = "other-model"
            return value

    def bind(payload, boundary_digest, request_digest):
        current()
        session_id = "session.approval-review." + payload["request_id"]

        def proof():
            return {
                "state": "committed" if state["proof"] else "missing",
                "payload_digest": request_digest,
                "request_digest": authority_digest({"prepared-broker": request_digest}),
                "lease_id": "fixture-independent-reviewer-lease",
                "boundary_digest": boundary_digest,
                "session_id": session_id,
                **({"expires_at": clock() + proof_ttl} if proof_ttl is not None else {}),
            }

        return ReviewerGenerateLease(
            Dispatch(),
            session_id,
            facts["caller_principal_id"],
            boundary_digest,
            request_digest,
            time.monotonic() + 5,
            cancellation,
            object(),
            current,
            current,
            current,
            proof,
            lambda: state.update(releases=state["releases"] + 1),
        )

    return (
        CanonicalReviewerGeneratePort(
            boundary, bind, clock=clock, evidence_validity_seconds=evidence_validity_seconds
        ),
        transport,
        state,
        cancellation,
    )


def run_review(port):
    return HostOperationReviewer(port).review(
        "file.read",
        {"path": "/workspace/hello.txt"},
        scope={"profile_id": "defaults"},
        turn_id="fixture-turn",
        model_reference="saved-default",
        review_scope={"allowed_operations": ["file.read"], "allowed_roots": ["/workspace"]},
    )


def test_real_gateway_pinned_offline_transport_and_committed_evidence():
    port, transport, state, _ = setup_port()
    verdict = run_review(port)
    assert verdict.decision == "approve", verdict.reason
    assert verdict.model_id == "model-a"
    assert verdict.provider_instance_id == "adapter-a"
    assert len(transport.transports) == 1
    assert state["releases"] == 1
    proof = port.consume_transport_evidence(verdict.provider_request_id)
    assert proof["transport_proof"]["state"] == "committed"
    with pytest.raises(AuthorityDenied):
        port.consume_transport_evidence(verdict.provider_request_id)


@pytest.mark.parametrize(
    "failure",
    [
        "unavailable",
        "tools",
        "rebound-result",
        "model-rebound-result",
        "catalog-rebound",
        "cancel",
        "stale",
        "proof",
    ],
)
def test_real_gateway_adapter_failures_stop(failure):
    port, transport, state, cancelled = setup_port()
    if failure == "catalog-rebound":
        transport.catalog_revision = "changed"
    elif failure == "cancel":
        cancelled.set()
    elif failure == "stale":
        state["current"] = False
    elif failure == "proof":
        state["proof"] = False
    else:
        transport.mode = failure
    assert run_review(port).decision == "unavailable"
    if failure in {"catalog-rebound", "cancel", "stale"}:
        assert transport.transports == []


@pytest.mark.parametrize("change", ["cancel", "stale"])
def test_cancellation_or_capture_revoke_during_real_transport_discard_response(change):
    port, transport, state, cancelled = setup_port()

    def revoke():
        if change == "cancel":
            cancelled.set()
        else:
            state["current"] = False

    transport.on_transport = revoke
    verdict = run_review(port)
    assert verdict.decision == "unavailable"
    assert len(transport.transports) == 1
    assert state["releases"] == 1
    with pytest.raises(AuthorityDenied):
        port.consume_transport_evidence(verdict.review_id)


def test_wait_deadline_and_precommitted_evidence_validity_are_distinct():
    now = [time.time()]
    port, _, _, _ = setup_port(evidence_validity_seconds=120, clock=lambda: now[0], proof_ttl=80)
    verdict = run_review(port)
    assert verdict.decision == "approve"
    evidence = port.consume_transport_evidence(verdict.review_id)
    assert evidence["expires_at"] == now[0] + 80
    assert evidence["expires_at"] > time.time() + 15
    # A longer evidence cap cannot outlive backing native authority.
    port, _, _, _ = setup_port(evidence_validity_seconds=120, clock=lambda: now[0], proof_ttl=30)
    verdict = run_review(port)
    now[0] += 31
    with pytest.raises(AuthorityDenied, match="expired"):
        port.consume_transport_evidence(verdict.review_id)


def test_longer_evidence_policy_does_not_extend_generation_wait():
    port, _, _, _ = setup_port(evidence_validity_seconds=120, proof_ttl=300)

    def blocked(request):
        time.sleep(0.08)
        return port(request)

    reviewer = HostOperationReviewer(blocked, timeout_seconds=0.02)
    verdict = reviewer.review(
        "file.read",
        {"path": "/workspace/hello.txt"},
        scope={"profile_id": "defaults"},
        turn_id="turn",
        model_reference="saved-default",
        review_scope={"allowed_operations": ["file.read"], "allowed_roots": ["/workspace"]},
    )
    assert verdict.decision == "unavailable"
    reviewer.close()
