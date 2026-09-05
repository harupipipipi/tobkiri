from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService  # noqa: E402
from domain.ai_client.rumi_process import (  # noqa: E402
    RUMI_BASE_MODEL,
    RUMI_INTENDED_BASE_MODEL,
    RUMI_MODEL_PACK_REF,
    resolve_rumi_base_model,
)


def _profile(
    profile_id: str,
    *,
    display_name: str,
    provider_id: str,
    model_id: str,
    availability: dict | None = None,
    profile_type: str = "chat",
    defaults: dict | None = None,
    capabilities: dict | list | None = None,
    metadata: dict | None = None,
):
    profile = {
        "profile_id": profile_id,
        "qualified_model_id": profile_id,
        "provider_id": provider_id,
        "provider_display_name": provider_id.title(),
        "model_id": model_id,
        "display_name": display_name,
        "availability": availability or {},
        "type": profile_type,
    }
    if defaults is not None:
        profile["defaults"] = defaults
    if capabilities is not None:
        profile["capabilities"] = capabilities
    if metadata is not None:
        profile["metadata"] = metadata
    return profile


def test_model_runtime_settings_preferred_model_and_thinking_level(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)

    assert service.get_preferred_model() == "stub/default"
    assert service.get_preferred_model_group() == "default"
    preferred = service.set_preferred_model("stub/default")
    assert preferred["profile_id"] == "stub/default"
    group = service.set_preferred_model_group("vision")
    assert group["group_id"] == "vision"
    route = service.set_auto_route_within_group(False)
    assert route["enabled"] is False

    updated = service.set_thinking_level("high")
    assert updated["level"] == "high"
    assert service.get_thinking_level()["level"] == "high"


def test_model_runtime_settings_cache_reuses_resolution_and_invalidates_on_update(
    tmp_path, monkeypatch
):
    calls = 0
    original_read_all = ModelRuntimeSettingsService._read_all

    def counted_read_all(service):
        nonlocal calls
        calls += 1
        return original_read_all(service)

    monkeypatch.setattr(ModelRuntimeSettingsService, "_read_all", counted_read_all)
    service = ModelRuntimeSettingsService(tmp_path)

    first = service.get_settings()
    second = ModelRuntimeSettingsService(tmp_path).get_settings()

    assert first == second
    assert calls == 1

    service.update_settings({"preferred_model": "stub/fast"})
    refreshed = ModelRuntimeSettingsService(tmp_path).get_settings()

    assert refreshed["preferred_model"] == "stub/fast"
    assert calls == 2


def test_model_runtime_settings_cache_invalidates_on_credential_store_change(
    tmp_path, monkeypatch
):
    from domain.ai_client.api_key_store import set_provider_api_key

    service = ModelRuntimeSettingsService(tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert service.get_settings()["google_api_key_configured"] is False
    result = set_provider_api_key(
        "google",
        "test-only-key",
        pack_root=tmp_path,
    )
    assert result["success"] is True

    refreshed = ModelRuntimeSettingsService(tmp_path).get_settings()

    assert refreshed["google_api_key_configured"] is True


def test_model_runtime_settings_deepthink_toggle_warns(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)

    assert service.get_deepthink_enabled()["enabled"] is False
    enabled = service.set_deepthink_enabled(True)
    assert enabled["enabled"] is True
    assert "数時間" in enabled["message"]
    assert service.get_settings()["deepthink_enabled"] is True

    disabled = service.set_deepthink_enabled(False)
    assert disabled["enabled"] is False
    assert service.get_settings()["deepthink_enabled"] is False


def test_model_runtime_settings_deepthink_uses_desired_state_snapshot(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)

    enabled = service.set_deepthink_enabled(
        True,
        expected_revision=0,
        idempotency_key="deepthink-enable-1",
    )
    replay = service.set_deepthink_enabled(
        True,
        expected_revision=0,
        idempotency_key="deepthink-enable-1",
    )

    assert enabled["state_snapshot"] == {
        "state_ref": "defaultspack:models.deepthink_enabled",
        "value": True,
        "revision": 1,
        "freshness": "authoritative",
    }
    assert replay["idempotent_replay"] is True
    assert replay["revision"] == 1


def test_model_runtime_settings_deepthink_two_desired_states_settle_to_last(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)

    enabled = service.set_deepthink_enabled(True, expected_revision=0)
    disabled = service.set_deepthink_enabled(False, expected_revision=1)

    assert enabled["enabled"] is True
    assert disabled["enabled"] is False
    assert disabled["revision"] == 2
    assert service.get_deepthink_enabled()["enabled"] is False


def test_model_runtime_settings_utility_models_and_groups(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    settings = service.update_settings(
        {
            "utility_models": {"tool_selector": "google/gemini-2.5-flash"},
            "model_groups": {"custom": {"label": "Custom", "allowed_models": ["stub/default"]}},
        }
    )
    assert settings["utility_models"]["tool_selector"] == "google/gemini-2.5-flash"
    assert settings["utility_models"]["vision_ocr"] == ""
    assert settings["model_groups"]["custom"]["allowed_models"] == ["stub/default"]


def test_simple_model_slots_sync_existing_runtime_settings(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service._runtime_rumi_base_model = lambda settings=None: RUMI_BASE_MODEL

    settings = service.update_settings(
        {
            "main_model": "google/gemini-2.5-pro",
            "lightweight_model": "google/gemini-2.5-flash",
        }
    )

    assert settings["main_model"] == "google/gemini-2.5-pro"
    assert settings["preferred_model"] == "google/gemini-2.5-pro"
    assert settings["model_slots"] == {
        "main": "google/gemini-2.5-pro",
        "lightweight": "google/gemini-2.5-flash",
    }
    assert settings["utility_models"]["fast_reply"] == "google/gemini-2.5-flash"
    assert settings["utility_models"]["subagent_default"] == "google/gemini-2.5-flash"


def test_simple_model_slots_preserve_advanced_utility_roles_and_allow_override(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service._runtime_rumi_base_model = lambda settings=None: RUMI_BASE_MODEL
    service.update_settings(
        {
            "utility_models": {
                "tool_selector": "custom/tools",
                "vision_ocr": "custom/vision",
            },
            "lightweight_model": "custom/fast",
        }
    )

    advanced = service.update_settings(
        {
            "utility_models": {
                "tool_selector": "custom/tools",
                "vision_ocr": "custom/vision",
                "fast_reply": "custom/fast-v2",
                "subagent_default": "custom/subagent",
            }
        }
    )

    assert advanced["utility_models"]["tool_selector"] == "custom/tools"
    assert advanced["utility_models"]["vision_ocr"] == "custom/vision"
    assert advanced["utility_models"]["fast_reply"] == "custom/fast-v2"
    assert advanced["utility_models"]["subagent_default"] == "custom/subagent"
    assert advanced["lightweight_model"] == "custom/fast-v2"

    cleared = service.update_settings({"lightweight_model": ""})
    assert cleared["utility_models"]["fast_reply"] == ""
    assert cleared["utility_models"]["subagent_default"] == ""
    assert cleared["model_slots"]["lightweight"] == ""


def test_lightweight_model_sparse_update_preserves_unrelated_utility_roles(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service._runtime_rumi_base_model = lambda settings=None: RUMI_BASE_MODEL
    service.update_settings(
        {
            "utility_models": {
                "tool_selector": "custom/tools",
                "vision_ocr": "custom/vision",
                "prompt_compactor": "custom/compactor",
            }
        }
    )

    updated = service.update_settings({"lightweight_model": "custom/fast"})

    assert updated["utility_models"]["tool_selector"] == "custom/tools"
    assert updated["utility_models"]["vision_ocr"] == "custom/vision"
    assert updated["utility_models"]["prompt_compactor"] == "custom/compactor"
    assert updated["utility_models"]["fast_reply"] == "custom/fast"
    assert updated["utility_models"]["subagent_default"] == "custom/fast"


def test_structured_model_slots_write_translates_and_persists_canonical_settings(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service._runtime_rumi_base_model = lambda settings=None: RUMI_BASE_MODEL

    updated = service.update_settings(
        {
            "model_slots": {
                "main": "openai/main-chat",
                "lightweight": "google/fast-chat",
            }
        }
    )

    assert updated["preferred_model"] == "openai/main-chat"
    assert updated["utility_models"]["fast_reply"] == "google/fast-chat"
    assert updated["utility_models"]["subagent_default"] == "google/fast-chat"
    settings_path = tmp_path / "user_data" / "shared" / "frontend_settings.json"
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))["models"]
    assert persisted["preferred_model"] == "openai/main-chat"
    assert persisted["utility_models"]["fast_reply"] == "google/fast-chat"
    assert persisted["utility_models"]["subagent_default"] == "google/fast-chat"


def test_model_runtime_settings_includes_builtin_rumi_model_pack(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service._runtime_rumi_base_model = lambda settings=None: RUMI_BASE_MODEL
    # This assertion targets the unresolved built-in pack shape, so keep it
    # isolated from any configured provider catalog state introduced elsewhere.
    service._base_profile_catalog = lambda settings=None: []

    settings = service.get_settings()
    rumi_pack = next(pack for pack in settings["model_packs"] if pack["id"] == "rumi")
    profiles = service.runtime_defined_profiles(settings)
    rumi_profile = next(profile for profile in profiles if profile["profile_id"] == RUMI_MODEL_PACK_REF)

    assert rumi_pack["display_name"] == "Rumi"
    assert rumi_pack["mode"] == "review_chain"
    assert [member["model"] for member in rumi_pack["members"]] == [RUMI_BASE_MODEL, RUMI_BASE_MODEL]
    assert rumi_pack["metadata"]["base_model"] == RUMI_BASE_MODEL
    assert rumi_pack["safety"]["pre_action_assumption_block_required"] is True
    assert rumi_profile["provider_id"] == "modelpack"
    assert rumi_profile["metadata"]["mode"] == "review_chain"
    assert rumi_profile["configured"] is False
    assert rumi_profile["availability"]["status"] == "missing_member_model"


def test_model_runtime_settings_materializes_builtin_rumi_against_available_provider(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service._runtime_rumi_base_model = lambda settings=None: "google/gemini-2.5-flash"
    service._base_profile_catalog = lambda settings=None: [
        _profile(
            "google/gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            provider_id="google",
            model_id="gemini-2.5-flash",
            availability={"configured": True, "active": True, "status": "configured"},
            metadata={
                "capabilities": {"tool_calls": True, "thinking": True, "vision": True},
                "supports_tool_calling": True,
                "supports_thinking": True,
                "supports_vision": True,
                "capability_tags": ["tools", "thinking", "vision"],
            },
        )
    ]

    settings = service.get_settings()
    rumi_pack = next(pack for pack in settings["model_packs"] if pack["id"] == "rumi")
    profiles = service.runtime_defined_profiles(settings)
    rumi_profile = next(profile for profile in profiles if profile["profile_id"] == RUMI_MODEL_PACK_REF)

    assert [member["model"] for member in rumi_pack["members"]] == ["google/gemini-2.5-flash", "google/gemini-2.5-flash"]
    assert rumi_pack["metadata"]["base_model"] == "google/gemini-2.5-flash"
    assert rumi_profile["configured"] is True
    assert rumi_profile["availability"]["status"] == "configured"
    assert rumi_profile["supports_tool_calling"] is True
    assert rumi_profile["supports_thinking"] is True


def test_runtime_rumi_base_model_prefers_configured_default_profile(tmp_path, monkeypatch):
    service = ModelRuntimeSettingsService(tmp_path)
    service._base_profile_catalog = lambda settings=None: [
        _profile(
            "openai/default-chat",
            display_name="Default Chat",
            provider_id="openai",
            model_id="default-chat",
            availability={"configured": True, "active": True, "status": "configured"},
        ),
        _profile(
            RUMI_INTENDED_BASE_MODEL,
            display_name="MiMo",
            provider_id="xiaomi-token-plan-sgp",
            model_id="mimo-v2.5-pro",
            availability={"configured": True, "active": True, "status": "configured"},
        ),
    ]
    monkeypatch.setattr("domain.ai_client.providers.detect_available_providers", lambda: {})
    monkeypatch.setattr("domain.ai_client.providers.get_all_known_models", lambda: [])

    resolved = service._runtime_rumi_base_model({"preferred_model": "openai/default-chat"})

    assert resolved == "openai/default-chat"
    pack = service._ensure_rumi_model_packs([], settings={"preferred_model": "openai/default-chat"})[0]
    assert [member["model"] for member in pack["members"]] == [
        "openai/default-chat",
        "openai/default-chat",
    ]


def test_runtime_rumi_base_model_rejects_stub_non_chat_and_synthetic_profiles(tmp_path, monkeypatch):
    service = ModelRuntimeSettingsService(tmp_path)
    configured = {"configured": True, "active": True, "status": "configured"}
    service._base_profile_catalog = lambda settings=None: [
        _profile(
            "stub/default",
            display_name="Stub",
            provider_id="stub",
            model_id="default",
            availability=configured,
        ),
        _profile(
            "google/image-only",
            display_name="Image only",
            provider_id="google",
            model_id="image-only",
            availability=configured,
            profile_type="image",
        ),
        _profile(
            "modelpack/rumi",
            display_name="Rumi",
            provider_id="modelpack",
            model_id="rumi",
            availability=configured,
        ),
        _profile(
            "composite/review",
            display_name="Review chain",
            provider_id="composite",
            model_id="review",
            availability=configured,
        ),
        _profile(
            "synthetic/generated",
            display_name="Synthetic",
            provider_id="synthetic",
            model_id="generated",
            availability=configured,
        ),
        _profile(
            "openai/gpt-4o",
            display_name="GPT-4o",
            provider_id="openai",
            model_id="gpt-4o",
            availability=configured,
        ),
    ]
    monkeypatch.setattr("domain.ai_client.providers.detect_available_providers", lambda: {})
    monkeypatch.setattr("domain.ai_client.providers.get_all_known_models", lambda: [])

    for preferred in (
        "stub/default",
        "google/image-only",
        "modelpack/rumi",
        "composite/review",
        "synthetic/generated",
    ):
        assert service._runtime_rumi_base_model({"preferred_model": preferred}) == "openai/gpt-4o"


def test_resolve_rumi_base_model_fallback_is_deterministic():
    assert resolve_rumi_base_model([], available_providers=[]) == RUMI_INTENDED_BASE_MODEL
    assert (
        resolve_rumi_base_model(
            ["google/gemini-2.5-flash", "openai/gpt-4o"],
            available_providers={"google", "openai"},
            default_profile_base_model="missing/default",
        )
        == "openai/gpt-4o"
    )


def test_model_runtime_settings_normalizes_model_api_routes(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)

    settings = service.update_settings(
        {
            "model_api_routes": [
                "google/gemini-2.5-pro: google/main, google/backup",
                "google/gemma: google/work",
            ]
        }
    )

    assert settings["model_api_routes"] == (
        "google/gemini-2.5-pro: google/main, google/backup\n"
        "google/gemma: google/work\n"
    )


def test_api_bound_profile_reports_missing_api_key_after_delete(tmp_path, monkeypatch):
    from domain.ai_client.api_key_store import set_provider_api_key

    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    service = ModelRuntimeSettingsService(tmp_path)
    settings = service.update_settings(
        {
            "api_bound_profiles": [
                {
                    "provider_id": "longcat",
                    "api_id": "work",
                    "model_id": "LongCat-Flash-Chat",
                }
            ]
        }
    )

    set_provider_api_key(
        "longcat",
        "secret",
        api_id="work",
        name="Work",
        allowed_models=["LongCat-Flash-Chat"],
        default_model="LongCat-Flash-Chat",
        pack_root=tmp_path,
    )
    configured = service.runtime_defined_profiles(settings)[0]
    assert configured["configured"] is True
    assert configured["availability"]["status"] == "configured"

    set_provider_api_key("longcat", "", api_id="work", name="Work", pack_root=tmp_path)
    missing = service.runtime_defined_profiles(settings)[0]
    assert missing["configured"] is False
    assert missing["availability"]["active"] is False
    assert missing["availability"]["status"] == "missing_api_key"


def test_effective_thinking_level_resolution_order(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)
    service.set_thinking_level("low", scope="global")
    service.set_thinking_level("medium", scope="profile", profile_id="stub/default")
    service.set_thinking_level("xhigh", scope="conversation", conversation_id="conv-1")

    assert service.get_effective_thinking_level("stub/default", "conv-1")["level"] == "xhigh"
    assert service.get_effective_thinking_level("stub/default", "conv-2")["level"] == "medium"
    assert service.get_effective_thinking_level("other", "conv-2")["level"] == "low"


def test_thinking_level_validation_and_provider_normalization(tmp_path):
    service = ModelRuntimeSettingsService(tmp_path)

    assert service.validate_thinking_level("bogus")["valid"] is False
    normalized = service.normalize_for_provider("openai", "gpt-5", "xhigh")

    assert normalized["provider_params"]["reasoning_effort"] == "high"
    assert normalized["level"] == "high"

    cerebras = service.normalize_for_provider("cerebras", "gpt-oss-120b", "xhigh")
    assert cerebras["provider_params"] == {"reasoning_effort": "high"}
    assert cerebras["level"] == "xhigh"

    nvidia_model = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    nvidia = service.normalize_for_provider("nvidia", nvidia_model, "high")
    assert nvidia["provider_params"] == {"reasoning_effort": "high"}

    nvidia_qualified = service.normalize_for_provider("nvidia", f"nvidia/{nvidia_model}", "medium")
    assert nvidia_qualified["provider_params"] == {"reasoning_effort": "medium"}


def test_resolve_model_candidates_exact_and_ambiguous_matches(tmp_path, monkeypatch):
    service = ModelRuntimeSettingsService(tmp_path)
    profiles = [
        _profile(
            "openai/gpt-4o",
            display_name="GPT-4o",
            provider_id="openai",
            model_id="gpt-4o",
        ),
        _profile(
            "openrouter/openai/gpt-4o",
            display_name="GPT-4o",
            provider_id="openrouter",
            model_id="openai/gpt-4o",
        ),
        _profile(
            "google/gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            provider_id="google",
            model_id="gemini-2.5-flash",
            availability={"configured": True, "status": "configured"},
        ),
    ]
    monkeypatch.setattr(service, "_list_profile_catalog", lambda: profiles)

    exact = service.resolve_model_candidates("openai/gpt-4o")
    assert exact["exact"]["profile_id"] == "openai/gpt-4o"
    assert exact["candidates"][0]["profile_id"] == "openai/gpt-4o"

    ambiguous = service.resolve_model_candidates("GPT-4o")
    assert ambiguous["exact"] is None
    assert {candidate["profile_id"] for candidate in ambiguous["candidates"]} == {
        "openai/gpt-4o",
        "openrouter/openai/gpt-4o",
    }

    missing = service.resolve_model_candidates("does-not-exist")
    assert missing == {"query": "does-not-exist", "exact": None, "candidates": []}


def test_resolve_model_candidates_ranking_uses_match_and_runtime_tie_breaks(tmp_path, monkeypatch):
    service = ModelRuntimeSettingsService(tmp_path)
    service.update_settings({"favorite_profiles": ["favorite/alpha-favorite"]})
    profiles = [
        _profile(
            "plain/alpha-plain",
            display_name="Alpha Plain",
            provider_id="plain",
            model_id="alpha-plain",
        ),
        _profile(
            "favorite/alpha-favorite",
            display_name="Alpha Favorite",
            provider_id="favorite",
            model_id="alpha-favorite",
        ),
        _profile(
            "ollama/alpha-local",
            display_name="Alpha Local",
            provider_id="ollama",
            model_id="alpha-local",
            availability={"local": True},
        ),
        _profile(
            "configured/alpha-configured",
            display_name="Alpha Configured",
            provider_id="configured",
            model_id="alpha-configured",
            availability={"configured": True, "status": "configured"},
        ),
        _profile(
            "configured/not-alpha",
            display_name="Not Alpha",
            provider_id="configured",
            model_id="not-alpha",
            availability={"configured": True, "status": "configured"},
        ),
    ]
    monkeypatch.setattr(service, "_list_profile_catalog", lambda: profiles)

    result = service.resolve_model_candidates("Alpha", limit=4)

    assert result["exact"] is None
    assert [candidate["profile_id"] for candidate in result["candidates"]] == [
        "configured/alpha-configured",
        "ollama/alpha-local",
        "favorite/alpha-favorite",
        "plain/alpha-plain",
    ]
    assert result["candidates"][0]["configured"] is True
    assert result["candidates"][1]["local"] is True
    assert result["candidates"][2]["favorite"] is True


def test_resolve_model_candidates_limits_model_command_to_chat_profiles(tmp_path, monkeypatch):
    service = ModelRuntimeSettingsService(tmp_path)
    profiles = [
        _profile(
            "openai/text-embedding-3-small",
            display_name="Text Embedding 3 Small",
            provider_id="openai",
            model_id="text-embedding-3-small",
            profile_type="embedding",
        ),
        _profile(
            "openai/gpt-4o",
            display_name="GPT-4o",
            provider_id="openai",
            model_id="gpt-4o",
            availability={"configured": False, "status": "unconfigured"},
        ),
        _profile(
            "xiaomi-token-plan-sgp/mimo-v2.5-pro",
            display_name="MiMo V2.5 Pro",
            provider_id="xiaomi-token-plan-sgp",
            model_id="mimo-v2.5-pro",
            profile_type="reasoning",
            defaults={"chat": True, "reasoning": True},
            capabilities={"chat": True, "tool_calls": True},
        ),
        _profile(
            "reasoning-only/deep-think",
            display_name="Deep Think",
            provider_id="reasoning-only",
            model_id="deep-think",
            profile_type="reasoning",
            defaults={"reasoning": True},
            capabilities={"reasoning": True},
        ),
    ]
    monkeypatch.setattr(service, "_list_profile_catalog", lambda: profiles)

    embedding = service.resolve_model_candidates("text-embedding-3-small")
    mimo = service.resolve_model_candidates("xiaomi-token-plan-sgp/mimo-v2.5-pro")
    reasoning_only = service.resolve_model_candidates("deep-think")
    chat = service.resolve_model_candidates("openai/gpt-4o")

    assert embedding["candidates"] == []
    assert mimo["exact"]["profile_id"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert reasoning_only["candidates"] == []
    assert chat["exact"]["profile_id"] == "openai/gpt-4o"
    assert chat["exact"]["requires_api_key"] is True
    assert chat["exact"]["availability"]["status"] == "unconfigured"
