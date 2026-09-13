"""Issue #1151 acceptance coverage for independent catalog and adapters."""

from __future__ import annotations

from typing import Any

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation


class _RevisionedAdapter:
    """Small deterministic execution stub with an independent revision."""

    def __init__(self) -> None:
        self.provider_instance_id = "adapter-instance"
        self.revision = "adapter-r1"
        self.compatible_catalog_revision = "catalog-r1"
        self.executions: list[dict[str, str]] = []

    def execute(self, selected: dict[str, Any]) -> dict[str, str]:
        catalog_revision = str(selected["catalog_revision"])
        if catalog_revision != self.compatible_catalog_revision:
            raise GlobalContractInvocationError(
                "incompatible",
                "adapter revision is incompatible with catalog revision",
            )
        evidence = {
            "catalog_revision": catalog_revision,
            "adapter_revision": self.revision,
            "provider_instance_id": self.provider_instance_id,
        }
        self.executions.append(evidence)
        return evidence


def _model(catalog_revision: str) -> dict[str, Any]:
    return {
        "model_id": "stable-provider/model",
        "provider_model_id": "stable-model",
        "provider_id": "stable-provider",
        "execution_provider_instance_id": "adapter-instance",
        "catalog_provider_instance_id": "catalog-owner",
        "catalog_revision": catalog_revision,
        "modalities": ["text"],
        "capabilities": ["tool_calling", "thinking"],
        "context_length": 8192,
        "request_surfaces": ["chat"],
        "input_cost": 0.1,
        "output_cost": 0.2,
        "priority": 3,
        "available": True,
    }


def _route(
    model: dict[str, Any], adapter: _RevisionedAdapter
) -> dict[str, Any]:
    return create_route_operation(None)(
        "route",
        {
            "models": [model],
            "execution_providers": [
                {
                    "provider_instance_id": adapter.provider_instance_id,
                    "routing_keys": ["stable-provider"],
                    "adapter_revision": adapter.revision,
                }
            ],
            "health": {
                adapter.provider_instance_id: {
                    "status": "healthy",
                    "observed_at": 1000.0,
                }
            },
            "requirements": {"modalities": ["text"]},
            "decision_time": 1000.0,
        },
    )


def test_catalog_and_adapter_revision_swaps_remain_independent() -> None:
    adapter = _RevisionedAdapter()
    model = _model("catalog-r1")

    initial = _route(model, adapter)
    assert initial["selected"]["catalog_revision"] == "catalog-r1"
    assert initial["selected"]["provider_id"] == "stable-provider"
    assert adapter.execute(initial["selected"])["adapter_revision"] == (
        "adapter-r1"
    )

    model["catalog_revision"] = "catalog-r2"
    adapter.compatible_catalog_revision = "catalog-r2"
    catalog_swap = _route(model, adapter)
    assert catalog_swap["selected"]["catalog_revision"] == "catalog-r2"
    catalog_evidence = adapter.execute(catalog_swap["selected"])
    assert catalog_evidence == {
        "catalog_revision": "catalog-r2",
        "adapter_revision": "adapter-r1",
        "provider_instance_id": "adapter-instance",
    }

    adapter.revision = "adapter-r2"
    adapter_swap = _route(model, adapter)
    assert adapter_swap == _route(model, adapter)
    adapter_evidence = adapter.execute(adapter_swap["selected"])
    assert adapter_evidence == {
        "catalog_revision": "catalog-r2",
        "adapter_revision": "adapter-r2",
        "provider_instance_id": "adapter-instance",
    }

    adapter.compatible_catalog_revision = "catalog-incompatible"
    mismatch = _route(model, adapter)
    assert mismatch["selected"]["catalog_revision"] == "catalog-r2"
    with pytest.raises(GlobalContractInvocationError) as captured:
        adapter.execute(mismatch["selected"])
    assert captured.value.code == "incompatible"
    assert len(adapter.executions) == 3
