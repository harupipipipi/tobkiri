from __future__ import annotations

import time
from typing import Any

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    CATALOG_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
    HEALTH_CONTRACT,
    MODEL_PROFILE_CONTRACT,
    FAILOVER_CONTRACT,
    ROUTING_CONTRACT,
    REQUEST_PREPARE_CONTRACT,
    STREAM_NORMALIZE_CONTRACT,
    STREAM_PROVIDER_CONTRACT,
    TOOL_BRIDGE_CONTRACT,
    USAGE_CONTRACT,
    RouteRequirement,
    _append_explicit_live_model,
    create_generate_operation,
    create_stream_operation,
)
from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_ai_stream_pack.runtime.normalizer import (
    create_stream_normalize_operation,
)
from ecosystem.rumi_ai_usage_pack.runtime.usage import create_cost_operation
from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    create_tool_intent_operation,
)
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
    create_prepare_operation,
)


class FakeContractClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict[str, Any]]] = []
        self.fail_first = False

    def providers(self, contract_id: str):
        values = {
            CATALOG_CONTRACT: (
                {"provider_instance_id": "catalog-main"},
            ),
            GENERATE_PROVIDER_CONTRACT: (
                {"provider_instance_id": "adapter-a", "routing_keys": ["*"]},
                {"provider_instance_id": "adapter-b"},
            ),
            STREAM_PROVIDER_CONTRACT: (
                {"provider_instance_id": "adapter-a", "routing_keys": ["*"]},
            ),
            HEALTH_CONTRACT: (
                {"provider_instance_id": "health-main"},
            ),
        }
        return values.get(contract_id, ())

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: dict[str, Any],
        *,
        provider_instance_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (contract_id, operation, provider_instance_id, dict(payload))
        )
        if contract_id == CATALOG_CONTRACT:
            return {"models": _models()}
        if contract_id == HEALTH_CONTRACT:
            return {
                "providers": [
                    {
                        "provider_instance_id": "adapter-a",
                        "status": "healthy",
                        "observed_at": time.time(),
                    },
                    {
                        "provider_instance_id": "adapter-b",
                        "status": "healthy",
                        "observed_at": time.time(),
                    },
                ]
            }
        if contract_id == ROUTING_CONTRACT:
            return create_route_operation(None)(operation, payload)
        if contract_id == USAGE_CONTRACT:
            return create_cost_operation(None)(operation, payload)
        if contract_id == STREAM_NORMALIZE_CONTRACT:
            return create_stream_normalize_operation(None)(operation, payload)
        if contract_id == TOOL_BRIDGE_CONTRACT:
            return create_tool_intent_operation(None)(operation, payload)
        if contract_id == REQUEST_PREPARE_CONTRACT:
            return create_prepare_operation(None)(operation, payload)
        if contract_id == FAILOVER_CONTRACT:
            return create_failover_operation(None)(operation, payload)
        if contract_id == MODEL_PROFILE_CONTRACT:
            if payload.get("identifier") != "saved-default":
                raise GlobalContractInvocationError("unknown", "unknown profile")
            return {
                "resolved_profile_id": "saved-default",
                "profile": {
                    "model_id": "model-a",
                    "requirements": {"thinking": True},
                    "parameters": {"temperature": 0.1},
                },
            }
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            if self.fail_first and provider_instance_id == "adapter-a":
                raise GlobalContractInvocationError(
                    "provider_unavailable",
                    "fixture unavailable",
                )
            return {
                "status": "ok",
                "output": "hello",
                "finish_reason": "stop",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        if contract_id == STREAM_PROVIDER_CONTRACT:
            return {
                "events": [
                    {"type": "thinking_delta", "delta": "private"},
                    {"type": "text_delta", "delta": "hello"},
                    {"type": "finish", "finish_reason": "stop"},
                ]
            }
        raise AssertionError(contract_id)


def _models() -> list[dict[str, Any]]:
    return [
        {
            "model_id": "model-a",
            "execution_provider_instance_id": "adapter-a",
            "catalog_revision": "catalog-r1",
            "modalities": ["text", "image"],
            "capabilities": ["tool_calling", "thinking"],
            "context_length": 128000,
            "request_surfaces": ["chat", "agent"],
            "input_cost": 1.0,
            "output_cost": 2.0,
            "priority": 10,
            "available": True,
        },
        {
            "model_id": "model-b",
            "execution_provider_instance_id": "adapter-b",
            "catalog_revision": "catalog-r2",
            "modalities": ["text"],
            "capabilities": ["tool_calling"],
            "context_length": 64000,
            "request_surfaces": ["chat"],
            "input_cost": 0.5,
            "output_cost": 1.0,
            "priority": 5,
            "available": True,
        },
    ]


def test_router_selects_by_capability_without_provider_pack_branch() -> None:
    client = FakeContractClient()
    operation = create_generate_operation(client)  # type: ignore[arg-type]

    result = operation(
        "generate",
        {
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {
                "modalities": ["text", "image"],
                "tool_calling": True,
                "thinking": True,
                "minimum_context": 100000,
                "request_surface": "chat",
            },
        },
    )

    assert result["model_id"] == "model-a"
    assert result["provider_instance_id"] == "adapter-a"
    assert result["output"] == "hello"


def test_gateway_forwards_startup_profile_to_provider_adapter() -> None:
    client = FakeContractClient()
    operation = create_generate_operation(client)  # type: ignore[arg-type]

    operation(
        "generate",
        {
            "profile_id": "defaults-profile",
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {"modalities": ["text", "image"]},
        },
    )

    provider_call = next(
        item for item in client.calls if item[0] == GENERATE_PROVIDER_CONTRACT
    )
    assert provider_call[3]["profile_id"] == "defaults-profile"


def test_gateway_resolves_immutable_model_provider_pricing_binding() -> None:
    client = FakeContractClient()

    result = create_generate_operation(client)(
        "resolve",
        {
            "requirements": {
                "modalities": ["text", "image"],
                "preferred_model_id": "model-a",
                "preferred_provider_instance_id": "adapter-a",
            }
        },
    )

    assert result == {
        "status": "ok",
        "model_id": "model-a",
        "provider_instance_id": "adapter-a",
        "catalog_provider_instance_id": "catalog-main",
        "catalog_revision": "catalog-r1",
        "pricing_revision": "catalog-r1",
        "pricing": {
            "input": 1.0,
            "output": 2.0,
            "currency": "USD",
        },
    }
    assert not any(
        contract_id == GENERATE_PROVIDER_CONTRACT
        for contract_id, _, _, _ in client.calls
    )


def test_gateway_projects_trusted_profile_pricing_to_explicit_live_model() -> None:
    catalog_models: list[dict[str, Any]] = []

    _append_explicit_live_model(
        catalog_models,
        RouteRequirement(
            modalities=frozenset({"text"}),
            capabilities=frozenset({"structured_output"}),
            tool_calling=False,
            thinking=False,
            minimum_context=0,
            request_surface="subagent",
            data_residency=None,
            maximum_cost=1.0,
            preferred_model_id="opencode-zen/mimo-v2.5-free",
            preferred_provider_id=None,
            preferred_provider_instance_id=None,
            health_max_age=60.0,
        ),
        explicit_pricing={
            "input": 0.0,
            "output": 0.0,
            "currency": "USD",
            "revision": "pricing-r1",
        },
    )

    assert catalog_models == [
        {
            "model_id": "opencode-zen/mimo-v2.5-free",
            "provider_model_id": "mimo-v2.5-free",
            "provider_id": "opencode-zen",
            "execution_provider_instance_id": (
                "provider.compatibility.generate"
            ),
            "health_provider_instance_id": "provider.opencode-zen",
            "catalog_revision": "pricing-r1",
            "input_cost": 0.0,
            "output_cost": 0.0,
            "currency": "USD",
            "capabilities": ["structured_output"],
            "modalities": ["text"],
            "context_length": 0,
            "priority": 0,
            "available": True,
            "request_surfaces": ["subagent"],
            "data_residency": "unknown",
        }
    ]


def test_failover_requires_explicit_replay_safe_request() -> None:
    client = FakeContractClient()
    client.fail_first = True
    operation = create_generate_operation(client)  # type: ignore[arg-type]

    result = operation(
        "generate",
        {
            "messages": [],
            "idempotency_key": "fixture-idempotency",
            "allow_failover": True,
            "requirements": {
                "modalities": ["text"],
                "preferred_model_id": "model-a",
            },
        },
    )

    assert result["provider_instance_id"] == "adapter-b"
    assert result["attempts"][0]["error_code"] == "provider_unavailable"


def test_failover_is_blocked_for_tool_payload() -> None:
    client = FakeContractClient()
    client.fail_first = True
    operation = create_generate_operation(client)  # type: ignore[arg-type]

    with pytest.raises(GlobalContractInvocationError) as captured:
        operation(
            "generate",
            {
                "messages": [],
                "tools": [{"name": "write"}],
                "idempotency_key": "fixture-idempotency",
                "allow_failover": True,
                "requirements": {
                    "modalities": ["text"],
                    "preferred_model_id": "model-a",
                },
            },
        )

    assert captured.value.code == "provider_unavailable"


def test_stream_event_types_remain_distinct() -> None:
    client = FakeContractClient()
    operation = create_stream_operation(client)  # type: ignore[arg-type]

    result = operation(
        "stream",
        {
            "messages": [],
            "requirements": {"modalities": ["text", "image"]},
        },
    )

    assert [item["type"] for item in result["events"]] == [
        "thinking_delta",
        "text_delta",
        "finish",
    ]
    assert [item["sequence"] for item in result["events"]] == [0, 1, 2]


def test_gateway_rejects_raw_credential_value() -> None:
    client = FakeContractClient()
    operation = create_generate_operation(client)  # type: ignore[arg-type]

    with pytest.raises(GlobalContractInvocationError) as captured:
        operation(
            "generate",
            {
                "messages": [],
                "credential_handle": "raw-secret",
                "requirements": {"modalities": ["text", "image"]},
            },
        )

    assert captured.value.code == "denied"


def test_saved_model_reference_resolves_profile_before_routing() -> None:
    client = FakeContractClient()
    result = create_generate_operation(client)(
        "generate",
        {
            "model_profile_id": "saved-default",
            "messages": [],
            "requirements": {"modalities": ["text", "image"]},
        },
    )

    assert result["model_id"] == "model-a"
    provider_call = next(
        item for item in client.calls if item[0] == GENERATE_PROVIDER_CONTRACT
    )
    assert provider_call[3]["parameters"]["temperature"] == 0.1


def test_live_provider_model_routes_before_static_catalog_refresh() -> None:
    client = FakeContractClient()

    result = create_generate_operation(client)(
        "generate",
        {
            "model_reference": "opencode-zen/deepseek-v4-flash-free",
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {"modalities": ["text"]},
        },
    )

    assert result["model_id"] == "opencode-zen/deepseek-v4-flash-free"
    provider_call = next(
        item for item in client.calls if item[0] == GENERATE_PROVIDER_CONTRACT
    )
    assert provider_call[2] == "adapter-a"
    assert provider_call[3]["provider_id"] == "opencode-zen"
    assert provider_call[3]["model_id"] == "deepseek-v4-flash-free"
