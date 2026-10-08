"""Native discovery uses official GET queries and never poisons full catalogs."""
import io
import json
from unittest.mock import patch

import pytest
from ecosystem.rumi_model_catalog_pack.runtime import catalog


class Response(io.BytesIO):
    status = 200
    headers = {}


def test_filtered_discovery_is_native_query_and_does_not_touch_full_cache():
    captured = []
    model = {"id": "vendor/new", "name": "New", "architecture": {
        "input_modalities": ["text"], "output_modalities": ["text"]},
        "pricing": {"prompt": "0", "completion": "0"}}

    def get(request, **kwargs):
        captured.append(request.full_url)
        return Response(json.dumps({"data": [model]}).encode())

    with patch.object(catalog, "_load_catalog", return_value=([{"provider_id": "openrouter"}], [])), \
         patch.object(catalog.urllib.request, "urlopen", side_effect=get), \
         patch.object(catalog, "_save_openrouter_inventory_cache") as save, \
         patch.object(catalog, "_merge_openrouter_inventory") as merge:
        result = catalog.create_model_catalog_operation(None)("list", {
            "provider_id": "openrouter", "discovery_filters": {
                "output_modalities": ["text"], "category": "programming"},
        })
    assert captured == ["https://openrouter.ai/api/v1/models?output_modalities=text&category=programming"]
    assert result["models"][0]["provider_model_id"] == "vendor/new"
    assert result["inventory"]["openrouter"]["source"] == "openrouter_models_api_filtered"
    save.assert_not_called()
    merge.assert_not_called()


def test_native_discovery_empty_success_is_distinct_from_offline_failure():
    with patch.object(catalog.urllib.request, "urlopen", return_value=Response(b'{"data":[]}')):
        assert catalog._fetch_openrouter_inventory(strict=True) == []
    with patch.object(catalog.urllib.request, "urlopen", side_effect=OSError("offline")):
        with pytest.raises(ValueError, match="unavailable"):
            catalog._fetch_openrouter_inventory(strict=True)
    with patch.object(catalog.urllib.request, "urlopen", return_value=Response(b'{"changed_schema":[]}')):
        with pytest.raises(ValueError, match="schema"):
            catalog._fetch_openrouter_inventory(strict=True)


@pytest.mark.parametrize("payload", [
    {"provider_id": "openai", "discovery_filters": {"category": "programming"}},
    {"provider_id": "openrouter", "discovery_filters": {"zdr": True}},
    {"provider_id": "openrouter", "model_id": "a/b", "catalog_source": "bundled_approved", "discovery_filters": {}},
])
def test_unverified_provider_filters_cannot_generate_network_request(payload):
    with patch.object(catalog, "_load_catalog", return_value=([], [])), \
         patch.object(catalog.urllib.request, "urlopen") as get:
        with pytest.raises(ValueError):
            catalog.create_model_catalog_operation(None)("list", payload)
        get.assert_not_called()
