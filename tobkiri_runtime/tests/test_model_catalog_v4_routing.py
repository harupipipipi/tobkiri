"""Bundled models must resolve the operation-specific v4 provider identity."""

import pytest

from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_model_catalog_pack.runtime import catalog as catalog_module
from ecosystem.rumi_model_catalog_pack.runtime.catalog import (
    create_model_catalog_operation,
)


@pytest.mark.parametrize("mode", ["generate", "stream"])
def test_bundled_models_resolve_exact_v4_provider(
    mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults routes real catalog data without injected routing wildcards."""
    # This checks captured execution identities, not public inventory discovery.
    # Make discovery explicitly unavailable and reject any accidental network
    # fallback; neither live catalog changes nor a local cache may drive it.
    monkeypatch.setattr(
        catalog_module, "_openrouter_inventory",
        lambda: ([], "unavailable", False),
    )
    monkeypatch.setattr(
        catalog_module.urllib.request, "urlopen",
        lambda *args, **kwargs: pytest.fail("routing test must not use network"),
    )
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


def test_exact_approved_lookup_never_refreshes_public_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saved readiness reads immutable owner pricing without network or LKG."""

    monkeypatch.setattr(
        catalog_module,
        "_openrouter_inventory",
        lambda: pytest.fail("exact bundled lookup must not refresh inventory"),
    )
    result = create_model_catalog_operation(None)(
        "rumi_model_catalog_pack.bundled-model-catalog.generate",
        {
            "provider_id": "openrouter",
            "model_id": "deepseek/deepseek-r1-0528",
            "catalog_source": "bundled_approved",
        },
    )

    assert result["inventory"] == {
        "openrouter": {
            "source": "bundled_approved",
            "model_count": 1,
        }
    }
    assert len(result["models"]) == 1
    assert result["models"][0]["catalog_source"] == "bundled_approved"
    assert result["models"][0]["provider_model_id"] == (
        "deepseek/deepseek-r1-0528"
    )
