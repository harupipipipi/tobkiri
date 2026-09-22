"""Compatibility tests for the global-contract-backed chat gateway."""

import json
from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.ai_client import gateway_contract_client
from ecosystem.defaultspack.domain.ai_client.gateway_contract_client import (
    ContractLLMGateway,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    _adapter,
    _connection,
    _provider_model_id,
    _stream_result,
)

pytestmark = pytest.mark.contract


def test_contract_gateway_reports_its_stream_implementation() -> None:
    gateway = ContractLLMGateway()

    assert gateway.supports_stream("openrouter/tencent/hy3:free") is True
    assert gateway.supports_stream("") is False


def test_contract_gateway_binds_active_startup_profile(monkeypatch) -> None:
    captured = {}

    class Container:
        def get_or_none(self, _key):
            return object()

    monkeypatch.setattr(gateway_contract_client, "get_container", lambda: Container())
    monkeypatch.setattr(
        gateway_contract_client,
        "active_profile_id",
        lambda: "defaults-profile",
    )

    def invoke(_registry, _contract, _operation, payload):
        captured.update(payload)
        return {"events": []}

    monkeypatch.setattr(gateway_contract_client, "invoke_global_contract", invoke)

    assert gateway_contract_client.stream({"messages": []}) == []
    assert captured["profile_id"] == "defaults-profile"


def test_contract_gateway_projects_tool_intent_stream_to_chat_chunks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gateway_contract_client,
        "_invoke",
        lambda *_args, **_kwargs: {
            "events": [
                {
                    "type": "tool_intent_delta",
                    "tool_intent": {
                        "intent_id": "call-1",
                        "operation": "repository_context_prepare",
                        "arguments": {"workspace_id": "tobkiri-pr1322"},
                    },
                },
                {
                    "type": "usage",
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                    "usage_cost": {"amount": 0, "currency": "USD"},
                },
                {"type": "finish", "finish_reason": "tool_calls"},
            ]
        },
    )

    assert gateway_contract_client.stream({"messages": []}) == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "repository_context_prepare",
            "input": {"workspace_id": "tobkiri-pr1322"},
        },
        {
            "type": "stream_end",
            "finish_reason": "tool_calls",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 7,
                "usage_cost": {"amount": 0, "currency": "USD"},
            },
        },
    ]


def test_contract_gateway_projects_tool_intents_to_legacy_content(
    monkeypatch,
) -> None:
    intent = {
        "intent_id": "call-1",
        "operation": "repository_context_prepare",
        "arguments": {"workspace_id": "tobkiri-pr1322"},
    }
    monkeypatch.setattr(
        gateway_contract_client,
        "_invoke",
        lambda *_args, **_kwargs: {
            "output": "Preparing context.",
            "tool_intents": [intent],
            "finish_reason": "tool_calls",
        },
    )

    result = gateway_contract_client.generate({"messages": []})

    assert result["content"] == [
        {"type": "text", "text": "Preparing context."},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "repository_context_prepare",
            "input": {"workspace_id": "tobkiri-pr1322"},
        },
    ]
    assert result["tool_calls"] == [intent]


def test_contract_gateway_stream_fails_closed_when_contract_is_unconfigured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gateway_contract_client,
        "stream",
        lambda _payload: (_ for _ in ()).throw(
            gateway_contract_client.GlobalContractInvocationError(
                "not_configured",
                "provider connection is not configured",
            )
        ),
    )

    with pytest.raises(gateway_contract_client.GlobalContractInvocationError):
        list(ContractLLMGateway().stream({"model": "opencode-zen/test"}))


def test_contract_gateway_complete_fails_closed_when_contract_is_unconfigured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gateway_contract_client,
        "generate",
        lambda _payload: (_ for _ in ()).throw(
            gateway_contract_client.GlobalContractInvocationError(
                "not_configured",
                "provider connection is not configured",
            )
        ),
    )

    with pytest.raises(gateway_contract_client.GlobalContractInvocationError):
        ContractLLMGateway().complete({"model": "opencode-zen/test"})


def test_contract_gateway_does_not_fallback_after_denial(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway_contract_client,
        "stream",
        lambda _payload: (_ for _ in ()).throw(
            gateway_contract_client.GlobalContractInvocationError(
                "denied",
                "provider access denied",
            )
        ),
    )

    with pytest.raises(gateway_contract_client.GlobalContractInvocationError):
        list(ContractLLMGateway().stream({"model": "opencode-zen/test"}))


def test_provider_adapter_stream_preserves_tool_intents() -> None:
    intent = {
        "id": "call-1",
        "function": {
            "name": "repository_context_prepare",
            "arguments": '{"workspace_id":"tobkiri-pr1322"}',
        },
    }

    assert _stream_result(
        {
            "output": "",
            "tool_intents": [intent],
            "usage": {"input_tokens": 2},
            "finish_reason": "tool_calls",
        }
    ) == {
        "events": [
            {"type": "tool_intent_delta", "tool_intent": intent},
            {"type": "usage", "usage": {"input_tokens": 2}},
            {"type": "finish", "finish_reason": "tool_calls"},
        ]
    }


def test_provider_adapter_instance_matches_catalog_execution_hint() -> None:
    """Keep catalog models routable to the shared compatibility adapter."""
    ecosystem = Path(__file__).parents[1] / "ecosystem"
    adapter_manifest = json.loads(
        (
            ecosystem / "rumi_provider_adapters_pack" / "rumi.pack.v3.json"
        ).read_text(encoding="utf-8")
    )
    providers = adapter_manifest["contracts"]["provides"]

    assert len({provider["provider_instance_id"] for provider in providers}) == 4
    assert all(provider["routing_keys"] == ["*"] for provider in providers)


def test_openrouter_uses_the_openai_compatible_protocol() -> None:
    assert _adapter("openrouter") is _adapter("openai-compatible")
    assert _adapter("llm", provider_id="openrouter") is _adapter(
        "openai-compatible"
    )


def test_openrouter_model_id_drops_provider_prefix() -> None:
    assert _provider_model_id(
        {
            "provider_id": "openrouter",
            "model_id": "openrouter/tencent/hy3:free",
        }
    ) == "tencent/hy3:free"


def test_provider_connection_lookup_is_bound_to_startup_profile() -> None:
    class RegistryClient:
        def __init__(self) -> None:
            self.payload = None

        def invoke(self, _contract, _operation, payload):
            self.payload = dict(payload)
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.openrouter",
                        "enabled": True,
                    }
                ]
            }

    client = RegistryClient()

    connection = _connection(
        client,  # type: ignore[arg-type]
        {"provider_id": "openrouter", "profile_id": "defaults-profile"},
    )

    assert connection["provider_instance_id"] == "provider.openrouter"
    assert client.payload == {"profile_id": "defaults-profile"}
