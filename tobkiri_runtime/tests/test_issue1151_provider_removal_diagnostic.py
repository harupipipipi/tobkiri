"""Issue #1151 acceptance coverage for provider-removal diagnostics."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    CATALOG_CONTRACT,
    FAILOVER_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
    HEALTH_CONTRACT,
    MODEL_PROFILE_CONTRACT,
    REQUEST_PREPARE_CONTRACT,
    ROUTING_CONTRACT,
    create_generate_operation,
    create_routing_diagnostics_operation,
)
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
    create_prepare_operation,
)
from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry


class _RemovedProviderClient:
    """Restarted gateway fixture retaining only a non-executable tombstone."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        provider_registry: ProviderRegistry,
    ) -> None:
        self.model_registry = model_registry
        self.provider_registry = provider_registry
        self.execution_calls: list[dict[str, Any]] = []

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            return (
                {
                    "provider_instance_id": "provider-tombstone",
                    "routing_keys": [],
                    "enabled": False,
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
        if contract_id == FAILOVER_CONTRACT:
            return create_failover_operation(None)(operation, payload)
        if contract_id == MODEL_PROFILE_CONTRACT:
            value = self.model_registry.resolve(str(payload.get("identifier") or ""))
            if value is None:
                raise GlobalContractInvocationError(
                    "unknown", "model profile is unknown"
                )
            return value
        if contract_id == CATALOG_CONTRACT:
            return {
                "models": [
                    {
                        "model_id": "provider-a/model",
                        "provider_model_id": "model",
                        "provider_id": "provider-a",
                        "execution_provider_instance_id": "provider-a",
                        "modalities": ["text"],
                        "capabilities": [],
                        "context_length": 4096,
                        "request_surfaces": ["chat"],
                        "catalog_revision": "catalog-r1",
                        "available": True,
                    }
                ]
            }
        if contract_id == HEALTH_CONTRACT:
            return {"providers": []}
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            self.execution_calls.append(
                {
                    "provider_instance_id": provider_instance_id,
                    "payload": dict(payload),
                }
            )
            raise AssertionError("removed provider must not execute")
        raise AssertionError(f"unexpected contract: {contract_id}/{operation}")


def test_removed_provider_returns_unresolved_profile_remediation_data(
    tmp_path,
) -> None:
    model_registry = ModelRegistry(
        "default", user_data_root=tmp_path / "model-registry"
    )
    model_registry.save(
        {
            "model_profile_id": "saved-profile",
            "model_id": "provider-a/model",
            "requirements": {
                "preferred_provider_instance_id": "provider-a"
            },
        },
        expected_revision=0,
    )
    model_registry.set_alias("default", "saved-profile", expected_revision=1)

    provider_registry = ProviderRegistry(
        "default", user_data_root=tmp_path / "provider-registry"
    )
    saved = provider_registry.save(
        {
            "provider_instance_id": "provider-a",
            "adapter_id": "openai-compatible",
            "endpoint": "http://127.0.0.1:11434/v1",
        },
        expected_revision=0,
    )
    provider_registry.delete(
        "provider-a", expected_revision=saved["store_revision"]
    )
    restarted_provider_registry = ProviderRegistry(
        "default", user_data_root=tmp_path / "provider-registry"
    )
    assert restarted_provider_registry.snapshot()["providers"] == []

    client = _RemovedProviderClient(model_registry, restarted_provider_registry)
    generate = create_generate_operation(client)  # type: ignore[arg-type]
    with pytest.raises(GlobalContractInvocationError) as captured:
        generate(
            "generate",
            {
                "request_id": "provider-removal-request",
                "model_profile_id": "default",
                "messages": [{"role": "user", "content": "no fallback"}],
            },
        )
    assert captured.value.code == "unresolved_profile"
    assert "saved model reference" in str(captured.value)
    assert client.execution_calls == []

    diagnostics = create_routing_diagnostics_operation(client)(
        "get", {"request_id": "provider-removal-request"}
    )
    assert diagnostics["count"] == 1
    record = diagnostics["diagnostics"][0]
    assert record["selected"] is None
    assert record["excluded"] == [
        {
            "model_id": "provider-a/model",
            "provider_id": "provider-a",
            "reason": "execution_provider_unresolved",
        }
    ]
    assert record["requirements"]["modalities"] == ["text"]
