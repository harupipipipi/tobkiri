"""External-QA-oriented specifications for provider adapter boundaries."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    _PROVIDER_OPERATIONS,
    _adapter,
    _openai_compatible,
    ProviderAdapterHostFactoryV4,
    create_generate_operation,
    create_stream_operation,
)


def test_adapter_selection_is_protocol_not_provider_specific() -> None:
    assert callable(_adapter("openai-compatible"))
    assert callable(_adapter("local-openai-compatible"))
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
                "provider_extension": {"trace": "adapter-owned"},
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
    assert "provider_extension" not in result


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


@pytest.mark.parametrize("streaming", [False, True])
def test_local_adapter_posts_only_to_bound_chat_route_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def read(self, amount: int) -> bytes:
            captured["read_limit"] = amount
            return json.dumps({
                "choices": [{
                    "message": {"content": "local ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"total_tokens": 3},
            }).encode()

        def close(self) -> None:
            captured["response_closed"] = True

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            captured.update(host=host, port=port, timeout=timeout)
            self.sock = SimpleNamespace(
                settimeout=lambda value: captured.setdefault(
                    "phase_timeouts", [],
                ).append(value)
            )

        def connect(self) -> None:
            captured["connected"] = True

        def request(self, method: str, path: str, **kwargs: Any) -> None:
            captured.update(method=method, path=path, **kwargs)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        "ecosystem.rumi_provider_adapters_pack.runtime.adapter.http.client.HTTPConnection",
        FakeConnection,
    )

    class FakeHostClient:
        def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"providers": [{
                "provider_instance_id": "provider.local",
                "adapter_id": "local-openai-compatible",
                "credential_handle": None,
                "endpoint": "http://127.0.0.1:1234/v1",
                "enabled": True,
            }]}

        def post_json_with_credential(self, **_kwargs: Any) -> None:
            raise AssertionError("local transport must not use credential transport")

    factory = create_stream_operation if streaming else create_generate_operation
    result = factory(FakeHostClient())(
        "stream" if streaming else "generate",
        {
            "provider_connection_id": "provider.local",
            "model_id": "gemma-local",
            "messages": [{"role": "user", "content": "hello"}],
            "deadline": time.time() + 5,
        },
    )

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 1234
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["closed"] is True
    assert captured["response_closed"] is True
    assert captured["connected"] is True
    assert len(captured["phase_timeouts"]) == 3
    assert b"gemma-local" in captured["body"]
    assert "Authorization" not in captured["headers"]
    if streaming:
        assert result["events"][0] == {
            "type": "text_delta", "delta": "local ok",
        }
    else:
        assert result["output"] == "local ok"


@pytest.mark.parametrize(
    "request_change,record_change",
    [
        ({"endpoint": "http://127.0.0.1:1234/v1"}, {}),
        ({"credential_handle": "credential:forged"}, {}),
        ({}, {"credential_handle": "credential:forged"}),
        ({}, {"endpoint": "http://localhost:1234/v1"}),
        ({}, {"endpoint": "http://127.0.0.1:1234/v1?redirect=1"}),
    ],
)
def test_local_adapter_rejects_caller_authority_and_invalid_registry_records(
    request_change: dict[str, Any],
    record_change: dict[str, Any],
) -> None:
    class FakeHostClient:
        def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"providers": [{
                "provider_instance_id": "provider.local",
                "adapter_id": "local-openai-compatible",
                "credential_handle": None,
                "endpoint": "http://127.0.0.1:1234/v1",
                "enabled": True,
                **record_change,
            }]}

    with pytest.raises(GlobalContractInvocationError) as denied:
        create_generate_operation(FakeHostClient())("generate", {
            "provider_connection_id": "provider.local",
            "model_id": "gemma-local",
            "messages": [],
            **request_change,
        })
    assert denied.value.code == "denied"


def test_captured_adapter_checks_invocation_before_and_after_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function_id = (
        "rumi_provider_adapters_pack.provider.compatibility.generate"
    )
    calls: list[str] = []

    def operation_factory(_client: Any) -> Any:
        def operation(name: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert name == "generate"
            assert payload == {"fixture": True}
            calls.append("operation")
            return {"output": "ok"}

        return operation

    monkeypatch.setitem(
        _PROVIDER_OPERATIONS, function_id, ("generate", operation_factory),
    )
    factory = ProviderAdapterHostFactoryV4(function_id)
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id, implementation_digest="sha256:implementation",
        ),
        operation=SimpleNamespace(
            contract_id="fixture.contract",
            contract_version="1.0.0",
            operation_id="fixture.operation",
        ),
        principal_ref=SimpleNamespace(value="fixture.principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    captured = factory.capture(SimpleNamespace(
        provider_bindings=(binding,),
        domain_ids={
            ("fixture.contract", "fixture.operation", "fixture.principal"):
            "fixture.domain",
        },
    ))

    class Invocation:
        def assert_current(self) -> None:
            calls.append("assert_current")

        def contract_client(self, **_kwargs: Any) -> object:
            calls.append("contract_client")
            return object()

    result = captured.contributions[0].invoke(
        "fixture.operation", {"fixture": True}, Invocation(),
    )
    assert result == {"output": "ok"}
    assert calls == [
        "assert_current", "contract_client", "operation", "assert_current",
    ]
