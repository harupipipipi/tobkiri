from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("provider_model_catalog_selected")


def test_provider_capability_registry_loads_default_manifests():
    from domain.ai_client.capabilities.registry import default_registry

    registry = default_registry()
    assert {"openai", "google", "cerebras", "openrouter"}.issubset(set(registry.provider_ids()))


def test_provider_capability_registry_merges_model_metadata():
    from domain.ai_client.capabilities.registry import default_registry

    caps = default_registry().for_model(
        "openai/custom",
        {"provider_id": "openai", "capabilities": ["vision"], "max_context_tokens": 1234},
    )

    assert caps.supports_vision is True
    assert caps.max_context_tokens == 1234


def test_provider_api_surface_does_not_leak_aggregator_model_capabilities():
    from domain.ai_client.capabilities.registry import default_registry

    text_only = default_registry().for_model(
        "openrouter/example-text",
        {
            "provider_id": "openrouter",
            "capabilities": {
                "text_input": True,
                "text_output": True,
                "image_input": False,
                "tool_calling": False,
            },
        },
    )
    vision = default_registry().for_model(
        "openrouter/example-vision",
        {
            "provider_id": "openrouter",
            "capabilities": {
                "text_input": True,
                "text_output": True,
                "image_input": True,
                "tool_calling": True,
            },
        },
    )

    assert text_only.api_surface["accepts_content_blocks"]
    assert text_only.supports_vision is False
    assert "image_url" not in text_only.supported_content_blocks
    assert text_only.supports_tool_calling is False
    assert vision.supports_vision is True
    assert "image_url" in vision.supported_content_blocks
    assert vision.supports_tool_calling is True


def test_groq_vision_model_keeps_image_content_block():
    from domain.ai_client.capabilities.registry import default_registry
    from domain.ai_client.providers import get_all_known_models

    model = next(
        item
        for item in get_all_known_models("groq")
        if item["id"] == "groq/meta-llama/llama-4-scout-17b-16e-instruct"
    )
    caps = default_registry().for_model(model["id"], model)

    assert caps.supports_vision is True
    assert "image_url" in caps.api_surface["accepts_content_blocks"]
    assert "image_url" in caps.supported_content_blocks


def test_parallel_tool_capability_does_not_rewrite_provider_shape():
    from domain.ai_client.capabilities.schema import ProviderCapabilities, merge_capabilities

    base = ProviderCapabilities.from_dict(
        {
            "provider_id": "example",
            "api_surface": {
                "api_family": "openai_compatible",
                "accepts_content_blocks": ["text", "tool_call", "tool_result"],
                "supports_tool_call_shape": True,
                "supports_parallel_tool_call_shape": False,
            },
        }
    )
    caps = merge_capabilities(
        base,
        {
            "capabilities": {
                "text_input": True,
                "text_output": True,
                "tool_calling": True,
                "parallel_tool_calls": True,
            }
        },
    )

    assert caps.supports_parallel_tool_calls is True
    assert caps.api_surface["supports_parallel_tool_call_shape"] is False


def test_cerebras_parallel_tool_model_capability_is_preserved():
    from domain.ai_client.capabilities.registry import default_registry
    from domain.ai_client.providers import get_all_known_models

    model = next(item for item in get_all_known_models("cerebras") if item["id"] == "cerebras/zai-glm-4.7")
    caps = default_registry().for_model(model["id"], model)

    assert caps.supports_tool_calling is True
    assert caps.supports_parallel_tool_calls is True
    assert caps.api_surface["supports_parallel_tool_call_shape"] is True


def test_provider_capability_manifest_duplicate_json_keys_fail(tmp_path):
    from domain.ai_client.capabilities.registry import ProviderCapabilityRegistry
    from domain.ai_client.metadata_json import MetadataJsonError

    (tmp_path / "broken.json").write_text(
        '{"provider_id":"broken","api_surface":{},"api_surface":{}}',
        encoding="utf-8",
    )

    with pytest.raises(MetadataJsonError, match="duplicate JSON key: api_surface"):
        ProviderCapabilityRegistry(tmp_path)


def test_provider_capability_registry_cerebras_quirks_and_google_native():
    from domain.ai_client.capabilities.registry import default_registry

    cerebras = default_registry().for_model("cerebras/gpt-oss-120b")
    google = default_registry().for_model("google/gemma-4-31b-it")

    assert cerebras.quirks["max_tokens_name"] == "max_completion_tokens"
    assert google.api_family == "google_native"


def test_ai_client_runtime_model_includes_provider_capabilities():
    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.stub_provider import StubProvider

    AIClient._instance = None
    client = AIClient()
    try:
        client.register_provider("custom", StubProvider())
        client._providers["custom"].KNOWN_MODELS = [{"id": "custom/model", "model_id": "model", "capabilities": ["vision"]}]
        models = client.list_models(provider="custom")
    finally:
        AIClient._instance = None

    model = next(item for item in models if item["id"] == "custom/model")
    assert "provider_capabilities" in model
    assert model["provider_capabilities"]["supports_vision"] is True
