"""External-QA-oriented specifications for the Wave 5 model catalog."""

from __future__ import annotations

import json

import ecosystem.rumi_model_catalog_pack.runtime.catalog as catalog
import pytest

from ecosystem.defaultspack.backend.ai_client import provider_catalog
from ecosystem.rumi_model_catalog_pack.runtime.catalog import (
    CATALOG_REVISION,
    create_model_catalog_operation,
)

_FETCH_OPENROUTER_INVENTORY = catalog._fetch_openrouter_inventory
pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def isolate_openrouter_inventory(monkeypatch, tmp_path):
    """Keep catalog contract tests independent of the public network."""
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user-data"))
    monkeypatch.setattr(catalog, "_OPENROUTER_MEMORY_INVENTORY", None)
    monkeypatch.setattr(catalog, "_fetch_openrouter_inventory", lambda: [])


def _live_openrouter_model() -> dict[str, object]:
    return {
        "id": "acme/atlas-reasoner",
        "name": "Atlas Reasoner",
        "context_length": 262144,
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "reasoning", "response_format"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }


def test_catalog_is_provider_neutral_and_credential_free() -> None:
    operation = create_model_catalog_operation(None)
    result = operation("list", {})

    assert result["catalog_revision"] == CATALOG_REVISION
    assert result["providers"]
    assert result["models"]
    for model in result["models"]:
        assert model["execution_provider_instance_id"].startswith("provider.")
        assert "credential" not in model
        assert "adapter" not in model


def test_catalog_filter_is_finite() -> None:
    operation = create_model_catalog_operation(None)
    result = operation("list", {"provider_id": "does-not-exist"})

    assert result["providers"] == []
    assert result["models"] == []


def test_expired_openrouter_free_variants_are_not_exposed_without_live_inventory() -> None:
    operation = create_model_catalog_operation(None)
    result = operation("list", {"provider_id": "openrouter"})
    models = {item["model_id"]: item for item in result["models"]}

    assert models == {}
    inventory = result["inventory"]["openrouter"]
    assert inventory["source"] == "static"
    assert inventory["stale"] is False
    assert inventory["model_count"] == 0
    assert inventory["static_models_ignored"] > 0


def test_openrouter_live_inventory_replaces_static_catalog(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Length": "512"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit):
            assert limit == catalog._OPENROUTER_MAX_RESPONSE_BYTES + 1
            return json.dumps({"data": [_live_openrouter_model()]}).encode("utf-8")

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        catalog,
        "_fetch_openrouter_inventory",
        _FETCH_OPENROUTER_INVENTORY,
    )

    result = create_model_catalog_operation(None)(
        "list", {"provider_id": "openrouter"}
    )
    models = {item["model_id"]: item for item in result["models"]}

    assert "openrouter/acme/atlas-reasoner" in models
    assert "openrouter/tencent/hy3:free" not in models
    assert "openrouter/tencent/hy3-preview:free" not in models
    assert models["openrouter/acme/atlas-reasoner"]["type"] == "reasoning"
    inventory = result["inventory"]["openrouter"]
    assert inventory["source"] == "live"
    assert inventory["stale"] is False
    assert inventory["model_count"] == 1
    assert inventory["static_models_ignored"] > 0
    assert captured["request"].full_url == "https://openrouter.ai/api/v1/models"
    assert captured["request"].get_header("Authorization") is None
    assert 1 <= captured["timeout"] <= 5


def test_openrouter_network_failure_uses_stale_last_known_good(monkeypatch) -> None:
    live_model = catalog._normalize_openrouter_model(_live_openrouter_model())
    assert live_model is not None
    catalog._save_openrouter_inventory_cache(
        {
            "version": 1,
            "saved_at": 1,
            "expires_at": 1,
            "models": [live_model],
        }
    )
    monkeypatch.setattr(catalog, "_fetch_openrouter_inventory", lambda: [])

    result = create_model_catalog_operation(None)(
        "list", {"provider_id": "openrouter"}
    )
    models = {item["model_id"] for item in result["models"]}

    assert "openrouter/acme/atlas-reasoner" in models
    assert result["inventory"]["openrouter"]["source"] == "last_known_good"
    assert result["inventory"]["openrouter"]["stale"] is True


def test_runtime_catalog_keeps_live_openrouter_inventory_authoritative(monkeypatch) -> None:
    live_model = catalog._normalize_openrouter_model(_live_openrouter_model())
    assert live_model is not None

    class Client:
        @staticmethod
        def list_providers():
            return [{"provider_id": "openrouter"}]

        @staticmethod
        def list_models(provider=None):
            assert provider == "openrouter"
            return [live_model]

    monkeypatch.setattr(
        provider_catalog,
        "get_all_known_models",
        lambda provider_id=None, active_provider_ids=None: [
            {
                "provider_id": "openrouter",
                "qualified_model_id": "openrouter/static/model",
                "model_id": "openrouter/static/model",
            }
        ],
    )
    monkeypatch.setattr(provider_catalog, "_runtime_client", lambda: Client())

    models = provider_catalog.list_model_catalog("openrouter")

    assert [model["model_id"] for model in models] == [
        "openrouter/acme/atlas-reasoner"
    ]
