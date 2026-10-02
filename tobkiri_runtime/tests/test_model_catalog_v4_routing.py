"""Bundled models must resolve the operation-specific v4 provider identity."""

import pytest

from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_model_catalog_pack.runtime.catalog import (
    create_model_catalog_operation,
)


@pytest.mark.parametrize("mode", ["generate", "stream"])
def test_bundled_models_resolve_exact_v4_provider(mode: str) -> None:
    """Defaults routes real catalog data without injected routing wildcards."""
    catalog = create_model_catalog_operation(None)
    models = catalog(
        f"rumi_model_catalog_pack.bundled-model-catalog.{mode}", {}
    )["models"]
    provider_id = f"provider.compatibility.{mode}"
    assert models
    assert {item["execution_provider_instance_id"] for item in models} == {
        provider_id
    }
    router = create_route_operation(None)
    payload = {
        "models": models,
        "execution_providers": [{"provider_instance_id": provider_id}],
        "requirements": {"request_surface": "defaultspack.conversation"},
        "health": {},
        "decision_time": 0,
    }
    result = router("route", payload)
    assert result["candidates"]
    assert all(
        item["reason"] != "execution_provider_unresolved"
        for item in result["excluded"]
    )
    # Correcting the hint must not allow an unrelated selected provider.
    denied = router(
        "route",
        {**payload, "execution_providers": [{"provider_instance_id": "other"}]},
    )
    assert not denied["candidates"]
    assert {item["reason"] for item in denied["excluded"]} == {
        "execution_provider_unresolved"
    }
