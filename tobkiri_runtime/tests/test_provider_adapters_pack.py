"""External-QA-oriented specifications for provider adapter boundaries."""

from __future__ import annotations

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_provider_adapters_pack.runtime import adapter as adapter_module
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    _adapter,
    _openai_compatible,
)


def test_adapter_selection_is_protocol_not_provider_specific() -> None:
    assert callable(_adapter("openai-compatible"))
    assert callable(_adapter("anthropic"))


def test_unknown_protocol_is_explicitly_incompatible() -> None:
    with pytest.raises(GlobalContractInvocationError) as exc:
        _adapter("provider-specific-name")

    assert exc.value.code == "incompatible"


def test_openai_compatible_requests_identify_the_client(monkeypatch) -> None:
    captured = {}

    def fake_post(endpoint, headers, body, request):
        captured.update(
            endpoint=endpoint,
            headers=dict(headers),
            body=dict(body),
            request=dict(request),
        )
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }

    monkeypatch.setattr(adapter_module, "_post", fake_post)

    result = _openai_compatible(
        {
            "provider_id": "opencode-zen",
            "model_id": "deepseek-v4-flash-free",
            "messages": [{"role": "user", "content": "hello"}],
        },
        {"endpoint": "https://opencode.ai/zen/v1"},
        {"api_key": "test-key"},
        False,
    )

    assert result["output"] == "ok"
    assert captured["endpoint"] == "https://opencode.ai/zen/v1/chat/completions"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-Agent"] == "RumiAI/1.0"
