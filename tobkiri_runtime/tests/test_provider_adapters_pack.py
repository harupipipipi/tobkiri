"""External-QA-oriented specifications for provider adapter boundaries."""

from __future__ import annotations

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    _adapter,
    _openai_compatible,
    create_generate_operation,
)


def test_adapter_selection_is_protocol_not_provider_specific() -> None:
    assert callable(_adapter("openai-compatible"))
    assert callable(_adapter("anthropic"))


def test_unknown_protocol_is_explicitly_incompatible() -> None:
    with pytest.raises(GlobalContractInvocationError) as exc:
        _adapter("provider-specific-name")

    assert exc.value.code == "incompatible"


def test_openai_compatible_requests_identify_the_client() -> None:
    captured = {}

    class FakeHostClient:
        def post_json_with_credential(self, **kwargs):
            captured.update(kwargs)
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }

    result = _openai_compatible(
        FakeHostClient(),
        {
            "provider_id": "opencode-zen",
            "model_id": "deepseek-v4-flash-free",
            "messages": [{"role": "user", "content": "hello"}],
        },
        {
            "endpoint": "https://opencode.ai/zen/v1",
            "provider_instance_id": "opencode-zen",
        },
        "opaque-handle",
        "provider.invoke",
        False,
    )

    assert result["output"] == "ok"
    assert captured["endpoint"] == "https://opencode.ai/zen/v1/chat/completions"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-Agent"] == "RumiAI/1.0"
    assert captured["credential_handle"] == "opaque-handle"


@pytest.mark.parametrize(
    "endpoint",
    ["http://provider.example/v1", "http://127.0.0.1:11434/v1"],
)
def test_adapter_rejects_credentialed_http_before_host_transport(endpoint: str) -> None:
    class FakeHostClient:
        posted = False

        def invoke(self, *_args, **_kwargs):
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.review-a",
                        "adapter_id": "openai-compatible",
                        "credential_handle": "credential:opaque-review-a",
                        "endpoint": endpoint,
                        "enabled": True,
                    }
                ]
            }

        def post_json_with_credential(self, **_kwargs):
            self.posted = True
            raise AssertionError("plaintext credential transport must not be called")

    client = FakeHostClient()
    with pytest.raises(GlobalContractInvocationError) as denied:
        create_generate_operation(client)(
            "generate",
            {
                "provider_id": "review-a",
                "model_id": "review-a/model",
                "messages": [],
            },
        )

    assert denied.value.code == "denied"
    assert client.posted is False
    assert "credential:opaque-review-a" not in str(denied.value)


def test_adapter_accepts_credentialed_https_record() -> None:
    captured = {}

    class FakeHostClient:
        def invoke(self, *_args, **_kwargs):
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.review-a",
                        "adapter_id": "openai-compatible",
                        "credential_handle": "credential:opaque-review-a",
                        "endpoint": "https://provider.example/v1",
                        "enabled": True,
                    }
                ]
            }

        def post_json_with_credential(self, **kwargs):
            captured.update(kwargs)
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }

    result = create_generate_operation(FakeHostClient())(
        "generate",
        {
            "provider_id": "review-a",
            "model_id": "review-a/model",
            "messages": [],
        },
    )

    assert result["output"] == "ok"
    assert captured["endpoint"] == "https://provider.example/v1/chat/completions"
    assert captured["credential_handle"] == "credential:opaque-review-a"
