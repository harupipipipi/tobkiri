"""Complete offline model discovery and approved named provider bindings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecosystem.rumi_model_catalog_pack.runtime import catalog
from ecosystem.rumi_provider_registry_pack.runtime.process import (
    _provider_connection_snapshot,
)
from ecosystem.defaultspack.backend.ai_client import provider_catalog


def test_setup_catalog_contains_all_checked_in_model_choices_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundled catalog read preserves exact raw IDs without a network probe."""
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline catalog read attempted a network request")

    monkeypatch.setattr(catalog.urllib.request, "urlopen", no_network)
    setup_path = Path(catalog.__file__).parents[1] / "catalog/provider-setup.json"
    setup = json.loads(setup_path.read_text(encoding="utf-8"))
    providers, models = catalog._load_catalog()
    provider_ids = {item["provider_id"] for item in providers}
    model_ids = {
        (item["provider_id"], item["provider_model_id"]) for item in models
    }
    for provider_id, descriptor in setup["providers"].items():
        assert provider_id in provider_ids
        assert descriptor["models"], provider_id
        for model in descriptor["models"]:
            assert (provider_id, model["model_id"]) in model_ids
    assert len(setup["providers"]["openrouter"]["models"]) > 400
    assert setup["sources"]["models.dev"]["license"] == "MIT"


def test_provider_status_exposes_discovery_identity_but_no_credentials() -> None:
    """The UI can join an opaque connection with its provider model choices."""
    result = _provider_connection_snapshot({
        "revision": 3,
        "providers": [{
            "provider_instance_id": "opaque.connection-7",
            "display_name": "仕事用 API",
            "enabled": True,
            "credential_handle": "credential:private-handle",
            "metadata": {"catalog_provider_id": "openrouter"},
        }],
    })
    connection = result["providers"][0]
    assert connection["catalog_provider_id"] == "openrouter"
    assert connection["display_name"] == "仕事用 API"
    assert connection["credential_status"] == "configured"
    assert connection["health_status"] == "unverified"
    assert connection["reachability"] == "unknown"
    assert "private-handle" not in str(result)


def test_named_provider_keys_are_counted_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery joins keys by catalog identity and retains exact connection IDs."""
    connections = [
        {
            "provider_instance_id": "opaque.work",
            "display_name": "仕事用 API",
            "metadata": {"catalog_provider_id": "openrouter"},
            "credential_handle": "credential:private-work",
        },
        {
            "provider_instance_id": "opaque.personal",
            "display_name": "個人用 API",
            "catalog_provider_id": "openrouter",
            "credential_handle": "credential:private-personal",
        },
        {
            "provider_instance_id": "opaque.disabled",
            "metadata": {"catalog_provider_id": "openrouter"},
            "enabled": False,
        },
        {"provider_instance_id": "provider.openai"},
    ]

    def invoke(contract_id: str, operation: str, payload: dict) -> dict:
        if contract_id == provider_catalog._MODEL_CATALOG_CONTRACT:
            return {"providers": [
                {"provider_id": "openrouter"}, {"provider_id": "openai"}
            ]}
        return {"providers": connections}

    monkeypatch.setattr(provider_catalog, "_invoke", invoke)
    result = provider_catalog.list_provider_catalog()
    assert result[0]["configured"] is True
    assert result[0]["configured_api_count"] == 2
    assert [api["provider_instance_id"] for api in result[0]["named_apis"]] == [
        "opaque.work", "opaque.personal"
    ]
    assert result[0]["named_apis"][0]["name"] == "仕事用 API"
    assert result[1]["configured_api_count"] == 1
    assert "private-work" not in str(result)
    assert "private-personal" not in str(result)
