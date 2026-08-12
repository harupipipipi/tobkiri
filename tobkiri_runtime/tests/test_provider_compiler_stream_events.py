from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_openai_chat_stream_parser_emits_text_reasoning_and_end():
    from domain.ai_client.provider_compiler.base import CompiledProviderRequest
    from domain.ai_client.provider_compiler.openai_chat import OpenAIChatCompiler

    events = OpenAIChatCompiler().parse_stream_chunk(
        {"choices": [{"delta": {"content": "hi", "reasoning_content": "think"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}},
        CompiledProviderRequest(api_family="openai_chat", provider_id="openai", model="m", path=""),
    )

    assert [event.type for event in events] == ["content_delta", "reasoning_delta", "stream_end"]


def test_openai_chat_stream_parser_treats_trace_as_reasoning():
    from domain.ai_client.provider_compiler.base import CompiledProviderRequest
    from domain.ai_client.provider_compiler.openai_chat import OpenAIChatCompiler

    events = OpenAIChatCompiler().parse_stream_chunk(
        {"choices": [{"delta": {"trace": "private plan", "content": "final"}, "finish_reason": "stop"}]},
        CompiledProviderRequest(api_family="openai_chat", provider_id="opencode-go", model="kimi-k2.6", path=""),
    )

    assert [event.type for event in events] == ["content_delta", "reasoning_delta", "stream_end"]
    assert events[0].delta["text"] == "final"
    assert events[1].delta["text"] == "private plan"


def test_google_native_stream_parser_emits_tool_events():
    from domain.ai_client.provider_compiler.base import CompiledProviderRequest
    from domain.ai_client.provider_compiler.google_native import GoogleNativeCompiler

    events = GoogleNativeCompiler().parse_stream_chunk(
        {"candidates": [{"content": {"parts": [{"functionCall": {"id": "tc", "name": "lookup", "args": {}}}]}}]},
        CompiledProviderRequest(api_family="google_native", provider_id="google", model="m", path="", metadata={}),
    )

    assert [event.type for event in events] == ["tool_call_start", "tool_call_delta", "tool_call_end"]
