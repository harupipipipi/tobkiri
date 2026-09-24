from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_router_selects_vision_tool_model_inside_group():
    from domain.ai_client.model_router import ModelRoutingRequest, route_model_request

    profiles = [
        {"profile_id": "stub/default", "qualified_model_id": "stub/default", "provider_id": "stub", "model_id": "default", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False, "speed_tier": "fast", "knowledge_level": 0},
        {"profile_id": "google/gemini", "qualified_model_id": "google/gemini", "provider_id": "google", "model_id": "gemini", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": True, "supports_thinking": True, "speed_tier": "balanced", "knowledge_level": 85},
    ]
    decision = route_model_request(
        ModelRoutingRequest(
            has_images=True,
            requires_tool_calling=True,
            requested_thinking_level="high",
            preferred_model="stub/default",
            preferred_group="default",
            auto_route_within_group=True,
            settings={"model_groups": {"default": {"allowed_models": []}}},
        ),
        profiles=profiles,
    )

    assert decision.selected_model == "google/gemini"
    assert decision.bridge_required is False
    assert "requires_vision" in decision.reason_codes


def test_router_returns_bridge_plan_when_no_vision_model_available():
    from domain.ai_client.model_router import ModelRoutingRequest, route_model_request

    profiles = [
        {"profile_id": "local/text", "qualified_model_id": "local/text", "provider_id": "local", "model_id": "text", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False, "speed_tier": "fast", "knowledge_level": 30},
    ]
    decision = route_model_request(
        ModelRoutingRequest(has_images=True, preferred_model="local/text", settings={"on_switch_to_non_vision_with_images": "auto_bridge"}),
        profiles=profiles,
    )

    assert decision.bridge_required is True
    assert decision.bridge_plan["type"] == "vision_bridge"


def test_vision_bridge_prefers_a_vision_capable_lightweight_model():
    from domain.ai_client.model_router import _resolve_utility_models

    candidates = [
        {
            "profile_id": "demo/fast-vision",
            "configured": True,
            "supports_vision": True,
            "speed_tier": "fast",
            "knowledge_level": 40,
        },
        {
            "profile_id": "demo/fallback-vision",
            "configured": True,
            "supports_vision": True,
            "speed_tier": "balanced",
            "knowledge_level": 95,
        },
    ]

    resolved = _resolve_utility_models(
        {"lightweight_model": "demo/fast-vision", "utility_models": {}},
        candidates,
    )

    assert resolved["vision_ocr"] == "demo/fast-vision"


def test_vision_bridge_preserves_explicit_override_and_falls_back_from_nonvision_lightweight_model():
    from domain.ai_client.model_router import _resolve_utility_models

    candidates = [
        {
            "profile_id": "demo/text-fast",
            "configured": True,
            "supports_vision": False,
            "supports_image_input": False,
            "speed_tier": "fast",
            "knowledge_level": 50,
        },
        {
            "profile_id": "demo/fallback-vision",
            "configured": True,
            "supports_image_input": True,
            "speed_tier": "balanced",
            "knowledge_level": 90,
        },
    ]

    fallback = _resolve_utility_models(
        {"lightweight_model": "demo/text-fast", "utility_models": {}},
        candidates,
    )
    explicit = _resolve_utility_models(
        {
            "lightweight_model": "demo/text-fast",
            "utility_models": {"vision_ocr": "custom/vision"},
        },
        candidates,
    )

    assert fallback["vision_ocr"] == "demo/fallback-vision"
    assert explicit["vision_ocr"] == "custom/vision"


def test_router_honors_explicit_model_outside_active_group():
    from domain.ai_client.model_router import ModelRoutingRequest, route_model_request

    profiles = [
        {"profile_id": "google/gemini", "qualified_model_id": "google/gemini", "provider_id": "google", "model_id": "gemini", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": True, "supports_thinking": True, "speed_tier": "balanced", "knowledge_level": 85},
        {"profile_id": "gitlawb-opengateway/mimo-v2-omni", "qualified_model_id": "gitlawb-opengateway/mimo-v2-omni", "provider_id": "gitlawb-opengateway", "model_id": "mimo-v2-omni", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": False, "supports_thinking": False, "speed_tier": "balanced", "knowledge_level": 75},
    ]
    decision = route_model_request(
        ModelRoutingRequest(
            has_images=False,
            requested_thinking_level="high",
            preferred_model="gitlawb-opengateway/mimo-v2-omni",
            preferred_group="default",
            auto_route_within_group=True,
            settings={"model_groups": {"default": {"allowed_models": ["google/gemini"]}}},
        ),
        profiles=profiles,
    )

    assert decision.selected_model == "gitlawb-opengateway/mimo-v2-omni"
    assert "thinking_level_normalized" in decision.reason_codes


def test_router_honors_explicit_concrete_model_when_tools_are_requested():
    from domain.ai_client.model_router import ModelRoutingRequest, route_model_request

    profiles = [
        {"profile_id": "gitlawb-opengateway/mimo-v2-omni", "qualified_model_id": "gitlawb-opengateway/mimo-v2-omni", "provider_id": "gitlawb-opengateway", "model_id": "mimo-v2-omni", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": False, "supports_thinking": False, "speed_tier": "balanced", "knowledge_level": 75},
        {"profile_id": "google/gemini-2.5-pro", "qualified_model_id": "google/gemini-2.5-pro", "provider_id": "google", "model_id": "gemini-2.5-pro", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": True, "supports_thinking": True, "speed_tier": "fast", "knowledge_level": 95},
    ]
    decision = route_model_request(
        ModelRoutingRequest(
            requires_tool_calling=True,
            requested_tools=["browser_computer"],
            requested_thinking_level="high",
            preferred_model="gitlawb-opengateway/mimo-v2-omni",
            preferred_group="default",
            auto_route_within_group=True,
            settings={"model_groups": {"default": {"allowed_models": []}}},
        ),
        profiles=profiles,
    )

    assert decision.selected_model == "gitlawb-opengateway/mimo-v2-omni"
    assert "same_model" in decision.reason_codes
    assert "routed_within_group" not in decision.reason_codes
    assert "selected_model_does_not_support_tool_calling" in decision.warnings
    assert "selected_model_does_not_support_thinking" in decision.warnings


def test_router_still_routes_from_default_anchor_for_tools():
    from domain.ai_client.model_router import ModelRoutingRequest, route_model_request

    profiles = [
        {"profile_id": "stub/default", "qualified_model_id": "stub/default", "provider_id": "stub", "model_id": "default", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False, "speed_tier": "fast", "knowledge_level": 0},
        {"profile_id": "google/gemini-2.5-pro", "qualified_model_id": "google/gemini-2.5-pro", "provider_id": "google", "model_id": "gemini-2.5-pro", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": True, "supports_thinking": True, "speed_tier": "fast", "knowledge_level": 95},
    ]
    decision = route_model_request(
        ModelRoutingRequest(
            requires_tool_calling=True,
            requested_thinking_level="high",
            preferred_model="stub/default",
            preferred_group="default",
            auto_route_within_group=True,
            settings={"model_groups": {"default": {"allowed_models": []}}},
        ),
        profiles=profiles,
    )

    assert decision.selected_model == "google/gemini-2.5-pro"
    assert "routed_within_group" in decision.reason_codes


def test_router_excludes_embedding_profiles_from_chat_group_candidates():
    from domain.ai_client.model_search import models_for_group
    from domain.ai_client.model_router import ModelRoutingRequest, route_model_request

    profiles = [
        {
            "profile_id": "openai/text-embedding-3-small",
            "qualified_model_id": "openai/text-embedding-3-small",
            "provider_id": "openai",
            "model_id": "text-embedding-3-small",
            "type": "embedding",
            "configured": True,
        },
        {
            "profile_id": "reasoning/deep-only",
            "qualified_model_id": "reasoning/deep-only",
            "provider_id": "reasoning",
            "model_id": "deep-only",
            "type": "reasoning",
            "configured": True,
            "defaults": {"reasoning": True},
            "capabilities": {"reasoning": True},
        },
        {
            "profile_id": "openai/gpt-4o",
            "qualified_model_id": "openai/gpt-4o",
            "provider_id": "openai",
            "model_id": "gpt-4o",
            "type": "chat",
            "configured": True,
            "supports_tool_calling": True,
            "supports_thinking": True,
        },
    ]

    candidates = models_for_group(
        "default",
        {"model_groups": {"default": {"allowed_models": []}}},
        profiles=profiles,
    )
    decision = route_model_request(
        ModelRoutingRequest(
            requires_tool_calling=True,
            preferred_model="missing/model",
            preferred_group="default",
            auto_route_within_group=True,
            settings={"model_groups": {"default": {"allowed_models": []}}},
        ),
        profiles=profiles,
    )

    assert [item["profile_id"] for item in candidates] == ["openai/gpt-4o"]
    assert decision.selected_model == "openai/gpt-4o"
