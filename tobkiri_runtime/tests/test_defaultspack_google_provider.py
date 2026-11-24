from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("provider_model_catalog_selected")


class TestDefaultspackGoogleProvider(unittest.TestCase):
    def test_detect_available_providers_accepts_gemini_api_key(self):
        from tests.v4_provider_runtime_support import exercise_captured_provider_send

        with tempfile.TemporaryDirectory() as tmpdir, pytest.MonkeyPatch.context() as monkeypatch:
            sent = exercise_captured_provider_send(
                Path(tmpdir),
                monkeypatch,
                "google",
                endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            )

        self.assertEqual(sent["captured"]["body"]["model"], "account-visible-model")
        self.assertNotIn("credential-canary", json.dumps(sent["result"]))

    def test_detect_available_providers_keeps_google_with_manifest_provider(self):
        from tests.v4_provider_runtime_support import exercise_captured_provider_send

        with tempfile.TemporaryDirectory() as tmpdir, pytest.MonkeyPatch.context() as monkeypatch:
            google = exercise_captured_provider_send(
                Path(tmpdir),
                monkeypatch,
                "google",
                endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            )
            xiaomi = exercise_captured_provider_send(
                Path(tmpdir),
                monkeypatch,
                "xiaomi-token-plan-sgp",
                endpoint="https://token-plan-sgp.xiaomimimo.com/v1",
            )

        self.assertNotEqual(
            google["credential_digest"],
            xiaomi["credential_digest"],
        )
        self.assertNotIn("xiaomi-token-plan-sgp", google["captured"]["url"])
        self.assertNotIn("googleapis", xiaomi["captured"]["url"])

    def test_ai_client_resolves_google_when_manifest_provider_is_available(self):
        from tests.v4_provider_runtime_support import exercise_captured_provider_send

        with tempfile.TemporaryDirectory() as tmpdir, pytest.MonkeyPatch.context() as monkeypatch:
            sent = exercise_captured_provider_send(
                Path(tmpdir),
                monkeypatch,
                "google",
                endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
                model_id="gemma-4-31b-it",
            )

        self.assertEqual(sent["captured"]["body"]["model"], "gemma-4-31b-it")
        self.assertEqual(sent["result"]["output"], "ok")

    def test_google_provider_prefers_google_api_key_when_both_are_set(self):
        from tests.v4_provider_runtime_support import exercise_captured_provider_send

        with tempfile.TemporaryDirectory() as tmpdir, pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("GEMINI_API_KEY", "ambient-attacker")
            sent = exercise_captured_provider_send(
                Path(tmpdir),
                monkeypatch,
                "google",
                endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            )

        self.assertTrue(sent["credential_bound"])
        self.assertEqual(sent["provider_id"], "google")
        self.assertNotIn("ambient-attacker", json.dumps(sent, sort_keys=True))

    def test_google_provider_prefers_browser_oauth_bearer_when_available(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "google-key"}, clear=True):
            with patch("domain.ai_client.providers.google_provider.get_provider_access_token", return_value="oauth-token"):
                provider = GoogleProvider()
                provider._ensure_runtime_config()
                headers = provider._headers()

        self.assertEqual(headers["Authorization"], "Bearer oauth-token")

    def test_google_provider_uses_openai_compatible_chat_endpoint(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        def fake_request_json(path, body):
            captured["path"] = path
            captured["body"] = body
            return {
                "choices": [
                    {
                        "message": {"content": "hello from gemini"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
                "model": "gemini-2.5-flash",
            }

        provider._request_json = fake_request_json
        response = provider.complete(
            "gemini-2.5-flash",
            [{"role": "user", "content": "hello"}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            {
                "temperature": 0.2,
                "thinking_level": "low",
                "tool_choice": "auto",
                "stream_options": {"include_usage": False},
                "extra_body": {"google": {"thinking_config": {"include_thoughts": True}}},
            },
        )

        self.assertEqual(captured["path"], "/chat/completions")
        self.assertEqual(captured["body"]["model"], "gemini-2.5-flash")
        self.assertEqual(captured["body"]["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(captured["body"]["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(captured["body"]["reasoning_effort"], "low")
        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertEqual(captured["body"]["stream_options"], {"include_usage": False})
        self.assertEqual(captured["body"]["google"]["thinking_config"]["include_thoughts"], True)
        self.assertEqual(response["content"][0]["text"], "hello from gemini")

    def test_google_provider_autofixes_native_base_url_to_openai_compatible_path(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "gemini-key",
                "GOOGLE_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
            },
            clear=True,
        ):
            provider = GoogleProvider()

        self.assertEqual(
            provider.BASE_URL,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )

    def test_google_native_request_retries_transient_backend_errors(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        calls = []

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        def fake_urlopen(request, context=None, timeout=None):
            calls.append((request, context, timeout))
            if len(calls) < 3:
                raise urllib.error.HTTPError(
                    request.full_url,
                    500,
                    "Internal error encountered.",
                    {},
                    io.BytesIO(b'{"error":{"code":500}}'),
                )
            return io.BytesIO(b'{"ok":true}')

        with patch("domain.ai_client.providers.google_provider.urllib.request.urlopen", fake_urlopen), patch(
            "domain.ai_client.providers.google_provider._retry_sleep"
        ) as sleep:
            response = provider._native_request_json("gemini-test", {"contents": []})

        self.assertEqual(response.read(), b'{"ok":true}')
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    def test_google_native_request_retries_rate_limit_and_gateway_errors(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        codes = [429, 502, 504]
        calls = []

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        def fake_urlopen(request, context=None, timeout=None):
            calls.append((request, context, timeout))
            if codes:
                code = codes.pop(0)
                raise urllib.error.HTTPError(
                    request.full_url,
                    code,
                    "temporary",
                    {},
                    io.BytesIO(b'{"error":{"message":"temporary"}}'),
                )
            return io.BytesIO(b'{"ok":true}')

        with patch("domain.ai_client.providers.google_provider.urllib.request.urlopen", fake_urlopen), patch(
            "domain.ai_client.providers.google_provider._retry_sleep"
        ) as sleep:
            response = provider._native_request_json("gemini-test", {"contents": []})

        self.assertEqual(response.read(), b'{"ok":true}')
        self.assertEqual(len(calls), 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0, 1.5])

    def test_google_native_request_retries_transient_connection_errors(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        calls = []

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        def fake_urlopen(request, context=None, timeout=None):
            calls.append((request, context, timeout))
            if len(calls) < 2:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return io.BytesIO(b'{"ok":true}')

        with patch("domain.ai_client.providers.google_provider.urllib.request.urlopen", fake_urlopen), patch(
            "domain.ai_client.providers.google_provider._retry_sleep"
        ) as sleep:
            response = provider._native_request_json("gemini-test", {"contents": []})

        self.assertEqual(response.read(), b'{"ok":true}')
        self.assertEqual(len(calls), 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5])

    def test_google_native_request_respects_request_timeout_param(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {"parts": [{"text": "ok"}]},
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ).encode("utf-8")

        def fake_urlopen(request, context=None, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("domain.ai_client.providers.google_provider.urllib.request.urlopen", fake_urlopen):
            response = provider.complete(
                "gemma-4-31b-it",
                [{"role": "user", "content": "hello"}],
                [],
                {"request_timeout": 17},
            )

        self.assertEqual(captured["timeout"], 17.0)
        self.assertEqual(response["content"][0]["text"], "ok")

    def test_google_openai_compatible_request_retries_transient_backend_errors(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        calls = []

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, context=None, timeout=None):
            calls.append((request, context, timeout))
            if len(calls) < 3:
                raise urllib.error.HTTPError(
                    request.full_url,
                    500,
                    "Internal error encountered.",
                    {},
                    io.BytesIO(b'{"error":{"code":500,"message":"Internal error encountered."}}'),
                )
            return FakeResponse(b'{"ok":true}')

        with patch("domain.ai_client.providers.openai_provider.urllib.request.urlopen", fake_urlopen), patch(
            "domain.ai_client.providers.google_provider._retry_sleep"
        ) as sleep:
            response = provider._request_json("/chat/completions", {"model": "gemma-4-31b-it", "messages": []})

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    def test_google_openai_compatible_request_retries_transient_gateway_errors(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        calls = []

        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleProvider()
        provider._api_key = "gemini-key"

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, context=None, timeout=None):
            calls.append((request, context, timeout))
            if len(calls) < 2:
                raise urllib.error.HTTPError(
                    request.full_url,
                    504,
                    "Gateway Timeout",
                    {},
                    io.BytesIO(b'{"error":{"code":504,"message":"temporary"}}'),
                )
            return FakeResponse(b'{"ok":true}')

        with patch("domain.ai_client.providers.openai_provider.urllib.request.urlopen", fake_urlopen), patch(
            "domain.ai_client.providers.google_provider._retry_sleep"
        ) as sleep:
            response = provider._request_json("/chat/completions", {"model": "gemini-2.5-flash", "messages": []})

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5])

    def test_google_native_request_uses_bearer_auth_for_browser_oauth(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {}, clear=True):
            with patch("domain.ai_client.providers.google_provider.get_provider_access_token", return_value="oauth-token"):
                provider = GoogleProvider()

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, context=None, timeout=None):
            captured["headers"] = dict(request.headers)
            return FakeResponse(b'{"ok":true}')

        with patch("domain.ai_client.providers.google_provider.urllib.request.urlopen", fake_urlopen), patch(
            "domain.ai_client.providers.google_provider.get_provider_access_token",
            return_value="oauth-token",
        ):
            provider._native_request_json("gemini-test", {"contents": []})

        self.assertEqual(captured["headers"]["Authorization"], "Bearer oauth-token")
        self.assertNotIn("x-goog-api-key", {key.lower(): value for key, value in captured["headers"].items()})

    def test_google_provider_streams_openai_compatible_tool_call_deltas(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        class FakeStream:
            def __init__(self):
                self._chunks = [
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup","arguments":"{\\"q\\""}}]},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"rumi\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n',
                    b"data: [DONE]\n\n",
                ]

            def read(self, _size=4096):
                return self._chunks.pop(0) if self._chunks else b""

            def close(self):
                captured["closed"] = True

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        def fake_request_stream(path, body):
            captured["path"] = path
            captured["body"] = body
            return FakeStream()

        provider._request_stream = fake_request_stream
        chunks = list(provider.stream("gemini-2.5-flash", [{"role": "user", "content": "search"}], [], {}))

        self.assertEqual(captured["path"], "/chat/completions")
        self.assertEqual(captured["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(chunks[0], {"type": "tool_call_start", "id": "call_1", "name": "lookup"})
        self.assertEqual(chunks[1]["type"], "tool_call_delta")
        self.assertEqual(chunks[1]["arguments_chunk"], '{"q"')
        self.assertEqual(chunks[2]["arguments_chunk"], ':"rumi"}')
        self.assertEqual(chunks[3], {"type": "tool_call_end", "id": "call_1", "name": "lookup"})
        self.assertEqual(chunks[-1]["type"], "stream_end")

    def test_google_provider_strips_rumi_tool_metadata_before_request(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        def fake_request_json(path, body):
            captured["body"] = body
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            }

        provider._request_json = fake_request_json
        provider.complete(
            "gemini-2.5-flash",
            [{"role": "user", "content": "calculate"}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "Calculate an expression",
                        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
                    },
                    "metadata": {"source": "extension"},
                    "category": "math",
                    "action_type": "read",
                    "write_action": False,
                }
            ],
            {},
        )

        tool = captured["body"]["tools"][0]
        self.assertEqual(set(tool.keys()), {"type", "function"})
        self.assertEqual(tool["function"]["name"], "calculator")
        self.assertNotIn("metadata", tool)
        self.assertNotIn("category", tool)
        self.assertNotIn("action_type", tool)
        self.assertNotIn("write_action", tool)

    def test_google_provider_moves_inline_thoughts_to_metadata(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        provider = GoogleProvider()
        response = provider.parse_response(
            {
                "choices": [
                    {
                        "message": {"content": "<thought>private plan</thought> visible answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        )

        self.assertEqual(response["content"][0]["text"], "visible answer")
        self.assertEqual(response["metadata"]["thinking"]["transcript"], "private plan")
        self.assertNotIn("<thought>", response["content"][0]["text"])

    def test_google_provider_preserves_multimodal_content_blocks(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        provider = GoogleProvider()
        messages = provider.build_request(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                        },
                    ],
                }
            ]
        )

        self.assertEqual(messages[0]["content"][0]["text"], "what is in this image?")
        self.assertEqual(
            messages[0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,iVBORw0KGgo=",
        )

    def test_google_provider_hides_inline_thought_tags(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        response = GoogleProvider().parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "<thought>private reasoning</thought>赤",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        )

        self.assertEqual(response["content"][0]["text"], "赤")

    def test_google_provider_caps_gemini_thinking_levels(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        self.assertEqual(
            GoogleProvider._translate_params({"thinking_level": "xhigh"}, "gemini-3-pro-preview"),
            {"reasoning_effort": "high"},
        )
        self.assertEqual(
            GoogleProvider._translate_params({"thinking_level": "medium"}, "gemini-3-pro-preview"),
            {"reasoning_effort": "high"},
        )
        self.assertEqual(
            GoogleProvider._translate_params({"thinking_level": "none"}, "gemini-3-flash-preview"),
            {"reasoning_effort": "minimal"},
        )
        self.assertEqual(
            GoogleProvider._translate_params({"thinking_level": "none"}, "gemini-2.5-pro"),
            {},
        )
        self.assertEqual(
            GoogleProvider._translate_params({"thinking_level": "xhigh"}, "gemma-4-31b-it"),
            {"reasoning_effort": "high"},
        )
        self.assertEqual(
            GoogleProvider._translate_params({"thinking_level": "MINIMAL"}, "gemma-4-31b-it"),
            {"reasoning_effort": "minimal"},
        )

    def test_google_provider_uses_native_generative_api_for_gemma_4(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {"parts": [{"text": "gemma answer"}]},
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 2,
                            "candidatesTokenCount": 3,
                            "totalTokenCount": 5,
                        },
                    }
                ).encode("utf-8")

        def fake_native_request_json(model, body, stream=False, **kwargs):
            captured["model"] = model
            captured["body"] = body
            captured["stream"] = stream
            return FakeResponse()

        provider._native_request_json = fake_native_request_json
        response = provider.complete(
            "gemma-4-31b-it",
            [{"role": "user", "content": "hello"}],
            [{"function": {"name": "google_search"}}],
            {"thinking_level": "high"},
        )

        self.assertEqual(captured["model"], "gemma-4-31b-it")
        self.assertFalse(captured["stream"])
        self.assertEqual(captured["body"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "HIGH")
        self.assertEqual(captured["body"]["tools"], [{"googleSearch": {}}])
        self.assertEqual(response["content"][0]["text"], "gemma answer")
        self.assertEqual(response["usage"]["total_tokens"], 5)

    def test_google_provider_uses_native_generative_api_minimal_thinking_for_gemma_4(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {"parts": [{"text": "gemma answer"}]},
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 2,
                            "candidatesTokenCount": 3,
                            "totalTokenCount": 5,
                        },
                    }
                ).encode("utf-8")

        def fake_native_request_json(model, body, stream=False, **kwargs):
            captured["model"] = model
            captured["body"] = body
            captured["stream"] = stream
            return FakeResponse()

        provider._native_request_json = fake_native_request_json
        provider.complete(
            "gemma-4-31b-it",
            [{"role": "user", "content": "hello"}],
            [],
            {"thinking_level": "minimal"},
        )

        self.assertEqual(captured["model"], "gemma-4-31b-it")
        self.assertEqual(captured["body"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "MINIMAL")

    def test_google_native_gemma_sends_function_declarations_and_parses_calls(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "id": "call_browser_1",
                                                "name": "browser_use",
                                                "args": {"action": "screenshot"},
                                            }
                                        }
                                    ]
                                },
                                "finishReason": "STOP",
                            }
                        ],
                    }
                ).encode("utf-8")

        def fake_native_request_json(model, body, stream=False, **kwargs):
            captured["body"] = body
            return FakeResponse()

        provider._native_request_json = fake_native_request_json
        response = provider.complete(
            "gemma-4-31b-it",
            [
                {"role": "user", "content": "use the browser"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "previous_call",
                            "type": "function",
                            "function": {"name": "browser_use", "arguments": "{\"action\":\"screenshot\"}"},
                        }
                    ],
                },
                {"role": "tool", "name": "browser_use", "tool_call_id": "previous_call", "content": "{\"result\":\"ok\"}"},
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "browser_use",
                        "description": "Control the browser.",
                        "parameters": {
                            "type": "object",
                            "properties": {"action": {"type": "string"}},
                            "required": ["action"],
                        },
                    },
                }
            ],
            {"thinking_level": "high"},
        )

        self.assertEqual(captured["body"]["tools"][0]["functionDeclarations"][0]["name"], "browser_use")
        self.assertEqual(
            captured["body"]["tools"][0]["functionDeclarations"][0]["parameters"]["properties"]["action"]["type"],
            "string",
        )
        self.assertEqual(captured["body"]["contents"][1]["parts"][0]["functionCall"]["id"], "previous_call")
        self.assertEqual(captured["body"]["contents"][2]["parts"][0]["functionResponse"]["id"], "previous_call")
        self.assertEqual(response["finish_reason"], "tool_calls")
        self.assertEqual(response["content"][1]["type"], "tool_use")
        self.assertEqual(response["content"][1]["id"], "call_browser_1")
        self.assertEqual(response["content"][1]["name"], "browser_use")

    def test_google_native_gemma_adds_array_items_to_function_declarations(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        provider = GoogleProvider()
        body = provider._native_body(
            "gemma-4-31b-it",
            [{"role": "user", "content": "render a table"}],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "table_render",
                        "description": "Render table data.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "rows": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "cells": {"type": "array"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                }
            ],
            {},
        )

        parameters = body["tools"][0]["functionDeclarations"][0]["parameters"]
        rows_schema = parameters["properties"]["rows"]
        cells_schema = rows_schema["items"]["properties"]["cells"]
        self.assertEqual(rows_schema["type"], "array")
        self.assertEqual(rows_schema["items"]["type"], "object")
        self.assertEqual(cells_schema["type"], "array")
        self.assertEqual(cells_schema["items"]["type"], "object")

    def test_google_native_gemma_stream_parses_thought_and_function_call(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        class FakeStream:
            def __init__(self):
                self._chunks = [
                    b'data: {"candidates":[{"content":{"parts":[{"text":"I should use a tool.","thought":true},{"functionCall":{"id":"call_browser_1","name":"browser_use","args":{"action":"screenshot"}}}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1,"totalTokenCount":2}}\n\n',
                    b"data: [DONE]\n\n",
                ]

            def read(self, _size=4096):
                return self._chunks.pop(0) if self._chunks else b""

            def close(self):
                captured["closed"] = True

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        def fake_native_request_json(model, body, stream=False, **kwargs):
            captured["model"] = model
            captured["body"] = body
            captured["stream"] = stream
            return FakeStream()

        provider._native_request_json = fake_native_request_json
        chunks = list(
            provider.stream(
                "gemma-4-31b-it",
                [{"role": "user", "content": "use the browser"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "browser_use",
                            "description": "Control the browser.",
                            "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
                        },
                    }
                ],
                {"thinking_level": "high"},
            )
        )

        self.assertTrue(captured["stream"])
        self.assertEqual(chunks[0], {"type": "thinking_delta", "delta": {"type": "text", "text": "I should use a tool."}})
        self.assertEqual(chunks[1], {"type": "tool_call_start", "id": "call_browser_1", "name": "browser_use"})
        self.assertEqual(chunks[2]["type"], "tool_call_delta")
        self.assertEqual(json.loads(chunks[2]["arguments_chunk"]), {"action": "screenshot"})
        self.assertEqual(chunks[3], {"type": "tool_call_end", "id": "call_browser_1", "name": "browser_use"})
        self.assertEqual(chunks[-1]["finish_reason"], "tool_calls")
        self.assertTrue(captured["closed"])

    def test_google_native_gemma_sanitizes_invalid_tool_names_and_restores_original_name(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "id": "call_external_1",
                                                "name": "External_Send",
                                                "args": {"message": "hello"},
                                            }
                                        }
                                    ]
                                },
                                "finishReason": "STOP",
                            }
                        ],
                    }
                ).encode("utf-8")

        def fake_native_request_json(model, body, stream=False, **kwargs):
            captured["body"] = body
            return FakeResponse()

        provider._native_request_json = fake_native_request_json
        response = provider.complete(
            "gemma-4-31b-it",
            [
                {"role": "user", "content": "send externally"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "previous_call",
                            "type": "function",
                            "function": {"name": "External Send", "arguments": "{\"message\":\"hello\"}"},
                        }
                    ],
                },
                {"role": "tool", "name": "External Send", "tool_call_id": "previous_call", "content": "{\"result\":\"ok\"}"},
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "External Send",
                        "description": "Send to external destinations.",
                        "parameters": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    },
                }
            ],
            {"thinking_level": "high"},
        )

        declaration = captured["body"]["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["name"], "External_Send")
        self.assertEqual(captured["body"]["contents"][1]["parts"][0]["functionCall"]["name"], "External_Send")
        self.assertEqual(captured["body"]["contents"][2]["parts"][0]["functionResponse"]["name"], "External_Send")
        self.assertEqual(response["content"][1]["name"], "External Send")

    def test_google_provider_openai_compatible_sanitizes_invalid_tool_names_and_restores_original_name(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        captured = {}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            provider = GoogleProvider()

        def fake_request_json(path, body):
            captured["body"] = body
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_external_1",
                                    "type": "function",
                                    "function": {"name": "External_Send", "arguments": "{\"message\":\"hello\"}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {},
            }

        provider._request_json = fake_request_json
        response = provider.complete(
            "gemini-2.5-flash",
            [
                {"role": "user", "content": "send externally"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "previous_call",
                            "type": "function",
                            "function": {"name": "External Send", "arguments": "{\"message\":\"hello\"}"},
                        }
                    ],
                },
                {"role": "tool", "name": "External Send", "tool_call_id": "previous_call", "content": "{\"result\":\"ok\"}"},
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "External Send",
                        "description": "Send to external destinations.",
                        "parameters": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    },
                }
            ],
            {},
        )

        self.assertEqual(captured["body"]["tools"][0]["function"]["name"], "External_Send")
        self.assertEqual(captured["body"]["messages"][1]["tool_calls"][0]["function"]["name"], "External_Send")
        self.assertEqual(captured["body"]["messages"][2]["name"], "External_Send")
        self.assertEqual(response["content"][1]["name"], "External Send")

    def test_openai_provider_translates_generic_thinking_level(self):
        from domain.ai_client.providers.openai_provider import OpenAIProvider

        provider = OpenAIProvider()
        captured = {}

        def fake_request_json(path, body):
            captured["path"] = path
            captured["body"] = body
            return {
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }

        provider._request_json = fake_request_json
        provider.complete(
            "gpt-5.4",
            [{"role": "user", "content": "think"}],
            [],
            {"thinking_level": "xhigh"},
        )

        self.assertEqual(captured["path"], "/chat/completions")
        self.assertEqual(captured["body"]["reasoning_effort"], "high")
        self.assertNotIn("thinking_level", captured["body"])

    def test_anthropic_provider_translates_generic_thinking_level(self):
        from domain.ai_client.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider()
        captured = {}

        def fake_request_json(path, body):
            captured["path"] = path
            captured["body"] = body
            return {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
            }

        provider._request_json = fake_request_json
        provider.complete(
            "claude-sonnet-4-6",
            [{"role": "user", "content": "think"}],
            [],
            {"thinking_level": "xhigh", "max_tokens": 4096},
        )

        self.assertEqual(captured["path"], "/v1/messages")
        self.assertEqual(captured["body"]["thinking"]["budget_tokens"], 16384)
        self.assertGreaterEqual(captured["body"]["max_tokens"], 17408)
        self.assertNotIn("thinking_level", captured["body"])

    def test_google_provider_key_can_be_saved_as_defaultspack_secret(self):
        from core_runtime.secrets_store import SecretsStore
        from domain.ai_client.api_key_store import (
            load_provider_api_keys_into_env,
            provider_has_api_key,
            set_provider_api_key,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / "secrets"
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}, clear=True):
                result = set_provider_api_key("google", "google-secret")
                store = SecretsStore(str(secrets_dir))

                self.assertTrue(result["success"])
                self.assertEqual(result["key"], "GOOGLE_API_KEY")
                self.assertTrue(provider_has_api_key("google"))
                self.assertTrue(store.has_secret("GOOGLE_API_KEY"))

                os.environ.pop("GOOGLE_API_KEY", None)
                loaded = load_provider_api_keys_into_env()

        self.assertTrue(loaded["google"])

    def test_named_provider_api_key_can_be_saved_and_listed(self):
        from core_runtime.secrets_store import SecretsStore
        from domain.ai_client.api_key_store import (
            delete_provider_api_key,
            named_provider_secret_key,
            provider_has_api_key,
            provider_named_api_keys,
            rename_provider_api_key,
            read_provider_api_key,
            set_provider_api_key,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / "secrets"
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}, clear=True):
                result = set_provider_api_key(
                    "google",
                    "google-main-secret",
                    api_id="main",
                    name="Main",
                )
                key = named_provider_secret_key("google", api_id="main")
                store = SecretsStore(str(secrets_dir))

                self.assertTrue(result["success"])
                self.assertEqual(result["key"], key)
                self.assertTrue(store.has_secret(key))
                self.assertTrue(provider_has_api_key("google"))
                self.assertEqual(read_provider_api_key("google", "main"), "google-main-secret")
                listed = provider_named_api_keys("google")
                self.assertEqual(listed[0]["api_id"], "main")
                self.assertEqual(listed[0]["name"], "Main")
                self.assertEqual(listed[0]["label"], "google:main:***")

                renamed = rename_provider_api_key("google", "main", "work")
                self.assertTrue(renamed["success"])
                self.assertEqual(provider_named_api_keys("google")[0]["api_id"], "work")
                self.assertEqual(read_provider_api_key("google", "work"), "google-main-secret")

                deleted = delete_provider_api_key("google", "work")
                self.assertTrue(deleted["success"])
                self.assertFalse(provider_has_api_key("google"))

    def test_named_google_key_registers_runtime_without_cloud_flag(self):
        from domain.ai_client.api_key_store import set_provider_api_key
        from domain.ai_client.client import AIClient

        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / "secrets"
            env = {
                "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
                "RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS": "",
            }
            with patch.dict(os.environ, env, clear=True):
                set_provider_api_key("google", "google-main-secret", api_id="main", name="Main")
                AIClient._instance = None
                client = AIClient()

                self.assertIn("google", client._active_provider_ids())

    def test_google_provider_loads_profile_models_from_user_data(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "profiles"
            target_dir = profile_dir / "gemma-3-12b-it"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "profile.json").write_text(
                json.dumps(
                    {
                        "provider_id": "google",
                        "model_id": "gemma-3-12b-it",
                        "display_name": "Gemma 3 12B IT",
                        "metadata": {"type": "chat"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(GoogleProvider, "PROFILE_DIR", profile_dir):
                provider = GoogleProvider()
                model_ids = {item["id"] for item in provider.list_models()}

        self.assertIn("google/gemma-3-12b-it", model_ids)
        self.assertIn("google/gemini-2.5-pro", model_ids)

    def test_google_catalog_includes_gemini_and_gemma_models(self):
        from domain.ai_client.providers import get_all_known_models

        model_ids = {item["id"] for item in get_all_known_models(provider_id="google")}

        self.assertIn("google/gemini-3-pro-preview", model_ids)
        self.assertIn("google/gemini-3-flash-preview", model_ids)
        self.assertIn("google/gemini-2.0-flash-lite", model_ids)
        self.assertNotIn("google/gemini-2.5-flash-lite", model_ids)
        self.assertIn("google/gemma-4-31b-it", model_ids)
        self.assertNotIn("google/gemma-4-26b-a4b-it", model_ids)
        self.assertIn("google/gemma-3-27b-it", model_ids)
        self.assertIn("google/gemma-3n-e4b-it", model_ids)

    def test_google_catalog_does_not_expose_xhigh_for_gemini(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        profiles = {item["id"]: item for item in GoogleProvider().list_models()}

        self.assertNotIn("xhigh", profiles["google/gemini-2.5-pro"].get("thinking_levels", []))
        self.assertEqual(profiles["google/gemini-3-pro-preview"].get("thinking_levels", []), [])
        self.assertEqual(profiles["google/gemma-4-31b-it"]["thinking_levels"], ["minimal", "high"])

    def test_google_catalog_marks_gemma_4_as_tool_and_vision_capable(self):
        from domain.ai_client.providers.google_provider import GoogleProvider

        profiles = {item["id"]: item for item in GoogleProvider().list_models()}

        self.assertIn("tool_calls", profiles["google/gemma-4-31b-it"]["capabilities"])
        self.assertIn("vision", profiles["google/gemma-4-31b-it"]["capabilities"])
        self.assertNotIn("google/gemma-4-26b-a4b-it", profiles)


if __name__ == "__main__":
    unittest.main()
