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

pytestmark = pytest.mark.usefixtures("provider_model_catalog_selected")


ALL_MODELS = [
    "glm-5.1",
    "glm-5",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2.5-free",
    "minimax-m3",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.6-plus",
    "minimax-m2.7",
    "minimax-m2.5",
]

OPENAI_CHAT_MODELS = [
    "glm-5.1",
    "glm-5",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "mimo-v2.5-pro",
    "mimo-v2.5",
]

ANTHROPIC_MESSAGES_MODELS = [
    "minimax-m3",
    "minimax-m2.7",
    "minimax-m2.5",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.6-plus",
]
TOOL_CALL_MODELS = {"kimi-k2.6", "mimo-v2.5-pro", "mimo-v2.5"}
REASONING_EFFORT_MODELS = {"mimo-v2.5-pro", "mimo-v2.5"}
LIVE_SMOKE_MODEL = os.environ.get("RUMI_OPENCODE_GO_LIVE_MODEL", "minimax-m3")


class _FakeSseResponse:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def read(self, size):
        del size
        return next(self._chunks, b"")

    def close(self):
        self.closed = True


def _provider(monkeypatch):
    del monkeypatch
    from domain.ai_client.providers.opencode_go_provider import OpencodeGoProvider

    provider = OpencodeGoProvider()
    provider._api_key = "test-opencode-go-key"
    return provider


class _FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


def test_opencode_go_catalog_includes_all_models():
    from domain.ai_client.providers import get_all_known_models, get_provider_catalog_map

    catalog = get_provider_catalog_map()
    provider = catalog["opencode-go"]
    models = {item["id"]: item for item in get_all_known_models("opencode-go")}

    assert provider["metadata"]["adapter"] == "python_entrypoint"
    assert provider["metadata"]["default_base_url"] == "https://opencode.ai/zen/go/v1"
    assert provider["env_vars"] == ["OPENCODE_GO_API_KEY", "OPENCODE_ZEN_API_KEY"]
    assert provider["default_model_for"]["coding"] == "kimi-k2.6"
    assert provider["default_model_for"]["general"] == "qwen3.7-plus"
    assert provider["default_model_for"]["fast"] == "deepseek-v4-flash"
    assert provider["default_model_for"]["cheap"] == "deepseek-v4-flash"
    assert provider["default_model_for"]["vision"] == "qwen3.7-plus"
    assert "vision" in provider["capabilities"]
    assert {f"opencode-go/{model}" for model in ALL_MODELS}.issubset(models)

    minimax_m3 = models["opencode-go/minimax-m3"]
    assert minimax_m3["metadata"]["transport"] == "anthropic_messages"
    assert minimax_m3["metadata"]["endpoint_path"] == "/messages"
    assert minimax_m3["metadata"]["source"] == "opencode_go_docs"

    k27 = models["opencode-go/kimi-k2.7-code"]
    assert k27["metadata"]["transport"] == "openai_chat_completions"
    assert k27["metadata"]["endpoint_path"] == "/chat/completions"
    assert k27["metadata"]["source"] == "opencode_go_docs"

    kimi = models["opencode-go/kimi-k2.6"]
    assert kimi["metadata"]["transport"] == "openai_chat_completions"
    assert {"vision", "reasoning", "tool_calls"}.issubset(set(kimi["capabilities"]))
    assert kimi["metadata"]["capabilities"]["vision"] is True
    assert kimi["metadata"]["capabilities"]["tool_calls"] is True
    assert kimi["metadata"]["capabilities"]["reasoning"] is True
    assert kimi["metadata"]["tool_calls_verified"] is True
    assert kimi["metadata"]["vision_verified"] is True
    assert kimi["metadata"]["thinking_disabled_for_tool_calls"] is True

    qwen_max = models["opencode-go/qwen3.7-max"]
    assert qwen_max["defaults"]["reasoning"] is True
    assert qwen_max["metadata"]["transport"] == "anthropic_messages"
    assert qwen_max["metadata"]["endpoint_path"] == "/messages"
    assert "reasoning" in qwen_max["capabilities"]
    assert qwen_max["metadata"]["capabilities"]["reasoning"] is True

    minimax = models["opencode-go/minimax-m2.7"]
    assert minimax["metadata"]["transport"] == "anthropic_messages"
    assert minimax["metadata"]["endpoint_path"] == "/messages"

    qwen37 = models["opencode-go/qwen3.7-plus"]
    assert qwen37["metadata"]["transport"] == "anthropic_messages"
    assert {"vision", "reasoning"}.issubset(set(qwen37["capabilities"]))
    assert qwen37["metadata"]["capabilities"]["vision"] is True
    assert qwen37["metadata"]["capabilities"]["reasoning"] is True

    qwen36 = models["opencode-go/qwen3.6-plus"]
    assert qwen36["metadata"]["transport"] == "anthropic_messages"
    assert qwen36["metadata"]["endpoint_path"] == "/messages"

    mimo_free = models["opencode-go/mimo-v2.5-free"]
    assert mimo_free["model_id"] == "mimo-v2.5-free"
    assert mimo_free["defaults"] == {"chat": True}
    assert "mimo-v2.5-free" not in provider["default_model_for"].values()
    assert mimo_free["metadata"]["transport"] == "openai_chat_completions"
    assert mimo_free["metadata"]["endpoint_path"] == "/chat/completions"
    assert mimo_free["metadata"]["source"] == "opencode_go_compatibility_alias"
    assert mimo_free["metadata"]["alias_of"] == "opencode-go/mimo-v2.5"
    assert mimo_free["metadata"]["openai_model"] == "mimo-v2.5"
    assert "opencode-zen/mimo-v2.5-free" in mimo_free["metadata"]["compatibility_note"]
    assert "free_tier" not in mimo_free["metadata"]
    assert mimo_free["metadata"]["capabilities"]["tool_calls"] is True
    assert mimo_free["metadata"]["capabilities"]["reasoning"] is True
    assert mimo_free["metadata"]["reasoning_effort_verified"] is True

    for model_id in TOOL_CALL_MODELS:
        model_entry = models[f"opencode-go/{model_id}"]
        assert "tool_calls" in model_entry["capabilities"]
        assert model_entry["metadata"]["capabilities"]["tool_calls"] is True
        assert model_entry["metadata"]["tool_calls_verified"] is True
        assert model_entry["supports_thinking"] is True
        assert model_entry["metadata"]["capabilities"]["reasoning"] is True
        if model_id in REASONING_EFFORT_MODELS:
            assert model_entry["metadata"]["reasoning_effort_verified"] is True
        else:
            assert "reasoning_effort_verified" not in model_entry["metadata"]

    from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog

    legacy_models = {item["id"]: item for item in list_model_catalog("opencode-go")}
    legacy_kimi = legacy_models["opencode-go/kimi-k2.6"]
    assert legacy_kimi["supports_vision"] is True
    assert legacy_kimi["supports_image_input"] is True
    assert legacy_kimi["supports_tool_calling"] is True
    assert legacy_kimi["supports_thinking"] is True
    legacy_qwen37 = legacy_models["opencode-go/qwen3.7-plus"]
    assert legacy_qwen37["supports_vision"] is True
    assert legacy_qwen37["supports_image_input"] is True


def test_opencode_go_openai_transport_respects_request_timeout(monkeypatch):
    provider = _provider(monkeypatch)
    seen = {}

    def fake_urlopen(req, context=None, timeout=None):
        del req, context
        seen["timeout"] = timeout
        return _FakeJsonResponse(
            b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}],"usage":{}}'
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = provider.complete(
        "deepseek-v4-pro",
        [{"role": "user", "content": "hello"}],
        [],
        {"request_timeout": 4},
    )

    assert result["content"][0]["text"] == "ok"
    assert seen["timeout"] == 4.0


def test_opencode_go_messages_transport_respects_request_timeout(monkeypatch):
    provider = _provider(monkeypatch)
    seen = {}

    def fake_urlopen(req, context=None, timeout=None):
        del req, context
        seen["timeout"] = timeout
        return _FakeJsonResponse(
            b'{"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","usage":{}}'
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = provider.complete(
        "qwen3.7-plus",
        [{"role": "user", "content": "hello"}],
        [],
        {"request_timeout": 5},
    )

    assert result["content"][0]["text"] == "ok"
    assert seen["timeout"] == 5.0


@pytest.mark.parametrize("model", OPENAI_CHAT_MODELS)
def test_opencode_go_uses_chat_completions_for_openai_compatible_models(monkeypatch, model):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_test",
            "model": model,
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        result = provider.complete(
            f"opencode-go/{model}",
            [{"role": "user", "content": "Say OK"}],
            [{"type": "function", "function": {"name": "noop"}}],
            {
                "max_tokens": 8,
                "temperature": 0,
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
                "tool_choice": "auto",
            },
        )

    assert captured["path"] == "/chat/completions"
    assert captured["body"]["model"] == model
    assert captured["body"]["max_tokens"] == 8
    assert captured["body"]["temperature"] == 0
    if model in TOOL_CALL_MODELS:
        assert captured["body"]["tools"] == [{"type": "function", "function": {"name": "noop"}}]
        assert captured["body"]["tool_choice"] == "auto"
        if model == "kimi-k2.6":
            assert captured["body"]["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in captured["body"]
        else:
            assert captured["body"]["reasoning_effort"] == "high"
            assert "thinking" not in captured["body"]
    else:
        assert "tools" not in captured["body"]
        assert "tool_choice" not in captured["body"]
        assert "reasoning_effort" not in captured["body"]
        assert "thinking" not in captured["body"]
    assert result["content"] == [{"type": "text", "text": "OK"}]


def test_opencode_go_mimo_free_runtime_model_maps_to_go_compat_alias(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_test",
            "model": "mimo-v2.5",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {},
        }

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        result = provider.complete(
            "opencode-go/mimo-v2.5-free",
            [{"role": "user", "content": "Say OK"}],
            [{"type": "function", "function": {"name": "noop"}}],
            {
                "max_tokens": 8,
                "reasoning_effort": "high",
                "tool_choice": "auto",
            },
        )

    assert captured["path"] == "/chat/completions"
    assert captured["body"]["model"] == "mimo-v2.5"
    assert captured["body"]["tools"] == [{"type": "function", "function": {"name": "noop"}}]
    assert captured["body"]["tool_choice"] == "auto"
    assert captured["body"]["reasoning_effort"] == "high"
    assert result["content"] == [{"type": "text", "text": "OK"}]


def test_opencode_go_kimi_preserves_native_thinking_without_tools(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "chatcmpl_test",
            "model": "kimi-k2.6",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {},
        }

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        provider.complete(
            "opencode-go/kimi-k2.6",
            [{"role": "user", "content": "Say OK"}],
            [],
            {"thinking": {"type": "enabled"}, "max_tokens": 8},
        )

    assert captured["path"] == "/chat/completions"
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert "tools" not in captured["body"]


def test_opencode_go_kimi_image_analyze_uses_chat_vision(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {"choices": [{"message": {"content": "terminal visible"}}]}

    with patch.object(provider, "_request_json", side_effect=fake_request_json):
        result = provider.image_analyze(
            "opencode-go/kimi-k2.6",
            "data:image/png;base64,AAAA",
            "Describe the screen.",
        )

    assert result == {"text": "terminal visible"}
    assert captured["path"] == "/chat/completions"
    assert captured["body"]["model"] == "kimi-k2.6"
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_opencode_go_messages_vision_uses_messages_endpoint(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_messages_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {"content": [{"type": "text", "text": "image seen"}]}

    with patch.object(provider, "_request_messages_json", side_effect=fake_request_messages_json):
        result = provider.image_analyze(
            "opencode-go/qwen3.7-plus",
            "data:image/png;base64,AAAA",
            "Describe the screen.",
        )

    assert result == {"text": "image seen"}
    assert captured["path"] == "/messages"
    assert captured["body"]["model"] == "qwen3.7-plus"
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "AAAA",
    }
    assert content[1] == {"type": "text", "text": "Describe the screen."}


@pytest.mark.parametrize("model", ANTHROPIC_MESSAGES_MODELS)
def test_opencode_go_uses_messages_for_anthropic_compatible_models(monkeypatch, model):
    provider = _provider(monkeypatch)
    captured = {}

    def fake_request_messages_json(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "msg_test",
            "model": model,
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    with patch.object(provider, "_request_messages_json", side_effect=fake_request_messages_json):
        result = provider.complete(
            f"opencode-go/{model}",
            [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Say OK"},
            ],
            [{"name": "noop", "input_schema": {"type": "object"}}],
            {
                "max_tokens": 8,
                "temperature": 0,
                "stop": "END",
                "thinking": {"type": "enabled"},
            },
        )

    assert captured["path"] == "/messages"
    assert captured["body"]["model"] == model
    assert captured["body"]["max_tokens"] == 8
    assert captured["body"]["temperature"] == 0
    assert captured["body"]["stop_sequences"] == ["END"]
    assert captured["body"]["system"] == [{"type": "text", "text": "Be terse."}]
    assert "tools" not in captured["body"]
    assert "thinking" not in captured["body"]
    assert result["content"] == [{"type": "text", "text": "OK"}]


def test_opencode_go_secret_keys():
    from domain.ai_client.api_key_store import provider_secret_keys

    assert provider_secret_keys("opencode-go") == [
        "OPENCODE_GO_API_KEY",
        "OPENCODE_ZEN_API_KEY",
    ]


def test_opencode_go_detect_available_providers_with_key(tmp_path, monkeypatch):
    from tests.v4_provider_runtime_support import exercise_captured_provider_send

    sent = exercise_captured_provider_send(
        tmp_path,
        monkeypatch,
        "opencode-go",
        endpoint="https://opencode.ai/zen/go/v1",
    )

    assert sent["captured"]["body"]["model"] == "account-visible-model"
    assert "credential-canary" not in str(sent["result"])


def test_opencode_go_rejects_unknown_model(monkeypatch):
    provider = _provider(monkeypatch)

    with pytest.raises(RuntimeError, match="unsupported model"):
        provider.complete("opencode-go/not-a-real-model", [{"role": "user", "content": "hi"}], [], {})


def test_opencode_go_stream_parses_openai_sse(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}
    response = _FakeSseResponse(
        [
            b'data: {"choices":[{"delta":{"content":"O"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"K"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            b"data: [DONE]\n\n",
        ]
    )

    def fake_request_stream(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return response

    with patch.object(provider, "_request_stream", side_effect=fake_request_stream):
        events = list(provider.stream("kimi-k2.6", [{"role": "user", "content": "Say OK"}], [], {"max_tokens": 8}))

    assert captured["path"] == "/chat/completions"
    assert captured["body"]["model"] == "kimi-k2.6"
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert events[0] == {"type": "content_delta", "delta": {"type": "text", "text": "O"}}
    assert events[1] == {"type": "content_delta", "delta": {"type": "text", "text": "K"}}
    assert events[-1]["type"] == "stream_end"
    assert events[-1]["usage"]["total_tokens"] == 3
    assert response.closed is True


def test_opencode_go_stream_parses_anthropic_sse(monkeypatch):
    provider = _provider(monkeypatch)
    captured = {}
    response = _FakeSseResponse(
        [
            b'event: message_start\ndata: {"message":{"usage":{"input_tokens":1}}}\n\n'
            b'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"OK"}}\n\n'
            b'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":1}}\n\n'
            b"event: message_stop\ndata: {}\n\n",
        ]
    )

    def fake_request_messages_stream(path, body, **kwargs):
        del kwargs
        captured["path"] = path
        captured["body"] = body
        return response

    with patch.object(provider, "_request_messages_stream", side_effect=fake_request_messages_stream):
        events = list(provider.stream("minimax-m2.7", [{"role": "user", "content": "Say OK"}], [], {"max_tokens": 8}))

    assert captured["path"] == "/messages"
    assert captured["body"]["model"] == "minimax-m2.7"
    assert events[0] == {"type": "content_delta", "delta": {"type": "text", "text": "OK"}}
    assert events[-1]["type"] == "stream_end"
    assert events[-1]["usage"] == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    assert response.closed is True


def _live_enabled():
    return (
        os.environ.get("RUMI_OPENCODE_GO_LIVE_TEST") == "1"
        and bool(os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("OPENCODE_ZEN_API_KEY"))
    )


@pytest.mark.live
@pytest.mark.skipif(not _live_enabled(), reason="set RUMI_OPENCODE_GO_LIVE_TEST=1 and an OpenCode Go API key")
def test_opencode_go_live_messages_complete():
    from domain.ai_client.providers.opencode_go_provider import OpencodeGoProvider

    provider = OpencodeGoProvider()
    result = provider.complete(
        LIVE_SMOKE_MODEL,
        [{"role": "user", "content": "Reply with exactly: OK"}],
        [],
        {"max_tokens": 8},
    )
    assert result["content"]


@pytest.mark.live
@pytest.mark.skipif(not _live_enabled(), reason="set RUMI_OPENCODE_GO_LIVE_TEST=1 and an OpenCode Go API key")
def test_opencode_go_live_messages_stream():
    from domain.ai_client.providers.opencode_go_provider import OpencodeGoProvider

    provider = OpencodeGoProvider()
    events = list(
        provider.stream(
            LIVE_SMOKE_MODEL,
            [{"role": "user", "content": "Say OK"}],
            [],
            {"max_tokens": 8},
        )
    )
    assert events
