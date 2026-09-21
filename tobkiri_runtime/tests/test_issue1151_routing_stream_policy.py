"""Issue #1151 acceptance coverage for routing, streams, and failover."""

from __future__ import annotations

import time
from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    CATALOG_CONTRACT,
    FAILOVER_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
    HEALTH_CONTRACT,
    REQUEST_PREPARE_CONTRACT,
    ROUTING_CONTRACT,
    STREAM_NORMALIZE_CONTRACT,
    STREAM_PROVIDER_CONTRACT,
    USAGE_CONTRACT,
    create_stream_operation,
)
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
    create_prepare_operation,
)
from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_ai_stream_pack.runtime.normalizer import (
    create_stream_normalize_operation,
)
from ecosystem.rumi_ai_usage_pack.runtime.usage import create_cost_operation


def _model(model_id: str = "model-a", **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "model_id": model_id,
        "provider_model_id": model_id,
        "provider_id": "provider",
        "execution_provider_instance_id": "adapter",
        "health_provider_instance_id": "adapter",
        "modalities": ["text", "image"],
        "capabilities": ["tool_calling", "thinking"],
        "context_length": 8192,
        "request_surfaces": ["chat", "agent"],
        "data_residency": ["jp", "us"],
        "input_cost": 0.1,
        "output_cost": 0.2,
        "priority": 10,
        "available": True,
        "catalog_revision": "catalog-r1",
    }
    value.update(overrides)
    return value


def _route_payload(
    model: dict[str, Any],
    requirements: Mapping[str, Any],
    *,
    health: Mapping[str, Any] | None = None,
    decision_time: float = 1000.0,
) -> dict[str, Any]:
    return {
        "models": [model],
        "execution_providers": [
            {"provider_instance_id": "adapter", "routing_keys": ["provider"]}
        ],
        "health": dict(
            health
            if health is not None
            else {"adapter": {"status": "healthy", "observed_at": 1000.0}}
        ),
        "requirements": dict(requirements),
        "decision_time": decision_time,
    }


def test_router_covers_capability_health_residency_and_cost_policy() -> None:
    operation = create_route_operation(None)
    valid = operation(
        "route",
        _route_payload(
            _model(),
            {
                "modalities": ["text", "image"],
                "tool_calling": True,
                "thinking": True,
                "minimum_context": 4096,
                "request_surface": "agent",
                "data_residency": "jp",
                "maximum_cost": 1.0,
            },
        ),
    )
    assert valid["selected"]["model_id"] == "model-a"
    assert valid["selected"]["health"] == "healthy"

    cases = (
        ({"modalities": ["audio"]}, "modality_mismatch"),
        ({"tool_calling": True}, "tool_calling_mismatch"),
        ({"thinking": True}, "thinking_mismatch"),
        ({"minimum_context": 16384}, "context_length_mismatch"),
        ({"request_surface": "embedding"}, "request_surface_mismatch"),
        ({"data_residency": "eu"}, "data_residency_mismatch"),
        ({"maximum_cost": 0.1}, "cost_policy_mismatch"),
    )
    overrides = (
        {},
        {"capabilities": ["thinking"]},
        {"capabilities": ["tool_calling"]},
        {"context_length": 1024},
        {"request_surfaces": ["chat"]},
        {"data_residency": ["us"]},
        {},
    )
    for (requirements, reason), model_overrides in zip(cases, overrides):
        result = operation(
            "route",
            _route_payload(_model(**model_overrides), requirements),
        )
        assert result["selected"] is None
        assert result["excluded"][0]["reason"] == reason

    unavailable = operation(
        "route",
        _route_payload(_model(available=False), {}),
    )
    assert unavailable["excluded"][0]["reason"] == "model_unavailable"

    unhealthy = operation(
        "route",
        _route_payload(
            _model(),
            {},
            health={"adapter": {"status": "unavailable", "observed_at": 1000.0}},
        ),
    )
    assert unhealthy["excluded"][0]["reason"] == "health_unavailable"

    stale = operation(
        "route",
        _route_payload(
            _model(),
            {"health_max_age": 10.0},
            health={"adapter": {"status": "healthy", "observed_at": 0.0}},
        ),
    )
    assert stale["selected"]["health"] == "unknown"

    unknown = operation("route", _route_payload(_model(), {}, health={}))
    assert unknown["selected"]["health"] == "unknown"

    unknown_cost = operation(
        "route",
        _route_payload(
            _model(input_cost=None, output_cost=None),
            {"maximum_cost": 1.0},
        ),
    )
    assert unknown_cost["excluded"][0]["reason"] == "cost_unknown"

    ordered_models = [_model("b", priority=5), _model("a", priority=5)]
    ordered_payload = _route_payload(ordered_models[0], {})
    ordered_payload["models"] = ordered_models
    first = operation("route", ordered_payload)
    second = operation("route", ordered_payload)
    assert first == second
    assert first["selected"]["model_id"] == "a"


def test_stream_normalizer_covers_typed_terminal_and_malformed_events() -> None:
    operation = create_stream_normalize_operation(None)
    normalized = operation(
        "normalize",
        {
            "request_id": "stream-1",
            "provider_attempt": 2,
            "value": {
                "events": [
                    {"type": "thinking_delta", "delta": "private"},
                    {"type": "text_delta", "delta": "hello"},
                    {
                        "type": "tool_intent_delta",
                        "tool_intent": {"name": "lookup"},
                    },
                    {"type": "usage", "usage": {"output_tokens": 1}},
                    {"type": "finish", "finish_reason": "stop"},
                ]
            },
        },
    )
    assert [item["type"] for item in normalized["events"]] == [
        "thinking_delta",
        "text_delta",
        "tool_intent_delta",
        "usage",
        "finish",
    ]
    assert [item["sequence"] for item in normalized["events"]] == [0, 1, 2, 3, 4]
    assert all(item["request_id"] == "stream-1" for item in normalized["events"])
    assert all(item["provider_attempt"] == 2 for item in normalized["events"])

    cancelled = operation(
        "normalize",
        {
            "request_id": "stream-cancel",
            "value": [
                {"type": "text_delta", "delta": "partial"},
                {"type": "error", "error_code": "cancelled"},
            ],
        },
    )
    assert cancelled["events"][-1]["type"] == "error"

    invalid_values = (
        {"request_id": "bad", "value": {"events": "not-events"}},
        {"request_id": "bad", "value": [{"type": "unknown"}]},
        {"request_id": "bad", "value": [{"type": "text_delta"}]},
        {
            "request_id": "bad",
            "value": [{"type": "finish"}, {"type": "text_delta"}],
        },
        {"request_id": "bad", "value": ["not-an-event", {"type": "finish"}]},
        {"request_id": "bad", "value": {"events": {"type": "finish"}}},
    )
    for payload in invalid_values:
        with pytest.raises(GlobalContractInvocationError) as captured:
            operation("normalize", payload)
        assert captured.value.code == "invalid_response"


def test_usage_cost_and_failover_are_explicit_and_replay_bound() -> None:
    cost = create_cost_operation(None)(
        "calculate",
        {
            "usage": {"input_tokens": 2, "output_tokens": 4},
            "pricing": {"input": 0.25, "output": 0.5, "currency": "USD"},
            "pricing_revision": "catalog-r1",
            "usage_provenance": "provider-reported",
        },
    )
    assert cost["known"] is True
    assert cost["cost"] == 2.5
    assert cost["pricing_revision"] == "catalog-r1"
    unknown = create_cost_operation(None)(
        "calculate", {"usage": {}, "pricing": {}}
    )
    assert unknown["known"] is False
    assert unknown["cost"] is None

    failover = create_failover_operation(None)
    allowed = failover(
        "decide",
        {
            "allow_failover": True,
            "idempotency_key": "replay-1",
            "error_code": "provider_unavailable",
            "attempt": 1,
            "candidate_count": 2,
            "deadline": 1100.0,
            "decision_time": 1000.0,
        },
    )
    assert allowed["allowed"] is True
    assert allowed["reason"] == "replay_safe_failover"

    denied = (
        ({"allow_failover": False}, "explicitly_allowed"),
        ({"allow_failover": True, "idempotency_key": ""}, "idempotency_bound"),
        (
            {"allow_failover": True, "idempotency_key": "x", "tools": [{}]},
            "tool_free",
        ),
        (
            {
                "allow_failover": True,
                "idempotency_key": "x",
                "error_code": "denied",
            },
            "retryable_error",
        ),
        (
            {
                "allow_failover": True,
                "idempotency_key": "x",
                "error_code": "quota",
                "attempt": 2,
                "candidate_count": 2,
            },
            "candidate_available",
        ),
    )
    for extra, reason in denied:
        payload = {
            "error_code": "provider_unavailable",
            "attempt": 1,
            "candidate_count": 2,
            "deadline": 1100.0,
            "decision_time": 1000.0,
            "idempotency_key": "replay-1",
            "allow_failover": True,
        }
        payload.update(extra)
        result = failover("decide", payload)
        assert result["allowed"] is False
        assert result["reason"] == reason


class _BufferedMalformedStreamClient:
    """Gateway fixture whose first stream buffers a malformed event."""

    def __init__(self) -> None:
        self.provider_calls: list[str] = []
        self.failover_calls = 0

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        if contract_id in {STREAM_PROVIDER_CONTRACT, GENERATE_PROVIDER_CONTRACT}:
            return (
                {"provider_instance_id": "provider-a", "routing_keys": ["a"]},
                {"provider_instance_id": "provider-b", "routing_keys": ["b"]},
            )
        if contract_id == CATALOG_CONTRACT:
            return ({"provider_instance_id": "catalog"},)
        if contract_id == HEALTH_CONTRACT:
            return ({"provider_instance_id": "health"},)
        return ()

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        provider_instance_id: str | None = None,
    ) -> dict[str, Any]:
        if contract_id == REQUEST_PREPARE_CONTRACT:
            return create_prepare_operation(None)(operation, payload)
        if contract_id == ROUTING_CONTRACT:
            return create_route_operation(None)(operation, payload)
        if contract_id == STREAM_NORMALIZE_CONTRACT:
            return create_stream_normalize_operation(None)(operation, payload)
        if contract_id == USAGE_CONTRACT:
            return create_cost_operation(None)(operation, payload)
        if contract_id == FAILOVER_CONTRACT:
            self.failover_calls += 1
            return create_failover_operation(None)(operation, payload)
        if contract_id == CATALOG_CONTRACT:
            return {
                "models": [
                    {
                        "model_id": "a/model",
                        "provider_id": "a",
                        "provider_model_id": "model-a",
                        "execution_provider_instance_id": "provider-a",
                        "modalities": ["text"],
                        "capabilities": [],
                        "context_length": 1000,
                        "input_cost": 0.1,
                        "output_cost": 0.1,
                        "catalog_revision": "catalog-a",
                    },
                    {
                        "model_id": "b/model",
                        "provider_id": "b",
                        "provider_model_id": "model-b",
                        "execution_provider_instance_id": "provider-b",
                        "modalities": ["text"],
                        "capabilities": [],
                        "context_length": 1000,
                        "input_cost": 0.1,
                        "output_cost": 0.1,
                        "catalog_revision": "catalog-b",
                    },
                ]
            }
        if contract_id == HEALTH_CONTRACT:
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider-a",
                        "status": "healthy",
                        "observed_at": time.time(),
                    },
                    {
                        "provider_instance_id": "provider-b",
                        "status": "healthy",
                        "observed_at": time.time(),
                    },
                ]
            }
        if contract_id == STREAM_PROVIDER_CONTRACT:
            assert provider_instance_id is not None
            self.provider_calls.append(provider_instance_id)
            if provider_instance_id == "provider-a":
                return {
                    "events": [
                        {"type": "text_delta", "delta": "buffered"},
                        {"type": "not-a-valid-event"},
                    ]
                }
            return {
                "events": [
                    {"type": "text_delta", "delta": "fallback"},
                    {"type": "finish", "finish_reason": "stop"},
                ]
            }
        raise AssertionError(f"unexpected contract: {contract_id}/{operation}")


def test_stream_fails_over_before_buffered_events_are_returned() -> None:
    client = _BufferedMalformedStreamClient()
    stream = create_stream_operation(client)  # type: ignore[arg-type]

    result = stream(
        "stream",
        {
            "request_id": "buffered-malformed",
            "allow_failover": True,
            "idempotency_key": "replay-safe",
            "deadline": time.time() + 60.0,
            "requirements": {"modalities": ["text"]},
        },
    )

    assert client.provider_calls == ["provider-a", "provider-b"]
    assert client.failover_calls == 1
    assert result["provider_instance_id"] == "provider-b"
    assert result["events"] == [
        {
            "request_id": "buffered-malformed",
            "sequence": 0,
            "type": "text_delta",
            "delta": "fallback",
            "tool_intent": None,
            "usage": None,
            "finish_reason": None,
            "error_code": None,
            "provider_attempt": 2,
        },
        {
            "request_id": "buffered-malformed",
            "sequence": 1,
            "type": "finish",
            "delta": None,
            "tool_intent": None,
            "usage": None,
            "finish_reason": "stop",
            "error_code": None,
            "provider_attempt": 2,
        },
    ]
    assert result["attempts"] == [
        {
            "attempt": 1,
            "model_id": "a/model",
            "provider_instance_id": "provider-a",
            "error_code": "invalid_response",
        }
    ]
