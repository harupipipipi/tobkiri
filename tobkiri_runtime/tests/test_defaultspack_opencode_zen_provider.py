from __future__ import annotations

import os
import json
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
    del monkeypatch
    from domain.ai_client.providers.opencode_zen_provider import OpencodeZenProvider

    provider = OpencodeZenProvider()
    provider._api_key = "test-opencode-zen-key"
    return provider


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
    assert models[0]["capabilities"]["tool_calling"] is True
    assert models[1]["capabilities"]["tool_calling"] is False
    assert all(model["capabilities"]["image_input"] is False for model in models)
    assert all(model["capabilities"]["vision"] is False for model in models)
    assert models[0]["metadata"]["tool_calling_verified"] is True
    assert models[1]["metadata"]["tool_calling_verified"] is False


@pytest.mark.parametrize("payload", [{"data": []}, {"unexpected": []}])
def test_opencode_zen_model_inventory_falls_back_when_live_inventory_is_empty(monkeypatch, payload):
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


def test_opencode_zen_model_inventory_uses_last_known_good_after_refresh_failure(
    monkeypatch,
):
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
    provider._model_inventory_cache = [{"model_id": "deepseek-v4-flash-free"}]
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
    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "noop",
                "description": "",
                "parameters": {"type": "object"},
            },
        }
    ]
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
    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "noop",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert captured["body"]["tool_choice"] == "auto"
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


def test_opencode_zen_converts_standard_tool_use_continuation(monkeypatch):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "deepseek-v4-flash-free"}]
    captured = {}

    def fake_request_openai_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_tool_followup",
            "model": "deepseek-v4-flash-free",
            "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }

    messages = [
        {"role": "user", "content": "Call get_magic_number."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": ""},
                {
                    "type": "tool_use",
                    "id": "call_magic",
                    "name": "get_magic_number",
                    "input": {},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_magic",
            "name": "get_magic_number",
            "content": {"value": 314159},
        },
    ]

    with patch.object(provider, "_request_openai_json", side_effect=fake_request_openai_json):
        result = provider.complete(
            "opencode-zen/deepseek-v4-flash-free",
            messages,
            [],
            {"max_tokens": 128},
        )

    assert captured["path"] == "/v1/chat/completions"
    assert captured["body"]["messages"][1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_magic",
                "type": "function",
                "function": {
                    "name": "get_magic_number",
                    "arguments": "{}",
                },
            }
        ],
    }
    assert captured["body"]["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_magic",
        "name": "get_magic_number",
        "content": '{"value": 314159}',
    }
    assert result["content"] == [{"type": "text", "text": "Done"}]


def test_opencode_zen_converts_agent_engine_tool_call_continuation(monkeypatch):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "deepseek-v4-flash-free"}]
    captured = {}

    def fake_request_openai_json(path, body, **kwargs):
        del path, kwargs
        captured["body"] = body
        return {
            "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
            "usage": {},
        }

    messages = [
        {"role": "user", "content": "Call get_magic_number."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "type": "tool_use",
                    "id": "call_magic",
                    "name": "get_magic_number",
                    "input": {},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_magic",
            "name": "get_magic_number",
            "content": "314159",
        },
    ]

    with patch.object(provider, "_request_openai_json", side_effect=fake_request_openai_json):
        provider.complete(
            "opencode-zen/deepseek-v4-flash-free",
            messages,
            [],
            {"max_tokens": 128},
        )

    assert captured["body"]["messages"][1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_magic",
                "type": "function",
                "function": {
                    "name": "get_magic_number",
                    "arguments": "{}",
                },
            }
        ],
    }
    assert captured["body"]["messages"][2]["tool_call_id"] == "call_magic"


def test_opencode_zen_reasoning_stream_omits_tools_and_applies_token_floor(
    monkeypatch,
):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "deepseek-v4-flash-free"}]
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
    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "noop",
                "description": "",
                "parameters": {"type": "object"},
            },
        }
    ]
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
    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "noop",
                "description": "",
                "parameters": {"type": "object"},
            },
        }
    ]
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


@pytest.mark.parametrize("container_key", ["message", "choice"])
def test_opencode_zen_stream_recovers_completed_tool_calls_outside_delta(
    monkeypatch,
    container_key,
):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "mimo-v2.5-free"}]
    tool_call = {
        "id": "call_repository",
        "type": "function",
        "function": {
            "name": "repository_context_prepare",
            "arguments": '{"workspace_id":"tobkiri-pr1322"}',
        },
    }
    choice = {"delta": {}, "finish_reason": "tool_calls"}
    if container_key == "message":
        choice["message"] = {"content": None, "tool_calls": [tool_call]}
    else:
        choice["tool_calls"] = tool_call
    response = _FakeSseResponse(
        [
            (
                "data: "
                + json.dumps(
                    {
                        "choices": [choice],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "total_tokens": 5,
                        },
                    }
                )
                + "\n\n"
            ).encode(),
            b"data: [DONE]\n\n",
        ]
    )

    with patch.object(provider, "_request_openai_stream", return_value=response):
        events = list(
            provider.stream(
                "opencode-zen/mimo-v2.5-free",
                [{"role": "user", "content": "Use the tool"}],
                [{"name": "repository_context_prepare", "input_schema": {"type": "object"}}],
                {},
            )
        )

    assert events == [
        {
            "type": "tool_call_start",
            "id": "call_repository",
            "name": "repository_context_prepare",
        },
        {
            "type": "tool_call_delta",
            "id": "call_repository",
            "name": "repository_context_prepare",
            "arguments_chunk": '{"workspace_id":"tobkiri-pr1322"}',
        },
        {
            "type": "tool_call_end",
            "id": "call_repository",
            "name": "repository_context_prepare",
        },
        {
            "type": "stream_end",
            "finish_reason": "tool_calls",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        },
    ]


def test_opencode_zen_stream_recovers_missing_tool_payload_with_complete_call(
    monkeypatch,
):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "mimo-v2.5-free"}]
    response = _FakeSseResponse(
        [
            (
                b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
                b'"usage":{"prompt_tokens":2,"completion_tokens":1,'
                b'"total_tokens":3}}\n\n'
            ),
            b"data: [DONE]\n\n",
        ]
    )
    recovered = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_recovered",
                            "type": "function",
                            "function": {
                                "name": "repository_context_prepare",
                                "arguments": '{"query":"find files"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 2,
            "total_tokens": 4,
        },
    }

    with (
        patch.object(
            provider,
            "_request_openai_stream",
            return_value=response,
        ),
        patch.object(
            provider,
            "_request_openai_json",
            return_value=recovered,
        ) as recover,
    ):
        events = list(
            provider.stream(
                "opencode-zen/mimo-v2.5-free",
                [{"role": "user", "content": "Use the tool"}],
                [
                    {
                        "name": "repository_context_prepare",
                        "input_schema": {"type": "object"},
                    }
                ],
                {},
            )
        )

    recover.assert_called_once()
    assert events == [
        {
            "type": "tool_call_start",
            "id": "call_recovered",
            "name": "repository_context_prepare",
        },
        {
            "type": "tool_call_delta",
            "id": "call_recovered",
            "name": "repository_context_prepare",
            "arguments_chunk": '{"query":"find files"}',
        },
        {
            "type": "tool_call_end",
            "id": "call_recovered",
            "name": "repository_context_prepare",
        },
        {
            "type": "stream_end",
            "finish_reason": "tool_calls",
            "usage": {
                "input_tokens": 4,
                "output_tokens": 3,
                "total_tokens": 7,
            },
        },
    ]


def test_opencode_zen_secret_keys_and_detection(tmp_path, monkeypatch):
    from domain.ai_client.api_key_store import provider_secret_keys
    from tests.v4_provider_runtime_support import exercise_captured_provider_send

    assert provider_secret_keys("opencode-zen") == ["OPENCODE_ZEN_API_KEY"]
    sent = exercise_captured_provider_send(
        tmp_path,
        monkeypatch,
        "opencode-zen",
        endpoint="https://opencode.ai/zen/v1",
    )
    assert sent["credential_bound"] is True
    assert sent["provider_id"] == "opencode-zen"


def test_opencode_zen_rejects_unknown_model(monkeypatch):
    provider = _provider(monkeypatch)

    with pytest.raises(RuntimeError, match="unsupported model"):
        provider.complete(
            "opencode-zen/not-a-real-model", [{"role": "user", "content": "hi"}], [], {}
        )


@pytest.mark.parametrize("method_name", ["complete", "stream"])
@pytest.mark.parametrize(
    "image_block",
    [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AA==",
            },
        },
        {"type": "input_image", "image_url": "https://example.invalid/image.png"},
    ],
)
def test_opencode_zen_rejects_image_input_before_network(monkeypatch, method_name, image_block):
    provider = _provider(monkeypatch)
    provider._model_inventory_cache = [{"model_id": "mimo-v2.5-free"}]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                image_block,
            ],
        }
    ]

    with (
        patch.object(provider, "_request_openai_json") as request_json,
        patch.object(provider, "_request_openai_stream") as request_stream,
        pytest.raises(RuntimeError, match="text-only"),
    ):
        result = getattr(provider, method_name)(
            "opencode-zen/mimo-v2.5-free",
            messages,
            [],
            {},
        )
        if method_name == "stream":
            list(result)

    request_json.assert_not_called()
    request_stream.assert_not_called()


def _live_enabled():
    return os.environ.get("RUMI_OPENCODE_ZEN_LIVE_TEST") == "1" and bool(
        os.environ.get("OPENCODE_ZEN_API_KEY")
    )


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(), reason="set RUMI_OPENCODE_ZEN_LIVE_TEST=1 and OPENCODE_ZEN_API_KEY"
)
def test_opencode_zen_live_inventory_and_free_model_complete():
    from domain.ai_client.providers.opencode_zen_provider import OpencodeZenProvider

    provider = OpencodeZenProvider()
    models = provider.list_models()
    model_ids = [model["model_id"] for model in models]
    assert all(model["capabilities"]["image_input"] is False for model in models)
    assert all(model["capabilities"]["vision"] is False for model in models)
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
