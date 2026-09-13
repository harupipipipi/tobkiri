from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_model_capability_inference_for_frontier_vision_tool_model():
    from domain.ai_client.model_capabilities import flatten_capability_fields

    fields = flatten_capability_fields(
        {
            "qualified_model_id": "openrouter/openai/gpt-5.5-pro",
            "provider_id": "openrouter",
            "model_id": "openai/gpt-5.5-pro",
            "supports_tool_calling": True,
            "supports_vision": True,
            "supports_thinking": True,
        }
    )

    assert fields["supports_vision"] is True
    assert fields["supports_tool_calling"] is True
    assert fields["supports_thinking"] is True
    assert fields["knowledge_level"] == 96
    assert fields["knowledge_band"] == "gpt_5_5_pro_tier"
    assert "vision_ocr" in fields["recommended_roles"]


def test_model_type_chat_can_have_vision_and_thinking_capabilities():
    from domain.ai_client.model_capabilities import flatten_capability_fields

    fields = flatten_capability_fields(
        {
            "qualified_model_id": "example/chat-omni",
            "provider_id": "example",
            "model_id": "chat-omni",
            "type": "chat",
            "capabilities": {
                "text_input": True,
                "text_output": True,
                "image_input": True,
                "thinking": True,
                "tool_calling": True,
            },
            "thinking": {
                "supported": True,
                "levels": ["low", "medium", "high"],
                "default_level": "medium",
                "provider_mapping": {
                    "low": {"reasoning_effort": "low"},
                    "medium": {"reasoning_effort": "medium"},
                    "high": {"reasoning_effort": "high"},
                },
            },
        }
    )

    assert fields["supports_vision"] is True
    assert fields["supports_thinking"] is True
    assert fields["supports_tool_calling"] is True
    assert "vision_ocr" in fields["allowed_roles"]


def test_provider_catalog_enriches_models_and_profiles(provider_model_catalog_selected):
    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog, list_profile_catalog

    models = list_model_catalog(provider="google")
    gemini = next(item for item in models if item["model_id"] == "gemini-2.5-flash")
    assert "supports_vision" in gemini
    assert "supports_tool_calling" in gemini
    assert "knowledge_level" in gemini
    assert gemini["metadata"]["knowledge_band"] == gemini["knowledge_band"]

    profiles = list_profile_catalog()
    sample = next(item for item in profiles if item["profile_id"] == "google/gemini-2.5-flash")
    assert "capability_tags" in sample
    assert "recommended_roles" in sample
