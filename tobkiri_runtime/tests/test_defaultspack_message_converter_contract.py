from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
DEFAULTS_ROOT = ROOT / "ecosystem" / "defaults"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def test_convert_to_standard_text_and_metadata_contract():
    from domain.chat.message_converter import convert_to_standard

    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
            "metadata": {"secret": "not prompt"},
            "usage": {"total_tokens": 99},
            "events": [{"type": "ignored"}],
            "tool_logs": [{"ignored": True}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]

    assert convert_to_standard(messages) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_convert_to_standard_multimodal_blocks_contract():
    from domain.chat.message_converter import convert_to_standard

    image_url = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
    messages = [{"role": "user", "content": [{"type": "text", "text": "look"}, image_url, image]}]

    assert convert_to_standard(messages) == [
        {"role": "user", "content": [{"type": "text", "text": "look"}, image_url, image]}
    ]


def test_convert_to_standard_tools_and_unknown_contract():
    from domain.chat.message_converter import convert_to_standard

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_call", "id": "call_1", "name": "lookup", "arguments": "{\"q\":\"x\"}"},
            ],
        },
        {
            "role": "tool",
            "content": [{"type": "tool_result", "tool_call_id": "call_1", "content": "done"}],
        },
        {"role": "user", "content": [{"type": "mystery", "value": 3}]},
    ]

    assert convert_to_standard(messages) == [
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        {"role": "user", "content": "{'type': 'mystery', 'value': 3}"},
    ]


def test_defaults_and_defaultspack_message_converter_match():
    from domain.chat.message_converter import convert_to_standard

    defaults_file = DEFAULTS_ROOT / "domain" / "chat" / "message_converter.py"
    assert not defaults_file.exists()
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert convert_to_standard(messages) == [{"role": "user", "content": "hello"}]

    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert_profile_resolver_requires_authority_snapshot()


def test_defaults_chat_send_legacy_converter_still_works():
    from domain.chat.message_converter import convert_to_standard

    assert not (DEFAULTS_ROOT / "domain" / "chat" / "message_converter.py").exists()
    assert convert_to_standard([{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]) == [
        {"role": "assistant", "content": "ok"}
    ]

    from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

    assert_profile_resolver_requires_authority_snapshot()
