from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_vercel_ai_gateway_compiler_preserves_gateway_params():
    from domain.ai_client.bridge_plan import PlannedProviderRequest
    from domain.ai_client.provider_compiler.registry import compile_complete, compiler_for_api_family
    from domain.chat.ir_legacy_adapter import legacy_standard_messages_to_ir

    planned = PlannedProviderRequest(
        ir=legacy_standard_messages_to_ir([{"role": "user", "content": "hello"}], "c"),
        model="google/gemma-4-31b-it",
        provider_capabilities={
            "provider_id": "vercel-ai-gateway",
            "api_family": "vercel_ai_gateway",
        },
        params={
            "thinking_level": "high",
            "providerOptions": {"google": {"safetySettings": []}},
            "models": ["google/gemma-4-31b-it", "google/gemma-4-26b-a4b-it"],
            "max_tokens": 32,
        },
    )

    compiled = compile_complete(planned)

    assert compiler_for_api_family("vercel_ai_gateway") is not None
    assert compiled.api_family == "vercel_ai_gateway"
    assert compiled.provider_id == "vercel-ai-gateway"
    assert compiled.path == "/chat/completions"
    assert compiled.body["model"] == "google/gemma-4-31b-it"
    assert compiled.body["max_tokens"] == 32
    assert compiled.body["reasoning"] == {"effort": "high"}
    assert compiled.body["providerOptions"] == {"google": {"safetySettings": []}}
    assert compiled.body["models"] == ["google/gemma-4-31b-it", "google/gemma-4-26b-a4b-it"]
    assert "reasoning_effort" not in compiled.body


def test_vercel_ai_gateway_provider_catalog_and_credentials(
    monkeypatch,
    tmp_path,
    provider_model_catalog_selected,
):
    from domain.ai_client.api_key_store import provider_secret_keys
    from domain.ai_client.capabilities.registry import ProviderCapabilityRegistry
    from domain.ai_client.providers import get_all_known_models, get_provider_catalog_map

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_AI_GATEWAY_API_KEY", raising=False)

    provider = get_provider_catalog_map()["vercel-ai-gateway"]
    models = {item["id"]: item for item in get_all_known_models("vercel-ai-gateway")}
    caps = ProviderCapabilityRegistry().for_model(
        "vercel-ai-gateway/google/gemma-4-31b-it",
        models["vercel-ai-gateway/google/gemma-4-31b-it"],
    )

    assert provider["metadata"]["adapter"] == "openai_compatible"
    assert provider["metadata"]["default_base_url"] == "https://ai-gateway.vercel.sh/v1"
    assert provider["env_vars"] == ["AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"]
    assert provider["availability"]["supports_invoke"] is True
    assert provider["availability"]["configured"] is False
    assert provider_secret_keys("vercel-ai-gateway") == ["AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"]
    assert {
        "vercel-ai-gateway/google/gemma-4-31b-it",
        "vercel-ai-gateway/google/gemma-4-26b-a4b-it",
    }.issubset(models)
    assert models["vercel-ai-gateway/google/gemma-4-31b-it"]["context_window"] == 262144
    assert caps.api_family == "vercel_ai_gateway"
    assert caps.supports_vision is True
    assert caps.supports_reasoning is True

    from tests.v4_provider_runtime_support import exercise_captured_provider_send

    sent = exercise_captured_provider_send(
        tmp_path,
        monkeypatch,
        "vercel-ai-gateway",
        endpoint="https://ai-gateway.vercel.sh/v1",
    )
    assert sent["captured"]["url"] == (
        "https://ai-gateway.vercel.sh/v1/chat/completions"
    )
    assert "credential-canary" not in str(sent["result"])


def test_vercel_ai_gateway_legacy_provider_moves_gateway_params_to_body(monkeypatch):
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    captured = {}
    provider = OpenAICompatibleProvider(
        provider_id="vercel-ai-gateway",
        api_key="test-key",
        default_base_url="https://ai-gateway.vercel.sh/v1",
        credential_required=False,
    )

    def fake_request_json(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)

    provider.complete(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "hi"}],
        [],
        {
            "thinking_level": "medium",
            "providerOptions": {"google": {"safetySettings": []}},
            "models": ["google/gemma-4-31b-it"],
        },
    )

    assert captured["path"] == "/chat/completions"
    assert captured["body"]["reasoning"] == {"effort": "medium"}
    assert captured["body"]["providerOptions"] == {"google": {"safetySettings": []}}
    assert captured["body"]["models"] == ["google/gemma-4-31b-it"]
    assert "reasoning_effort" not in captured["body"]


@pytest.mark.skipif(
    not os.environ.get("RUMI_RUN_VERCEL_AI_GATEWAY_LIVE")
    or not (os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("VERCEL_AI_GATEWAY_API_KEY")),
    reason="Set RUMI_RUN_VERCEL_AI_GATEWAY_LIVE=1 and AI_GATEWAY_API_KEY or VERCEL_AI_GATEWAY_API_KEY to run live smoke.",
)
def test_vercel_ai_gateway_live_gemma_smoke_is_opt_in():
    from domain.ai_client.providers import detect_available_providers

    provider = detect_available_providers()["vercel-ai-gateway"]
    response = provider.complete(
        "google/gemma-4-31b-it",
        [{"role": "user", "content": "Reply with exactly: ok"}],
        [],
        {"max_tokens": 8, "reasoning_effort": "none"},
    )

    assert isinstance(response.get("content"), str)
    assert response["content"].strip()
