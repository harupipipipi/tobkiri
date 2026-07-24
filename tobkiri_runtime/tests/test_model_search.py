from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _profile(profile_id, **extra):
    provider_id, model_id = profile_id.split("/", 1)
    return {
        "profile_id": profile_id,
        "qualified_model_id": profile_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "display_name": model_id,
        "type": "chat",
        "configured": extra.pop("configured", True),
        **extra,
    }


def test_search_models_filters_by_capabilities():
    from domain.ai_client.model_search import search_models

    profiles = [
        _profile("openai/gpt-5", supports_vision=True, supports_tool_calling=True, supports_thinking=True, supports_fast=False, speed_tier="balanced", knowledge_level=92),
        _profile("local/fast", supports_vision=False, supports_tool_calling=False, supports_thinking=False, supports_fast=True, speed_tier="fast", knowledge_level=30),
    ]

    result = search_models(
        {
            "requires": {"vision": True, "tool_calling": True, "thinking": True},
            "min_knowledge_level": 85,
            "max_results": 5,
        },
        profiles=profiles,
    )

    assert [item["profile_id"] for item in result["models"]] == ["openai/gpt-5"]
    assert result["filters_applied"]["requires"]["vision"] is True


def test_search_models_matches_multi_word_queries_across_model_separators():
    from domain.ai_client.model_search import search_models

    profiles = [
        _profile(
            "gitlawb-opengateway/mimo-v2-omni",
            display_name="MiMo V2 Omni via Gitlawb OpenGateway",
            provider_display_name="Gitlawb OpenGateway",
            supports_vision=True,
            capability_tags=["vision"],
            recommended_roles=["primary_chat", "vision_ocr"],
        ),
    ]

    result = search_models({"query": "mimo omni", "max_results": 5}, profiles=profiles)

    assert [item["profile_id"] for item in result["models"]] == ["gitlawb-opengateway/mimo-v2-omni"]


def test_search_models_maps_legacy_hy3_free_terms_to_current_live_profiles():
    from domain.ai_client.model_search import search_models

    profiles = [
        _profile(
            "openrouter/tencent/hy3",
            display_name="Tencent: Hy3",
            provider_display_name="OpenRouter",
        ),
        _profile(
            "openrouter/tencent/hy3-preview",
            display_name="Tencent: Hy3 preview",
            provider_display_name="OpenRouter",
        ),
    ]

    result = search_models({"query": "hy3 free", "max_results": 5}, profiles=profiles)

    assert [item["profile_id"] for item in result["models"]] == [
        "openrouter/tencent/hy3",
        "openrouter/tencent/hy3-preview",
    ]
    assert all(not item["profile_id"].endswith(":free") for item in result["models"])


def test_recommend_model_reports_reason_codes():
    from domain.ai_client.model_search import recommend_model

    result = recommend_model(
        {"requires": {"vision": True}, "max_results": 2},
        profiles=[_profile("google/gemini", supports_vision=True, supports_tool_calling=True, supports_thinking=True, supports_fast=True, speed_tier="fast", knowledge_level=85)],
    )

    assert result["selected_model"]["profile_id"] == "google/gemini"
    assert "requires_vision" in result["reason_codes"]


def test_profile_catalog_unions_live_openrouter_chat_and_reasoning_models(monkeypatch):
    from ecosystem.defaultspack.backend.ai_client import provider_catalog
    from domain.ai_client import model_search
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

    saved_profile = _profile("openrouter/acme/saved", configured=True)
    live_models = [
        {
            "model_id": "openrouter/acme/atlas-reasoner",
            "provider_model_id": "acme/atlas-reasoner",
            "provider_id": "openrouter",
            "display_name": "Atlas Reasoner",
            "type": "reasoning",
            "capabilities": ["chat", "text_input", "text_output", "thinking"],
            "context_length": 262144,
            "available": True,
            "metadata": {"inventory_source": "openrouter_models_api"},
        },
        {
            "model_id": "openrouter/acme/vector",
            "provider_model_id": "acme/vector",
            "provider_id": "openrouter",
            "display_name": "Atlas Vector",
            "type": "embedding",
            "capabilities": ["text_input"],
            "available": True,
        },
    ]
    monkeypatch.setattr(provider_catalog, "list_profile_catalog", lambda: [saved_profile])
    monkeypatch.setattr(
        provider_catalog,
        "list_model_catalog",
        lambda provider="": live_models if provider == "openrouter" else [],
    )
    monkeypatch.setattr(ModelRuntimeSettingsService, "get_settings", lambda self: {})
    monkeypatch.setattr(
        ModelRuntimeSettingsService,
        "runtime_defined_profiles",
        lambda self, settings: [],
    )

    profiles = model_search._profile_catalog()
    result = model_search.search_models(
        {
            "provider_id": "openrouter",
            "requires": {"thinking": True},
            "max_results": 10,
        },
        profiles=profiles,
    )

    assert [item["profile_id"] for item in result["models"]] == [
        "openrouter/acme/atlas-reasoner"
    ]
    assert result["models"][0]["model_id"] == "acme/atlas-reasoner"
    assert {profile["profile_id"] for profile in profiles} == {
        "openrouter/acme/saved",
        "openrouter/acme/atlas-reasoner",
    }
    assert "openrouter/acme/vector" not in {
        profile["profile_id"] for profile in profiles
    }
