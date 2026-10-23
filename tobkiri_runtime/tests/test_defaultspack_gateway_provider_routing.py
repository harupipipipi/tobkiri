from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_auto_routing_emits_no_gateway_routing_objects():
    from domain.ai_client.provider_routing_settings import (
        openrouter_provider_options,
        vercel_gateway_options,
    )

    settings = {
        "gateway_routing_target": "all",
        "gateway_provider_mode": "auto",
    }

    assert openrouter_provider_options(settings) == {}
    assert vercel_gateway_options(settings) == {}


def test_gateway_slug_namespaces_are_kept_separate():
    from domain.ai_client.provider_routing_settings import (
        openrouter_provider_options,
        vercel_gateway_options,
    )

    settings = {
        "gateway_routing_target": "all",
        "openrouter_provider_mode": "only",
        "openrouter_primary_provider": "openrouter-slug",
        "vercel_provider_mode": "only",
        "vercel_primary_provider": "vercel-slug",
    }

    assert openrouter_provider_options(settings)["only"] == ["openrouter-slug"]
    assert vercel_gateway_options(settings)["only"] == ["vercel-slug"]


def test_provider_command_returns_typed_errors_for_unknown_gateway_inputs():
    from blocks.ai.provider_command import run

    unknown_target = run(
        {"target": "not-a-gateway", "upstream": "openai"},
        {},
    )
    unknown_mode = run(
        {"target": "openrouter", "mode": "sometimes"},
        {},
    )
    ambiguous_slug = run(
        {"target": "all", "upstream": "openai"},
        {},
    )

    assert unknown_target["error"]["code"] == "GATEWAY_TARGET_NOT_FOUND"
    assert unknown_mode["error"]["code"] == "GATEWAY_ROUTING_MODE_INVALID"
    assert ambiguous_slug["error"]["code"] == "GATEWAY_TARGET_REQUIRED"


def test_vercel_public_inventory_does_not_require_invocation_credentials(monkeypatch):
    from domain.ai_client.providers.vercel_ai_gateway_provider import (
        VercelAIGatewayProvider,
    )

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_AI_GATEWAY_API_KEY", raising=False)
    provider = VercelAIGatewayProvider(known_models=[])
    monkeypatch.setattr(provider, "_load_remote_model_cache", lambda: None)
    monkeypatch.setattr(provider, "_save_remote_model_cache", lambda models, now=None: None)
    monkeypatch.setattr(
        provider,
        "_fetch_remote_models",
        lambda: [
            {
                "id": "vercel-ai-gateway/openai/public-model",
                "model_id": "openai/public-model",
                "provider_id": "vercel-ai-gateway",
                "provider": "vercel-ai-gateway",
                "name": "Public model",
                "display_name": "Public model",
                "type": "chat",
            }
        ],
    )

    assert {model["model_id"] for model in provider.list_models()} == {"openai/public-model"}


def test_vercel_invocation_strips_provider_prefix_exactly_once(monkeypatch):
    monkeypatch.setattr(
        "domain.ai_client.providers.vercel_ai_gateway_provider.vercel_gateway_options",
        lambda: {},
    )
    from domain.ai_client.providers.vercel_ai_gateway_provider import (
        VercelAIGatewayProvider,
    )

    provider = VercelAIGatewayProvider(api_key="test-key", known_models=[])
    requests = []
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda path, body: (
            requests.append((path, body))
            or {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ),
    )

    provider.complete(
        "vercel-ai-gateway/google/gemma-test",
        [{"role": "user", "content": "hi"}],
        [],
        {},
    )
    provider.complete(
        "google/gemma-test",
        [{"role": "user", "content": "hi"}],
        [],
        {},
    )

    assert [body["model"] for _, body in requests] == [
        "google/gemma-test",
        "google/gemma-test",
    ]


def test_manifest_detection_uses_dedicated_vercel_adapter(tmp_path, monkeypatch):
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


def test_openrouter_invocation_uses_catalog_without_network_refresh(monkeypatch):
    from domain.ai_client.providers import openrouter_provider

    monkeypatch.setattr(
        openrouter_provider,
        "openrouter_provider_options",
        lambda: {},
    )

    provider = openrouter_provider.OpenRouterProvider(
        known_models=[
            {
                "id": "openrouter/openai/test-model",
                "model_id": "openai/test-model",
                "provider_id": "openrouter",
                "provider": "openrouter",
                "name": "Test model",
                "display_name": "Test model",
                "type": "chat",
            }
        ]
    )
    monkeypatch.setattr(provider, "_load_remote_model_cache", lambda: None)
    monkeypatch.setattr(
        provider,
        "_fetch_remote_models",
        lambda: (_ for _ in ()).throw(AssertionError("invocation refreshed inventory")),
    )
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda path, body: {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
    )

    provider.complete(
        "openrouter/openai/test-model",
        [{"role": "user", "content": "hi"}],
        [],
        {},
    )
