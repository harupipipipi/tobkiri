from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_xiaomi_token_plan_provider_uses_api_key_header():
    from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import XiaomiMimoTokenPlanSgpProvider

    provider = XiaomiMimoTokenPlanSgpProvider(api_key="test-token")

    headers = provider._headers()

    assert headers["api-key"] == "test-token"
    assert "Authorization" not in headers


def test_xiaomi_token_plan_provider_passes_openai_tools():
    from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import XiaomiMimoTokenPlanSgpProvider

    provider = XiaomiMimoTokenPlanSgpProvider(api_key="test-token")
    captured = {}
    tool = {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }

    def fake_request_json(path, body):
        captured["path"] = path
        captured["body"] = body
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "I should call the write tool.",
                        "tool_calls": [
                            {
                                "id": "call_write",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": "{\"path\":\"demo.txt\",\"content\":\"ok\"}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        response = provider.complete(
            "mimo-v2.5-pro",
            [{"role": "user", "content": "Call the tool."}],
            [tool],
            {"tool_choice": {"type": "function", "function": {"name": "write_file"}}},
        )

    assert captured["path"] == "/chat/completions"
    assert captured["body"]["model"] == "mimo-v2.5-pro"
    assert captured["body"]["tools"] == [tool]
    assert captured["body"]["tool_choice"]["function"]["name"] == "write_file"
    assert response["content"][1]["type"] == "tool_use"
    assert response["content"][1]["name"] == "write_file"
    assert response["reasoning_content"] == "I should call the write tool."
    assert response["metadata"]["thinking"]["transcript"] == "I should call the write tool."

    followup_messages = provider.build_request(
        [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I should call the write tool.",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "{\"path\":\"demo.txt\",\"content\":\"ok\"}",
                        },
                    }
                ],
            },
        ]
    )
    assert followup_messages[0]["reasoning_content"] == "I should call the write tool."


def test_xiaomi_token_plan_models_are_tool_capable():
    from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import XiaomiMimoTokenPlanSgpProvider

    models = {
        item["id"]: item
        for item in XiaomiMimoTokenPlanSgpProvider(api_key="test-token").list_models()
    }

    pro = models["xiaomi-token-plan-sgp/mimo-v2.5-pro"]
    fast = models["xiaomi-token-plan-sgp/mimo-v2.5"]
    omni = models["xiaomi-token-plan-sgp/mimo-v2-omni"]

    assert pro["type"] == "reasoning"
    assert pro["defaults"]["chat"] is True
    assert pro["capabilities"]["tool_calls"] is True
    assert pro["metadata"]["tool_call_type"] == "openai"
    assert fast["defaults"]["fast"] is True
    assert omni["defaults"]["vision"] is True
    assert omni["capabilities"]["vision"] is True
    assert omni["metadata"]["vision_verified"] is True
    assert omni["metadata"]["request_defaults"]["top_p"] == 0.95
    assert "xiaomi-token-plan-sgp/mimo-v2-flash" not in models


def test_xiaomi_token_plan_rejects_removed_flash_model():
    from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import XiaomiMimoTokenPlanSgpProvider

    provider = XiaomiMimoTokenPlanSgpProvider(api_key="test-token")

    try:
        provider.complete("mimo-v2-flash", [{"role": "user", "content": "hello"}], [], {})
    except RuntimeError as exc:
        assert "unsupported model" in str(exc)
        assert "mimo-v2-omni" in str(exc)
    else:
        raise AssertionError("mimo-v2-flash should not be advertised or callable")



def test_xiaomi_token_plan_translates_thinking_level_to_xiaomi_thinking_payload():
    from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import XiaomiMimoTokenPlanSgpProvider

    provider = XiaomiMimoTokenPlanSgpProvider(api_key="test-token")

    translated = provider._translate_model_params("mimo-v2.5-pro", {"thinking_level": "high", "top_p": 0.8})
    disabled = provider._translate_model_params("mimo-v2.5-pro", {"thinking_level": "none"})

    assert translated["top_p"] == 0.8
    assert translated["extra_body"]["thinking"]["type"] == "enabled"
    assert "reasoning_effort" not in translated
    assert disabled["extra_body"]["thinking"]["type"] == "disabled"


def test_xiaomi_token_plan_credential_registers_sgp_only(
    monkeypatch,
    configured_cloud_provider,
):
    from domain.ai_client.client import AIClient
    from domain.ai_client.providers import detect_available_providers

    configured_cloud_provider("xiaomi-token-plan-sgp", "test-token")
    monkeypatch.setattr(AIClient, "_instance", None)

    token_plan_providers = {
        provider_id
        for provider_id in detect_available_providers()
        if provider_id.startswith("xiaomi-token-plan")
    }
    client = AIClient()
    provider, model_name = client.resolve_provider("mimo-v2.5-pro")

    assert token_plan_providers == {"xiaomi-token-plan-sgp"}
    assert provider.provider_id == "xiaomi-token-plan-sgp"
    assert model_name == "mimo-v2.5-pro"
