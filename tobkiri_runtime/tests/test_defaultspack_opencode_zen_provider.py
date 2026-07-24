from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.ai_client.providers import (  # noqa: E402
    opencode_zen_provider as opencode_zen_provider_module,
)


class _FakeSseResponse:
    def __init__(self, chunks, *, fail_after_chunks=False):
        self._chunks = iter(chunks)
        self._fail_after_chunks = fail_after_chunks
        self.closed = False

    def read(self, size):
        del size
        try:
            return next(self._chunks)
        except StopIteration:
            if self._fail_after_chunks:
                raise AssertionError("stream read continued after terminal SSE chunk")
            return b""

    def close(self):
        self.closed = True


class _FakeJsonResponse:
    def __init__(self, payload):
        import json

        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback

    def read(self):
        return self._body


def _provider(monkeypatch):
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "test-opencode-zen-key")
    from domain.ai_client.providers.opencode_zen_provider import OpencodeZenProvider

    return OpencodeZenProvider()


def test_opencode_zen_model_inventory_prefers_live_endpoint(monkeypatch):
    provider = _provider(monkeypatch)
    response = _FakeJsonResponse(
        {
            "data": [
                {
                    "id": "deepseek-v4-flash-free",
                    "display_name": "DeepSeek V4 Flash Free",
                },
                {"id": "account-only-model", "display_name": "Account Model"},
            ]
        }
    )

    with patch.object(
        opencode_zen_provider_module.urllib.request,
        "urlopen",
        return_value=response,
    ):
        models = provider.list_models()

    assert [model["model_id"] for model in models] == [
        "deepseek-v4-flash-free",
        "account-only-model",
    ]
    assert all(model["metadata"]["inventory_source"] == "live" for model in models)
    assert models[0]["metadata"]["transport"] == "openai_chat_completions"


@pytest.mark.parametrize("payload", [{"data": []}, {"unexpected": []}])
def test_opencode_zen_model_inventory_falls_back_when_live_inventory_is_empty(
    monkeypatch, payload
):
    provider = _provider(monkeypatch)

    with patch.object(
        opencode_zen_provider_module.urllib.request,
        "urlopen",
        return_value=_FakeJsonResponse(payload),
    ):
        models = provider.list_models()

    assert models == []


def test_opencode_zen_model_inventory_falls_back_on_network_failure(monkeypatch):
    provider = _provider(monkeypatch)

    with patch.object(
        opencode_zen_provider_module.urllib.request,
        "urlopen",
        side_effect=TimeoutError,
    ):
        models = provider.list_models()

    assert models == []


def test_opencode_zen_model_inventory_uses_last_known_good_after_refresh_failure(monkeypatch):
    provider = _provider(monkeypatch)
    provider.MODEL_INVENTORY_TTL_SECONDS = 0

    with patch.object(
        opencode_zen_provider_module.urllib.request,
        "urlopen",
        side_effect=[
            _FakeJsonResponse({"data": [{"id": "account-only-model"}]}),
            TimeoutError(),
        ],
    ):
        live = provider.list_models()
        fallback = provider.list_models()

    assert [model["model_id"] for model in live] == ["account-only-model"]
    assert [model["model_id"] for model in fallback] == ["account-only-model"]
    assert fallback[0]["metadata"]["inventory_source"] == "last_known_good"
    assert fallback[0]["metadata"]["inventory_stale"] is True


def test_opencode_zen_catalog_uses_live_inventory_not_bundled_models():
    from domain.ai_client.providers import get_all_known_models, get_provider_catalog_map

    catalog = get_provider_catalog_map()
    provider = catalog["opencode-zen"]
    models = {item["id"]: item for item in get_all_known_models("opencode-zen")}

    assert provider["metadata"]["adapter"] == "python_entrypoint"
    assert provider["metadata"]["default_base_url"] == "https://opencode.ai/zen"
    assert provider["env_vars"] == ["OPENCODE_ZEN_API_KEY"]
    assert provider["default_model"] == ""
    assert provider["default_model_for"] == {}
    assert models == {}


def test_opencode_zen_reasoning_complete_uses_live_openai_model_and_token_floor(
    monkeypatch,
):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [
        {"model_id": "deepseek-v4-flash-free"}
    ]
    captured = {}

    def fake_request_openai_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_test",
            "model": "deepseek-v4-flash-free",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    with patch.object(
        provider,
        "_request_openai_json",
        side_effect=fake_request_openai_json,
    ):
        result = provider.complete(
            "opencode/deepseek-v4-flash-free",
            [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Say OK"},
            ],
            [{"name": "noop", "input_schema": {"type": "object"}}],
            {"max_tokens": 8, "temperature": 0},
        )

    assert captured["path"] == "/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-v4-flash-free"
    assert captured["body"]["max_tokens"] == 96
    assert captured["body"]["temperature"] == 0
    assert "tools" not in captured["body"]
    assert result["content"] == [{"type": "text", "text": "OK"}]


def test_opencode_zen_mimo_free_uses_openai_chat_completions(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_openai_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_test",
            "model": "mimo-v2.5-free",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    with patch.object(provider, "_request_openai_json", side_effect=fake_request_openai_json):
        result = provider.complete(
            "opencode-zen/mimo-v2.5-free",
            [{"role": "user", "content": "Say OK"}],
            [{"type": "function", "function": {"name": "noop"}}],
            {
                "max_tokens": 8,
                "temperature": 0,
                "reasoning_effort": "high",
                "tool_choice": "auto",
            },
        )

    assert captured["path"] == "/v1/chat/completions"
    assert captured["body"]["model"] == "mimo-v2.5-free"
    assert captured["body"]["max_tokens"] == 8
    assert captured["body"]["temperature"] == 0
    assert "tools" not in captured["body"]
    assert "tool_choice" not in captured["body"]
    assert "reasoning_effort" not in captured["body"]
    assert result["content"] == [{"type": "text", "text": "OK"}]


def test_opencode_zen_mimo_free_preserves_tool_call_continuations(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_openai_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_tool_followup",
            "model": "mimo-v2.5-free",
            "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }

    messages = [
        {"role": "user", "content": "Call noop."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_noop",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
            "metadata": {"thinking": {"transcript": "Need the noop result."}},
        },
        {"role": "tool", "tool_call_id": "call_noop", "name": "noop", "content": {"ok": True}},
    ]

    with patch.object(provider, "_request_openai_json", side_effect=fake_request_openai_json):
        result = provider.complete("opencode-zen/mimo-v2.5-free", messages, [], {"max_tokens": 8})

    assert captured["path"] == "/v1/chat/completions"
    assert captured["body"]["messages"][1]["tool_calls"] == messages[1]["tool_calls"]
    assert captured["body"]["messages"][1]["reasoning_content"] == "Need the noop result."
    assert captured["body"]["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_noop",
        "name": "noop",
        "content": '{"ok": true}',
    }
    assert result["content"] == [{"type": "text", "text": "Done"}]


def test_opencode_zen_reasoning_stream_omits_tools_and_applies_token_floor(
    monkeypatch,
):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [
        {"model_id": "deepseek-v4-flash-free"}
    ]
    captured = {}
    response = _FakeSseResponse(
        [
            b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n',
        ]
    )

    def fake_request_openai_stream(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return response

    with patch.object(
        provider,
        "_request_openai_stream",
        side_effect=fake_request_openai_stream,
    ):
        events = list(
            provider.stream(
                "opencode-zen/deepseek-v4-flash-free",
                [{"role": "user", "content": "Say OK"}],
                [{"name": "noop", "input_schema": {"type": "object"}}],
                {"max_tokens": 8},
            )
        )

    assert captured["path"] == "/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-v4-flash-free"
    assert captured["body"]["max_tokens"] == 96
    assert "tools" not in captured["body"]
    assert events[0] == {"type": "content_delta", "delta": {"type": "text", "text": "OK"}}
    assert events[-1]["type"] == "stream_end"
    assert response.closed is True


def test_opencode_zen_mimo_free_stream_stops_on_done_without_finish_chunk(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}
    response = _FakeSseResponse(
        [
            b'data: {"choices":[{"delta":{"reasoning_content":"The user wants"},'
            b'"finish_reason":null}]}\n\n',
            b"data: [DONE]\n\n",
        ],
        fail_after_chunks=True,
    )

    def fake_request_openai_stream(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return response

    with patch.object(provider, "_request_openai_stream", side_effect=fake_request_openai_stream):
        events = list(
            provider.stream(
                "opencode-zen/mimo-v2.5-free",
                [{"role": "user", "content": "Say OK"}],
                [{"name": "noop", "input_schema": {"type": "object"}}],
                {"max_tokens": 8},
            )
        )

    assert captured["path"] == "/v1/chat/completions"
    assert captured["body"]["model"] == "mimo-v2.5-free"
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert "tools" not in captured["body"]
    assert events == [
        {"type": "reasoning_delta", "delta": {"type": "text", "text": "The user wants"}},
        {
            "type": "stream_end",
            "finish_reason": "stop",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        },
    ]
    assert response.closed is True


def test_opencode_zen_stream_emits_one_end_after_final_usage(monkeypatch):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "mimo-v2.5-free"}]
    response = _FakeSseResponse(
        [
            b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n',
            b"data: [DONE]\n\n",
        ]
    )

    with patch.object(provider, "_request_openai_stream", return_value=response):
        events = list(
            provider.stream(
                "opencode-zen/mimo-v2.5-free",
                [{"role": "user", "content": "Say OK"}],
                [],
                {"max_tokens": 8},
            )
        )

    stream_ends = [event for event in events if event["type"] == "stream_end"]
    assert stream_ends == [
        {
            "type": "stream_end",
            "finish_reason": "stop",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 1,
                "total_tokens": 3,
            },
        }
    ]
    assert response.closed is True


def test_opencode_zen_secret_keys_and_detection(monkeypatch):
    from domain.ai_client.api_key_store import provider_secret_keys
    from domain.ai_client.providers import detect_available_providers
    from domain.ai_client.providers.opencode_zen_provider import OpencodeZenProvider

    assert provider_secret_keys("opencode-zen") == ["OPENCODE_ZEN_API_KEY"]
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "test-opencode-zen-key")
    assert isinstance(detect_available_providers()["opencode-zen"], OpencodeZenProvider)


def test_opencode_zen_rejects_unknown_model(monkeypatch):
    provider = _provider(monkeypatch)

    with pytest.raises(RuntimeError, match="unsupported model"):
        provider.complete("opencode-zen/not-a-real-model", [{"role": "user", "content": "hi"}], [], {})


def _live_enabled():
    return os.environ.get("RUMI_OPENCODE_ZEN_LIVE_TEST") == "1" and bool(os.environ.get("OPENCODE_ZEN_API_KEY"))


@pytest.mark.live
@pytest.mark.skipif(not _live_enabled(), reason="set RUMI_OPENCODE_ZEN_LIVE_TEST=1 and OPENCODE_ZEN_API_KEY")
def test_opencode_zen_live_inventory_and_free_model_complete():
    from domain.ai_client.providers.opencode_zen_provider import OpencodeZenProvider

    provider = OpencodeZenProvider()
    model_ids = [model["model_id"] for model in provider.list_models()]
    preferred = ["mimo-v2.5-free", "deepseek-v4-flash-free"]
    model_id = next(
        (candidate for candidate in preferred if candidate in model_ids),
        next((candidate for candidate in model_ids if candidate.endswith("-free")), ""),
    )
    assert model_id, "OpenCode Zen live inventory did not expose an invokable free model"
    result = provider.complete(
        model_id,
        [{"role": "user", "content": "Reply with exactly: OK"}],
        [],
        {"max_tokens": 128},
    )
    assert result["content"]
