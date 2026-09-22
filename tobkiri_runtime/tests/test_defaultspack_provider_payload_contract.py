from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def test_openai_provider_build_request_and_parse_tool_calls_contract():
    from domain.ai_client.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider()
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc", "content": "ok"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]},
    ]

    request = provider.build_request(messages)
    parsed = provider.parse_response(
        {
            "choices": [
                {
                    "message": {"content": "", "tool_calls": [{"id": "tc2", "function": {"name": "lookup", "arguments": "{}"}}]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
    )

    assert request[0]["tool_calls"][0]["function"]["name"] == "lookup"
    assert request[1]["role"] == "tool"
    assert request[2]["content"][0]["type"] == "image_url"
    assert parsed["content"][1] == {"type": "tool_use", "id": "tc2", "name": "lookup", "input": "{}"}


def test_non_vision_provider_payload_contains_no_image_blocks():
    from domain.ai_client.provider_compiler.registry import compile_complete
    from domain.ai_client.request_planner import plan_model_request
    from domain.chat.ir_legacy_adapter import legacy_standard_messages_to_ir

    ir = legacy_standard_messages_to_ir(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read it"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaa"}},
                ],
            }
        ],
        "c",
    )

    planned = plan_model_request(
        ir,
        "text-only-model",
        {
            "provider_id": "local",
            "api_family": "openai_chat",
            "supports_vision": False,
            "supported_content_blocks": ["text"],
        },
        [],
        {},
        {},
    )
    compiled = compile_complete(planned)

    dumped = json.dumps(compiled.body)
    assert "image_url" not in dumped
    assert "data:image/" not in dumped
    assert all(block.type not in {"image", "image_url"} for message in planned.ir.messages for block in message.content)
    assert any(action.action == "vision_bridge_required" for action in planned.bridge_actions)
    assert any(item.feature == "image_url" for item in planned.dropped_features)


def test_google_provider_tool_name_mapping_and_native_body_contract():
    from domain.ai_client.providers.google_provider import GoogleProvider

    tool = {"type": "function", "function": {"name": "External Send", "description": "send", "parameters": {"type": "object", "properties": {}}}}
    name_map, reverse = GoogleProvider._tool_name_maps([tool])
    provider = GoogleProvider()
    body = provider._native_body(
        "gemma-4-31b-it",
        [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "tc", "function": {"name": "External Send", "arguments": "{\"x\":1}"}}]},
            {"role": "tool", "tool_call_id": "tc", "name": "External Send", "content": "{\"ok\":true}"},
        ],
        [tool],
        {"thinking_level": "high"},
        name_map,
    )
    text, thought, finish, tool_uses = provider._native_extract_parts(
        {"candidates": [{"content": {"parts": [{"functionCall": {"id": "tc2", "name": "External_Send", "args": {"x": 1}}}]}}]},
        reverse,
    )

    assert body["tools"][0]["functionDeclarations"][0]["name"] == "External_Send"
    assert body["contents"][0]["parts"][0]["functionCall"]["name"] == "External_Send"
    assert body["contents"][1]["parts"][0]["functionResponse"]["name"] == "External_Send"
    assert tool_uses[0]["name"] == "External Send"
    assert finish == "tool_calls"
    assert text == thought == ""


def test_openai_compatible_cerebras_reasoning_none_contract(monkeypatch):
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="cerebras",
        api_key="key",
        base_url="https://example.test",
        known_models=[{"id": "cerebras/gpt-oss-120b", "model_id": "gpt-oss-120b", "supports_thinking": True}],
    )
    captured = {}
    monkeypatch.setattr(provider, "_request_json", lambda path, body: captured.setdefault("body", body) or {"choices": [{"message": {"content": "ok"}}]})

    provider.complete("gpt-oss-120b", [{"role": "user", "content": "hi"}], [], {"max_tokens": 7, "thinking_level": "none"})

    assert captured["body"]["max_completion_tokens"] == 7
    assert "reasoning_effort" not in captured["body"]


def test_openai_compatible_recovers_tool_call_omitted_by_stream(monkeypatch):
    from domain.ai_client.providers.openai_compatible_provider import (
        OpenAICompatibleProvider,
    )
    from domain.ai_client.providers.openai_provider import OpenAIProvider

    provider = OpenAICompatibleProvider(
        provider_id="compatible",
        api_key="key",
        base_url="https://example.test",
        known_models=[{"id": "compatible/model", "model_id": "model"}],
    )
    monkeypatch.setattr(
        OpenAIProvider,
        "stream",
        lambda *_args, **_kwargs: iter(
            [
                {
                    "type": "stream_end",
                    "finish_reason": "tool_calls",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                }
            ]
        ),
    )
    monkeypatch.setattr(
        provider,
        "complete",
        lambda *_args, **_kwargs: {
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_recovered",
                    "name": "repository_context_prepare",
                    "input": '{"query":"find files"}',
                }
            ],
            "finish_reason": "tool_calls",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 2,
                "total_tokens": 4,
            },
        },
    )

    events = list(
        provider.stream(
            "model",
            [{"role": "user", "content": "Use the tool"}],
            [{"type": "function", "function": {"name": "tool"}}],
            {},
        )
    )

    assert [event["type"] for event in events] == [
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
        "stream_end",
    ]
    assert events[-1]["usage"] == {
        "input_tokens": 4,
        "output_tokens": 3,
        "total_tokens": 7,
    }


def test_openrouter_chat_body_preserves_curated_gateway_params(monkeypatch):
    from domain.ai_client.providers.openrouter_provider import OpenRouterProvider

    captured = {}
    catalog_models = [
        {
            "id": "openrouter/tencent/hy3-preview:free",
            "model_id": "tencent/hy3-preview:free",
            "display_name": "Tencent Hy3 preview (free)",
            "provider": "openrouter",
            "provider_id": "openrouter",
            "type": "chat",
        },
        {
            "id": "openrouter/cohere/north-mini-code:free",
            "model_id": "cohere/north-mini-code:free",
            "display_name": "Cohere North Mini Code (free)",
            "provider": "openrouter",
            "provider_id": "openrouter",
            "type": "chat",
        },
    ]
    provider = OpenRouterProvider(known_models=[])
    monkeypatch.setattr(provider, "_remote_discovered_models", lambda: [dict(model) for model in catalog_models])

    def fake_request_json(path, body):
        captured["request"] = {"path": path, "body": body}
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)

    gateway_params = {
        "reasoning": {"effort": "high", "max_tokens": 1024},
        "include_reasoning": True,
        "provider": {"order": ["Cerebras", "Groq"], "allow_fallbacks": False},
        "models": ["openai/gpt-oss-120b", "qwen/qwen3-235b-a22b"],
        "web_search_options": {"search_context_size": "low"},
        "structured_outputs": True,
    }
    params = {
        **gateway_params,
        "tools": [
            {
                "type": "function",
                "function": {"name": "param_shadow", "parameters": {"type": "object"}},
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {"name": "real_tool", "parameters": {"type": "object"}},
        }
    ]
    provider.complete("cohere/north-mini-code:free", [{"role": "user", "content": "hi"}], tools, params)

    body = captured["request"]["body"]
    assert captured["request"]["path"] == "/chat/completions"
    for key, value in gateway_params.items():
        assert body[key] == value
    assert body["tools"] == tools


def test_groq_tool_messages_omit_name_in_chat_body(monkeypatch):
    from domain.ai_client.providers.provider_catalog import GroqProvider

    captured = {}
    provider = GroqProvider()

    def fake_request_json(path, body):
        captured["request"] = {"path": path, "body": body}
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)

    provider.complete(
        "llama-3.3-70b-versatile",
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "lookup", "content": "{\"ok\":true}"},
        ],
        [],
        {},
    )

    body = captured["request"]["body"]
    tool_message = next(message for message in body["messages"] if message["role"] == "tool")
    assert captured["request"]["path"] == "/chat/completions"
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "{\"ok\":true}",
    }
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "lookup"
