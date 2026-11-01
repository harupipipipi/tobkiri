from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
MODEL_CATALOG_ROOT = ROOT / "ecosystem" / "rumi_model_catalog_pack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _catalog_and_models(provider_id: str):
    from domain.ai_client.providers import get_all_known_models, get_provider_catalog_map

    catalog = get_provider_catalog_map()
    models = {item["id"]: item for item in get_all_known_models(provider_id)}
    return catalog[provider_id], models


def _bundled_model_catalog_paths():
    yield from (MODEL_CATALOG_ROOT / "catalog" / "providers").glob(
        "*/models.json"
    )
    yield from (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
        / "llm"
        / "providers"
    ).glob("*/models/*.json")


def _bundled_provider_manifest_paths():
    yield from (MODEL_CATALOG_ROOT / "catalog" / "providers").glob(
        "*/manifest.json"
    )
    yield from (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
        / "llm"
        / "providers"
    ).glob("*/manifest.json")


def test_bundled_provider_model_json_uses_strict_canonical_schema():
    from domain.ai_client.metadata_json import load_strict_metadata_json
    from domain.ai_client.model_metadata_schema import validate_model_catalog_source

    for path in _bundled_model_catalog_paths():
        payload = load_strict_metadata_json(path)
        if isinstance(payload, dict) and "models" in payload:
            validate_model_catalog_source(payload, path=path)
        else:
            validate_model_catalog_source({"models": [payload]}, path=path)


def test_openrouter_static_catalogs_are_non_authoritative_legacy_artifacts():
    from domain.ai_client.metadata_json import load_strict_metadata_json

    def keys_from_model_payload(payload):
        models = payload.get("models") if isinstance(payload, dict) and "models" in payload else [payload]
        return {
            (str(model.get("provider_id") or ""), str(model.get("model_id") or ""))
            for model in models
            if isinstance(model, dict)
        }

    legacy_payload = load_strict_metadata_json(
        MODEL_CATALOG_ROOT / "catalog" / "providers" / "openrouter" / "models.json"
    )
    legacy_keys = keys_from_model_payload(legacy_payload)
    catalog_dir = (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
        / "llm"
        / "providers"
        / "openrouter"
        / "models"
    )
    catalog_keys = set()
    for path in sorted(catalog_dir.glob("*.json")):
        catalog_keys.update(keys_from_model_payload(load_strict_metadata_json(path)))

    assert legacy_keys
    assert catalog_keys
    assert ("openrouter", "tencent/hy3:free") in legacy_keys
    assert ("openrouter", "tencent/hy3-preview:free") in catalog_keys


def test_model_catalog_validation_rejects_duplicate_ids_and_context_drift():
    from domain.ai_client.model_metadata_schema import (
        ModelMetadataSchemaError,
        validate_model_catalog_source,
    )

    with pytest.raises(ModelMetadataSchemaError, match="duplicate model id"):
        validate_model_catalog_source(
            {
                "models": [
                    {"id": "p/m", "provider_id": "p", "model_id": "m", "capabilities": {"text_input": True}},
                    {"id": "p/m", "provider_id": "p", "model_id": "m", "capabilities": {"text_input": True}},
                ]
            },
            path="models.json",
        )

    with pytest.raises(ModelMetadataSchemaError, match="context aliases disagree"):
        validate_model_catalog_source(
            {
                "models": [
                    {
                        "id": "p/m",
                        "provider_id": "p",
                        "model_id": "m",
                        "capabilities": {"text_input": True},
                        "context_window": 100,
                        "max_context": 99,
                    }
                ]
            },
            path="models.json",
        )


def test_provider_source_manifests_do_not_duplicate_model_capability_truth():
    from domain.ai_client.metadata_json import load_strict_metadata_json

    model_specific_provider_keys = {
        "supports_vision",
        "supports_audio",
        "supports_pdf",
        "supports_file_upload",
        "supports_reasoning",
    }
    for path in (
        DEFAULTSPACK_ROOT / "domain" / "ai_client" / "capabilities" / "manifests"
    ).glob("*.json"):
        payload = load_strict_metadata_json(path)
        assert "api_surface" in payload
        assert not model_specific_provider_keys.intersection(payload)

    for path in _bundled_provider_manifest_paths():
        payload = load_strict_metadata_json(path)
        assert "capabilities" not in payload
        assert "api_surface" not in payload
        provider_metadata = payload.get("provider_metadata")
        if isinstance(provider_metadata, dict):
            assert "capabilities" not in provider_metadata
            assert "api_surface" not in provider_metadata
        provider_manifest = payload.get("provider_manifest")
        if isinstance(provider_manifest, dict):
            assert "capabilities" not in provider_manifest
            assert "api_surface" not in provider_manifest


def test_provider_api_surface_contract_matches_bundled_model_requirements():
    from domain.ai_client.metadata_json import load_strict_metadata_json
    from domain.ai_client.model_metadata_schema import normalize_capability_map

    capability_dir = DEFAULTSPACK_ROOT / "domain" / "ai_client" / "capabilities" / "manifests"
    provider_surfaces = {
        path.stem: load_strict_metadata_json(path).get("api_surface", {})
        for path in capability_dir.glob("*.json")
    }

    for path in _bundled_model_catalog_paths():
        payload = load_strict_metadata_json(path)
        models = payload.get("models") if isinstance(payload, dict) and "models" in payload else [payload]
        for model in models:
            if not isinstance(model, dict):
                continue
            provider_id = str(model.get("provider_id") or model.get("id", "").split("/", 1)[0]).strip()
            surface = provider_surfaces.get(provider_id)
            if not isinstance(surface, dict):
                continue
            capabilities = normalize_capability_map(model.get("capabilities"))
            model_label = f"{path}: {model.get('id')}"
            if capabilities.get("image_input"):
                blocks = set(surface.get("accepts_content_blocks") or [])
                assert blocks.intersection({"image", "image_url"}), model_label
            if capabilities.get("parallel_tool_calls"):
                assert surface.get("supports_tool_call_shape") is True, model_label
                assert surface.get("supports_parallel_tool_call_shape") is True, model_label


def test_groq_manifest_first_runtime_provider_and_allowlist(configured_cloud_provider):
    from domain.ai_client.providers import detect_available_providers

    provider, models = _catalog_and_models("groq")

    assert provider["availability"]["supports_invoke"] is True
    assert provider["metadata"]["adapter"] == "openai_compatible"
    assert provider["metadata"]["default_base_url"] == "https://api.groq.com/openai/v1"
    assert provider["default_model_for"]["chat"] == "openai/gpt-oss-120b"
    assert provider["default_model_for"]["fast"] == "openai/gpt-oss-20b"
    assert models == {}

    configured_cloud_provider("groq", "test-groq-key")
    assert "groq" in detect_available_providers()


def test_cerebras_manifest_first_runtime_provider(
    monkeypatch,
    configured_cloud_provider,
):
    from domain.ai_client.providers import detect_available_providers

    provider, models = _catalog_and_models("cerebras")

    assert provider["availability"]["supports_invoke"] is True
    assert provider["metadata"]["adapter"] == "openai_compatible"
    assert provider["metadata"]["default_base_url"] == "https://api.cerebras.ai/v1"
    assert provider["default_model_for"] == {}
    assert models == {}
    assert provider["metadata"]["config"]["model_sync"] == "remote_merge"

    configured_cloud_provider("cerebras", "test-cerebras-key")
    assert "cerebras" in detect_available_providers()


def test_openai_compatible_provider_merges_remote_models_into_curated_catalog(tmp_path):
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="groq",
        api_key="test-groq-key",
        default_base_url="https://api.groq.com/openai/v1",
        credential_required=False,
        remote_model_discovery=True,
        known_models=[
            {
                "id": "groq/openai/gpt-oss-120b",
                "model_id": "openai/gpt-oss-120b",
                "display_name": "GPT OSS 120B",
                "capabilities": {"reasoning": True, "tool_calls": True},
            },
        ],
    )

    cache_path = tmp_path / "groq.models.json"

    with patch.object(provider, "_remote_model_cache_path", return_value=cache_path), patch.object(
        provider,
        "_fetch_remote_models",
        return_value=[
            provider._normalize_remote_model({"id": "groq/compound-beta"}) or {},
            provider._normalize_remote_model({"id": "meta-llama/llama-4-scout-17b-16e-instruct"}) or {},
        ],
    ):
        models = {item["id"]: item for item in provider.list_models()}

    assert "groq/openai/gpt-oss-120b" in models
    assert "groq/compound-beta" in models
    assert "groq/meta-llama/llama-4-scout-17b-16e-instruct" in models
    assert models["groq/compound-beta"]["capabilities"]["tool_calls"] is False
    scout = models["groq/meta-llama/llama-4-scout-17b-16e-instruct"]
    assert scout["capabilities"]["vision"] is False
    assert scout["capabilities"]["tool_calls"] is False
    assert scout["metadata"]["capability_source"] == "remote_models_endpoint"
    assert scout["metadata"]["capability_confidence"] == "unknown"
    assert cache_path.exists()


def test_openai_compatible_remote_model_cache_uses_defaultspack_shared_user_data():
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="groq",
        api_key="test-groq-key",
        default_base_url="https://api.groq.com/openai/v1",
        credential_required=False,
        remote_model_discovery=True,
    )

    cache_path = provider._remote_model_cache_path()
    assert cache_path.parent == DEFAULTSPACK_ROOT / "user_data" / "shared" / "provider_model_cache"
    assert cache_path.name.startswith("groq.")
    assert cache_path.name.endswith(".models.json")


def test_cerebras_openai_compatible_params_match_model_contract():
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    captured = {}
    provider = OpenAICompatibleProvider(
        provider_id="cerebras",
        api_key="test-cerebras-key",
        default_base_url="https://api.cerebras.ai/v1",
        credential_required=False,
        known_models=[
            {
                "id": "cerebras/gpt-oss-120b",
                "model_id": "gpt-oss-120b",
                "display_name": "GPT OSS 120B",
                "capabilities": {"reasoning": True},
                "metadata": {
                    "request_example": {
                        "max_completion_tokens": 32768,
                        "temperature": 1,
                        "top_p": 1,
                        "reasoning_effort": "high",
                    }
                },
            },
            {
                "id": "cerebras/llama3.1-8b",
                "model_id": "llama3.1-8b",
                "display_name": "Llama 3.1 8B",
                "capabilities": {"reasoning": False},
                "metadata": {
                    "request_defaults": {
                        "max_completion_tokens": 2048,
                        "temperature": 0.2,
                        "top_p": 1,
                    }
                },
            },
        ],
    )

    def fake_request_json(path, body):
        captured.setdefault("bodies", []).append(body)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        provider.complete(
            "gpt-oss-120b",
            [{"role": "user", "content": "hi"}],
            [],
            {"thinking_level": "high", "max_tokens": 123},
        )
        provider.complete(
            "llama3.1-8b",
            [{"role": "user", "content": "hi"}],
            [],
            {"thinking_level": "medium", "max_tokens": 99},
        )

    gpt_body, llama_body = captured["bodies"]
    assert gpt_body["max_completion_tokens"] == 123
    assert gpt_body["temperature"] == 1
    assert gpt_body["top_p"] == 1
    assert gpt_body["reasoning_effort"] == "high"
    assert "max_tokens" not in gpt_body

    assert llama_body["max_completion_tokens"] == 99
    assert llama_body["temperature"] == 0.2
    assert llama_body["top_p"] == 1
    assert "reasoning_effort" not in llama_body
    assert "max_tokens" not in llama_body


def test_cerebras_explicit_none_thinking_does_not_restore_default_reasoning_effort():
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    captured = {}
    provider = OpenAICompatibleProvider(
        provider_id="cerebras",
        api_key="test-cerebras-key",
        default_base_url="https://api.cerebras.ai/v1",
        credential_required=False,
        known_models=[
            {
                "id": "cerebras/gpt-oss-120b",
                "model_id": "gpt-oss-120b",
                "display_name": "GPT OSS 120B",
                "capabilities": {"reasoning": True},
            },
        ],
    )

    def fake_request_json(path, body):
        captured["body"] = body
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        provider.complete(
            "gpt-oss-120b",
            [{"role": "user", "content": "hi"}],
            [],
            {"thinking_level": "none"},
        )

    body = captured["body"]
    assert body["temperature"] == 1
    assert body["top_p"] == 1
    assert "reasoning_effort" not in body


def test_cerebras_thinking_normalization_only_emits_supported_reasoning_params():
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

    service = ModelRuntimeSettingsService()

    gpt = service.normalize_for_provider("cerebras", "gpt-oss-120b", "high")
    gpt_none = service.normalize_for_provider("cerebras", "gpt-oss-120b", "none")
    llama = service.normalize_for_provider("cerebras", "llama3.1-8b", "high")

    assert gpt["provider_params"] == {"reasoning_effort": "high"}
    assert gpt_none["provider_params"] == {}
    assert llama["provider_params"] == {}


def test_nvidia_manifest_first_runtime_provider_accepts_either_key(
    configured_cloud_provider,
):
    from domain.ai_client.api_key_store import set_provider_api_key
    from domain.ai_client.providers import detect_available_providers

    provider, models = _catalog_and_models("nvidia")

    assert provider["availability"]["supports_invoke"] is True
    assert provider["metadata"]["adapter"] == "openai_compatible"
    assert provider["metadata"]["default_base_url"] == "https://integrate.api.nvidia.com/v1"
    assert provider["env_vars"] == ["NVIDIA_API_KEY", "NGC_API_KEY"]
    assert provider["default_model_for"] == {}
    assert models == {}

    result = set_provider_api_key(
        "nvidia",
        "test-ngc-key",
        api_id="NGC_API_KEY",
        name="NGC_API_KEY",
    )
    assert result["success"] is True
    assert "nvidia" in detect_available_providers()


def test_cloud_provider_keys_are_persistable_in_secret_store():
    from domain.ai_client.api_key_store import provider_secret_keys

    assert provider_secret_keys("groq") == ["GROQ_API_KEY"]
    assert provider_secret_keys("gitlawb-opengateway") == ["GITLAWB_OPENGATEWAY_API_KEY"]
    assert provider_secret_keys("opencode-go") == ["OPENCODE_GO_API_KEY", "OPENCODE_ZEN_API_KEY"]
    assert provider_secret_keys("cerebras") == ["CEREBRAS_API_KEY"]
    assert provider_secret_keys("nvidia") == ["NVIDIA_API_KEY", "NGC_API_KEY"]
    assert provider_secret_keys("xiaomi-token-plan-sgp") == [
        "XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_API_KEY",
        "MIMO_API_KEY",
    ]


def test_named_token_plan_key_maps_back_to_long_provider_id(tmp_path, monkeypatch):
    from domain.ai_client.api_key_store import (
        load_provider_api_keys_into_env,
        provider_has_api_key,
        provider_named_api_keys,
        set_provider_api_key,
    )

    for env_name in (
        "XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_API_KEY",
        "MIMO_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)

    set_provider_api_key(
        "xiaomi-token-plan-sgp",
        "test-token",
        name="MiMo Token Plan SGP",
        default_model="mimo-v2.5-pro",
        pack_root=tmp_path,
    )

    assert provider_has_api_key("xiaomi-token-plan-sgp", pack_root=tmp_path) is True
    assert provider_named_api_keys("xiaomi-token-plan-sgp", pack_root=tmp_path)[0]["provider_id"] == "xiaomi-token-plan-sgp"

    loaded = load_provider_api_keys_into_env(pack_root=tmp_path)

    assert loaded["xiaomi-token-plan-sgp"] is True
    assert all(
        env_name not in os.environ
        for env_name in (
            "XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY",
            "XIAOMI_MIMO_TOKEN_PLAN_API_KEY",
            "MIMO_API_KEY",
        )
    )


def test_cloud_model_capability_false_values_are_preserved():
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog

    profiles = {item["profile_id"]: item for item in list_profile_catalog()}

    assert not any(profile_id.startswith(("cerebras/", "nvidia/")) for profile_id in profiles)


def test_openai_primary_chat_models_remain_tool_capable_in_public_catalog():
    from domain.ai_client.providers import get_best_model_for_provider
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog

    models = {item["id"]: item for item in list_model_catalog("openai")}
    assert get_best_model_for_provider("openai", "chat") is None
    assert get_best_model_for_provider("openai", "fast") is None
    assert models == {}


def test_moonshot_manifest_first_runtime_provider(configured_cloud_provider):
    from domain.ai_client.providers import detect_available_providers

    provider, models = _catalog_and_models("moonshotai")

    assert provider["availability"]["supports_invoke"] is True
    assert provider["metadata"]["adapter"] == "openai_compatible"
    assert provider["metadata"]["default_base_url"] == "https://api.moonshot.ai/v1"
    assert provider["default_model_for"] == {}
    assert models == {}

    configured_cloud_provider("moonshotai", "test-moonshot-key")
    assert "moonshotai" in detect_available_providers()


def test_xiaomi_mimo_direct_catalog_is_separate_and_not_runtime_enabled(
    configured_cloud_provider,
):
    from domain.ai_client.providers import detect_available_providers, get_provider_catalog_map
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog, list_provider_catalog

    catalog = get_provider_catalog_map()

    assert catalog["gitlawb-opengateway"]["provider_id"] == "gitlawb-opengateway"
    assert catalog["xiaomi-mimo"]["availability"]["supports_invoke"] is False
    assert catalog["xiaomi-mimo-global"]["availability"]["supports_invoke"] is True
    assert catalog["xiaomi-mimo-cn"]["availability"]["supports_invoke"] is False
    assert catalog["xiaomi-token-plan-sgp"]["availability"]["supports_invoke"] is True
    assert catalog["xiaomi-token-plan-sgp"]["metadata"]["adapter"] == "python_entrypoint"
    assert catalog["xiaomi-token-plan-sgp"]["metadata"]["default_base_url"] == "https://token-plan-sgp.xiaomimimo.com/v1"
    assert catalog["xiaomi-mimo-global"]["metadata"]["config"]["do_not_fallback_to_other_region"] is True
    assert catalog["xiaomi-mimo-cn"]["metadata"]["config"]["do_not_reuse_credentials_across_regions"] is True
    global_plan = catalog["xiaomi-mimo-global"]["subscription_plans"][0]
    cn_plan = catalog["xiaomi-mimo-cn"]["metadata"]["subscription_plans"][0]
    assert global_plan["id"] == "mimo_orbit_100t_grant_if_available"
    assert global_plan["token_quota_label"] == "100T tokens"
    assert global_plan["region"] == "global"
    assert global_plan["requires_manual_signup"] is True
    assert global_plan["do_not_auto_enable"] is True
    assert cn_plan["region"] == "cn"
    assert cn_plan["region_scoped"] is True

    # The public provider API is selected-pack scoped.  This test reads the
    # catalog owner directly, so it must not require that Pack to be active in
    # the defaultspack-only unit-test profile.
    assert isinstance(list_provider_catalog(), list)

    global_models = {model["id"]: model for model in list_model_catalog("xiaomi-mimo-global")}
    assert global_models == {}

    configured_cloud_provider("xiaomi-mimo-global", "test-global")
    assert "xiaomi-mimo-global" in detect_available_providers()

    configured_cloud_provider("xiaomi-token-plan-sgp", "test-token-plan")
    assert "xiaomi-token-plan-sgp" in detect_available_providers()


def test_xiaomi_token_plan_uses_live_inventory_without_static_models():
    provider, models = _catalog_and_models("xiaomi-token-plan-sgp")

    assert provider["availability"]["supports_invoke"] is True
    assert provider["metadata"]["config"]["auth_header"] == "api-key"
    assert provider["default_model_for"]["coding"] == "mimo-v2.5-pro"
    assert models == {}
