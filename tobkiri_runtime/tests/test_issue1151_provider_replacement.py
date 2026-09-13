"""Issue #1151 acceptance coverage for provider replacement."""

from __future__ import annotations

import time
from typing import Any, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    CATALOG_CONTRACT,
    CATALOG_GENERATE_OPERATION,
    FAILOVER_CONTRACT,
    FAILOVER_GENERATE_OPERATION,
    GENERATE_PROVIDER_CONTRACT,
    GENERATE_PROVIDER_OPERATION,
    HEALTH_CONTRACT,
    HEALTH_GENERATE_OPERATION,
    REQUEST_PREPARE_CONTRACT,
    REQUEST_PREPARE_GENERATE_OPERATION,
    ROUTING_CONTRACT,
    ROUTING_GENERATE_OPERATION,
    STREAM_NORMALIZE_CONTRACT,
    STREAM_NORMALIZE_OPERATION,
    STREAM_PROVIDER_CONTRACT,
    STREAM_PROVIDER_OPERATION,
    TOOL_BRIDGE_CONTRACT,
    TOOL_BRIDGE_GENERATE_OPERATION,
    USAGE_CONTRACT,
    USAGE_GENERATE_OPERATION,
    create_generate_operation,
    create_routing_diagnostics_operation,
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
from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    create_tool_intent_operation,
)
from ecosystem.rumi_ai_usage_pack.runtime.usage import create_cost_operation


class _ReplacementClient:
    """Deterministic provider-neutral dispatch fixture with a hot swap."""

    def __init__(self) -> None:
        self.active_provider = "provider-a"
        self.replacement_count = 0
        self.provider_calls: list[str] = []

    def replace_provider(self) -> None:
        """Replace the registered execution handle without changing the route."""
        self.active_provider = "provider-b"
        self.replacement_count += 1

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        if contract_id in {GENERATE_PROVIDER_CONTRACT, STREAM_PROVIDER_CONTRACT}:
            return (
                {
                    "provider_instance_id": self.active_provider,
                    "routing_keys": ["stable-provider"],
                    "adapter_revision": f"adapter-{self.active_provider}",
                },
            )
        if contract_id == CATALOG_CONTRACT:
            return ({"provider_instance_id": "catalog-owner"},)
        if contract_id == HEALTH_CONTRACT:
            return ({"provider_instance_id": "health-owner"},)
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
        if contract_id == TOOL_BRIDGE_CONTRACT:
            return create_tool_intent_operation(None)(operation, payload)
        if contract_id == USAGE_CONTRACT:
            return create_cost_operation(None)(operation, payload)
        if contract_id == FAILOVER_CONTRACT:
            return create_failover_operation(None)(operation, payload)
        if contract_id == CATALOG_CONTRACT:
            return {
                "catalog_revision": "catalog-r1",
                "models": [
                    {
                        "model_id": "stable-provider/model",
                        "provider_model_id": "stable-model",
                        "provider_id": "stable-provider",
                        "execution_provider_instance_id": "provider-a",
                        "modalities": ["text"],
                        "capabilities": ["thinking", "tool_calling"],
                        "context_length": 4096,
                        "request_surfaces": ["chat"],
                        "input_cost": 0.25,
                        "output_cost": 0.5,
                        "priority": 1,
                        "available": True,
                        "catalog_revision": "catalog-r1",
                    }
                ],
            }
        if contract_id == HEALTH_CONTRACT:
            return {
                "providers": [
                    {
                        "provider_instance_id": self.active_provider,
                        "status": "healthy",
                        "observed_at": time.time(),
                    }
                ]
            }
        if contract_id in {GENERATE_PROVIDER_CONTRACT, STREAM_PROVIDER_CONTRACT}:
            if provider_instance_id != self.active_provider:
                raise GlobalContractInvocationError(
                    "provider_unavailable", "replacement handle is not active"
                )
            self.provider_calls.append(self.active_provider)
            if contract_id == GENERATE_PROVIDER_CONTRACT:
                return {
                    "status": "ok",
                    "output": f"{self.active_provider}:generated",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 2, "output_tokens": 4},
                    "usage_provenance": "replacement-fixture",
                }
            if payload.get("input") == "cancel":
                return {
                    "events": [
                        {"type": "text_delta", "delta": "partial"},
                        {"type": "error", "error_code": "cancelled"},
                    ]
                }
            return {
                "events": [
                    {"type": "text_delta", "delta": "completed"},
                    {
                        "type": "usage",
                        "usage": {"input_tokens": 2, "output_tokens": 4},
                    },
                    {"type": "finish", "finish_reason": "stop"},
                ]
            }
        raise AssertionError(f"unexpected contract: {contract_id}/{operation}")


def test_gateway_keeps_one_consumer_across_provider_replacement() -> None:
    """Generate, stream, cancellation, usage, and diagnostics survive a swap."""
    client = _ReplacementClient()
    generate = create_generate_operation(client)  # type: ignore[arg-type]
    stream = create_stream_operation(client)  # type: ignore[arg-type]

    before = generate(
        "generate",
        {
            "request_id": "issue1151-before",
            "messages": [{"role": "user", "content": "before"}],
            "credential_handle": "credential:opaque:replacement-secret",
        },
    )
    assert before["provider_instance_id"] == "provider-a"
    assert before["output"] == "provider-a:generated"

    client.replace_provider()

    after = generate(
        "generate",
        {
            "request_id": "issue1151-after",
            "messages": [{"role": "user", "content": "after"}],
            "credential_handle": "credential:opaque:replacement-secret",
        },
    )
    assert after["provider_instance_id"] == "provider-b"
    assert after["output"] == "provider-b:generated"
    assert after["usage_cost"] == {
        "input_tokens": 2.0,
        "output_tokens": 4.0,
        "total_tokens": 6.0,
        "cost": 2.5,
        "currency": "USD",
        "known": True,
        "usage_provenance": "replacement-fixture",
        "pricing_revision": "catalog-r1",
    }

    completed = stream(
        "stream",
        {
            "request_id": "issue1151-stream",
            "messages": [{"role": "user", "content": "stream"}],
        },
    )
    assert completed["provider_instance_id"] == "provider-b"
    assert [event["type"] for event in completed["events"]] == [
        "text_delta",
        "usage",
        "finish",
    ]
    assert completed["events"][1]["usage_cost"]["cost"] == 2.5

    cancelled = stream(
        "stream",
        {
            "request_id": "issue1151-cancel",
            "input": "cancel",
        },
    )
    assert cancelled["provider_instance_id"] == "provider-b"
    assert [event["type"] for event in cancelled["events"]] == [
        "text_delta",
        "error",
    ]
    assert cancelled["events"][-1]["error_code"] == "cancelled"
    assert client.replacement_count == 1
    assert client.provider_calls == [
        "provider-a",
        "provider-b",
        "provider-b",
        "provider-b",
    ]

    diagnostics = create_routing_diagnostics_operation(client)(
        "get", {"request_id": "issue1151-after"}
    )
    assert diagnostics["count"] == 1
    record = diagnostics["diagnostics"][0]
    assert record["selected"] == {
        "model_id": "stable-provider/model",
        "provider_instance_id": "provider-b",
        "catalog_revision": "catalog-r1",
    }
    assert "credential:opaque:replacement-secret" not in repr(record)
