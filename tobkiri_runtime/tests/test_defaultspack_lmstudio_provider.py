from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.ai_client.providers.lmstudio_provider import (  # noqa: E402
    LMStudioAPIError,
    LMStudioProvider,
)


NATIVE_MODELS = [
    {
        "type": "llm",
        "publisher": "google",
        "key": "google/gemma-4-26b-a4b",
        "display_name": "Gemma 4 26B A4B",
        "architecture": "gemma4",
        "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
        "size_bytes": 17_990_911_801,
        "params_string": "26B-A4B",
        "loaded_instances": [
            {
                "id": "google/gemma-4-26b-a4b",
                "config": {"context_length": 4096, "parallel": 4},
            }
        ],
        "max_context_length": 262144,
        "format": "gguf",
        "capabilities": {
            "vision": True,
            "trained_for_tool_use": True,
            "reasoning": {
                "allowed_options": ["off", "on"],
                "default": "on",
            },
        },
        "variants": ["google/gemma-4-26b-a4b@q4_k_m"],
        "selected_variant": "google/gemma-4-26b-a4b@q4_k_m",
    },
    {
        "type": "embedding",
        "publisher": "gaianet",
        "key": "text-embedding-nomic-embed-text-v1.5-embedding",
        "display_name": "Nomic Embed Text v1.5",
        "quantization": {"name": "F16", "bits_per_weight": 16},
        "size_bytes": 274_290_560,
        "loaded_instances": [],
        "max_context_length": 2048,
        "format": "gguf",
    },
]


def _provider(**kwargs):
    return LMStudioProvider(
        base_url="http://127.0.0.1:1234/v1",
        management_base_url="http://127.0.0.1:1234",
        **kwargs,
    )


def test_lmstudio_native_models_preserve_ids_types_capabilities_and_load_state():
    provider = _provider()
    models = provider._normalize_native_models([*NATIVE_MODELS, dict(NATIVE_MODELS[0])])

    assert [model["id"] for model in models] == [
        "lmstudio/google/gemma-4-26b-a4b",
        "lmstudio/text-embedding-nomic-embed-text-v1.5-embedding",
    ]

    gemma = models[0]
    assert gemma["model_id"] == "google/gemma-4-26b-a4b"
    assert gemma["type"] == "chat"
    assert gemma["context_window"] == 262144
    assert gemma["capabilities"]["image_input"] is True
    assert gemma["capabilities"]["tool_calling"] is True
    assert gemma["capabilities"]["thinking"] is True
    assert gemma["thinking"]["levels"] == ["off", "on"]
    assert gemma["thinking"]["provider_mapping"] == {"off": "off", "on": "on"}
    assert gemma["metadata"]["loaded"] is True
    assert gemma["metadata"]["quantization"]["name"] == "Q4_K_M"
    assert gemma["metadata"]["selected_variant"].endswith("@q4_k_m")

    embedding = models[1]
    assert embedding["type"] == "embedding"
    assert embedding["capabilities"]["text_input"] is True
    assert embedding["capabilities"]["text_output"] is False
    assert embedding["capabilities"]["streaming"] is False
    assert embedding["metadata"]["loaded"] is False


def test_lmstudio_management_url_and_optional_auth(monkeypatch):
    monkeypatch.delenv("LMSTUDIO_SERVER_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_MANAGEMENT_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_API_TOKEN", raising=False)
    monkeypatch.delenv("LM_API_TOKEN", raising=False)

    provider = LMStudioProvider(base_url="http://localhost:1234/custom/v1")
    assert provider._management_base_url() == "http://localhost:1234/custom"
    assert "Authorization" not in provider._headers(content_type="")

    authenticated = LMStudioProvider(
        api_key="test-token",
        base_url="http://localhost:1234/v1",
        management_base_url="http://localhost:1234",
    )
    assert authenticated._headers(content_type="")["Authorization"] == "Bearer test-token"


def test_lmstudio_cache_keeps_last_known_inventory_on_refresh_failure(tmp_path):
    provider = _provider(cache_ttl_seconds=60)
    cache_path = tmp_path / "lmstudio.models.json"
    native = provider._normalize_native_models(NATIVE_MODELS)

    with patch.object(provider, "_remote_model_cache_path", return_value=cache_path), patch.object(
        provider,
        "_fetch_native_models",
        return_value=native,
    ):
        refreshed = provider.refresh_models()

    assert len(refreshed) == 2
    assert cache_path.exists()

    with patch.object(provider, "_remote_model_cache_path", return_value=cache_path), patch.object(
        provider,
        "_fetch_native_models",
        side_effect=LMStudioAPIError("offline", kind="network_error"),
    ):
        stale = provider.refresh_models()

    assert len(stale) == 2
    assert all(model["metadata"]["catalog_cache_state"] == "stale" for model in stale)


def test_lmstudio_explicit_load_and_unload_use_native_endpoints_only():
    provider = _provider()
    calls = []

    def fake_request(path, *, body=None, timeout=None):
        calls.append((path, body, timeout))
        return {"ok": True}

    with patch.object(provider, "_native_request_json", side_effect=fake_request):
        provider.load_model(
            "lmstudio/google/gemma-4-26b-a4b",
            context_length=16384,
            flash_attention=True,
            ignored_setting="not-forwarded",
        )
        provider.unload_model("lmstudio/google/gemma-4-26b-a4b")

    assert calls == [
        (
            "/api/v1/models/load",
            {
                "model": "google/gemma-4-26b-a4b",
                "context_length": 16384,
                "flash_attention": True,
            },
            None,
        ),
        (
            "/api/v1/models/unload",
            {"instance_id": "google/gemma-4-26b-a4b"},
            None,
        ),
    ]


def test_lmstudio_component_owns_runtime_discovery_and_has_no_placeholder():
    # The bundled model-catalog pack is the executable provider trust root;
    # the retired defaultspack/domain/providers path must not be recreated.
    component_manifest_path = (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "catalog"
        / "providers"
        / "lmstudio"
        / "manifest.json"
    )
    payload = json.loads(component_manifest_path.read_text(encoding="utf-8"))
    provider_manifest = payload["provider_manifest"]

    assert provider_manifest["adapter"] == "python_entrypoint"
    assert provider_manifest["entrypoint"].endswith("lmstudio_provider:LMStudioProvider")
    assert provider_manifest["credential_required"] is False
    assert provider_manifest["default_base_url"] == "local://lmstudio"
    assert provider_manifest["config"]["inference_base_url_default"] == "http://127.0.0.1:1234/v1"
    assert provider_manifest["config"]["model_list_path"] == "/api/v1/models"
    assert "default_model" not in provider_manifest

    catalog_provider_dir = (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
        / "llm"
        / "providers"
        / "lmstudio"
    )
    assert not (catalog_provider_dir / "manifest.json").exists()
    assert not (catalog_provider_dir / "models" / "local-model.json").exists()


def test_lmstudio_component_overrides_legacy_default_and_registers_runtime():
    from domain.ai_client.providers import detect_available_providers, get_provider_catalog_map
    from domain.components.registry import get_domain_component_registry

    get_domain_component_registry(force_reload=True)
    catalog = get_provider_catalog_map()
    entry = catalog["lmstudio"]

    assert entry["kind"] == "local"
    assert entry["default_model"] == ""
    assert entry["availability"]["configuration_source"] == "builtin_local_provider"
    assert entry["availability"]["supports_invoke"] is True
    assert isinstance(detect_available_providers()["lmstudio"], LMStudioProvider)
