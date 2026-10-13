"""Lossless provisional batching preserves real stream receipts and fences."""

from __future__ import annotations

from typing import Any

import pytest

from ecosystem.rumi_provider_adapters_pack.runtime.streaming import (
    DisplayProgressBuffer,
    ProviderStream,
)
from tobkiri_protocol.turn_progress_v1 import MAX_DELTA_BYTES, MAX_EVENTS


def _content(value: str) -> dict[str, Any]:
    return {"data": {"choices": [{"delta": {"content": value}}]}}


def _complete(stream: ProviderStream, reason: str = "stop") -> None:
    stream.receive({"data": {"choices": [{"finish_reason": reason}]}})
    stream.receive({"done": True})


def test_first_text_is_immediate_and_later_text_has_one_lossless_flush() -> None:
    published: list[dict[str, Any]] = []
    stream = ProviderStream("openai", published.append)
    stream.receive(_content("first"))
    assert published == [{"type": "text_delta", "delta": "first"}]
    for _ in range(1000):
        stream.receive(_content("あ"))
    assert len(published) == 1
    _complete(stream)
    assert published == [
        {"type": "text_delta", "delta": "first"},
        {"type": "text_delta", "delta": "あ" * 1000},
        {"type": "finish", "finish_reason": "stop"},
    ]
    result = stream.result()
    assert result["output"] == "first" + "あ" * 1000
    assert len(result["events"]) == 1003
    assert result["events"][1] == {"type": "text_delta", "delta": "あ"}


def test_utf8_threshold_and_aggregate_limit_do_not_split_received_deltas() -> None:
    published: list[dict[str, Any]] = []
    buffer = DisplayProgressBuffer(published.append)
    values = ["first", "あ" * 1000, "あ" * 1000, "z" * MAX_DELTA_BYTES]
    for value in values:
        buffer.publish({"type": "text_delta", "delta": value})
    assert [event["delta"] for event in published] == [
        "first",
        "あ" * 2000,
        "z" * MAX_DELTA_BYTES,
    ]
    assert all(len(event["delta"].encode()) <= MAX_DELTA_BYTES for event in published)


def test_pending_text_drains_before_maximum_sized_delta() -> None:
    published: list[dict[str, Any]] = []
    buffer = DisplayProgressBuffer(published.append)
    for value in ("first", "pending", "あ" * (MAX_DELTA_BYTES // 3)):
        buffer.publish({"type": "text_delta", "delta": value})
    assert [event["delta"] for event in published] == [
        "first",
        "pending",
        "あ" * (MAX_DELTA_BYTES // 3),
    ]


def test_empty_reasoning_state_is_folded_without_exposing_raw_reasoning() -> None:
    published: list[dict[str, Any]] = []
    stream = ProviderStream("openai", published.append)
    for _ in range(1000):
        stream.receive(
            {
                "data": {
                    "choices": [
                        {"delta": {"reasoning_content": "private reasoning"}},
                    ]
                }
            }
        )
    assert published == [{"type": "thinking_delta", "delta": ""}]
    stream.receive(_content("text"))
    stream.receive(_content(" pending"))
    stream.receive(
        {
            "data": {
                "choices": [
                    {"delta": {"reasoning_content": "more private reasoning"}},
                ]
            }
        }
    )
    _complete(stream)
    assert published == [
        {"type": "thinking_delta", "delta": ""},
        {"type": "text_delta", "delta": "text"},
        {"type": "text_delta", "delta": " pending"},
        {"type": "thinking_delta", "delta": ""},
        {"type": "finish", "finish_reason": "stop"},
    ]
    assert "private reasoning" not in str(stream.result())
    assert sum(event["type"] == "thinking_delta" for event in stream.events) == 1001


def test_two_tool_intents_and_usage_are_preserved_with_text_batching() -> None:
    published: list[dict[str, Any]] = []
    stream = ProviderStream("openai", published.append)
    stream.receive(_content("I"))
    stream.receive(_content(" will do both."))
    for index, name in enumerate(("coding_file_create", "python_sandbox_execute")):
        stream.receive(
            {
                "data": {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": f"call.{index}",
                                        "function": {
                                            "name": name,
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        )
    stream.receive({"data": {"choices": [], "usage": {"total_tokens": 40}}})
    _complete(stream, "tool_calls")
    result = stream.result()
    assert [tool["function"]["name"] for tool in result["tool_intents"]] == [
        "coding_file_create",
        "python_sandbox_execute",
    ]
    assert result["usage"] == {"total_tokens": 40}
    assert [event["type"] for event in result["events"]][-4:] == [
        "tool_intent_delta",
        "tool_intent_delta",
        "usage",
        "finish",
    ]
    assert published == [
        {"type": "text_delta", "delta": "I"},
        {"type": "text_delta", "delta": " will do both."},
        {"type": "finish", "finish_reason": "tool_calls"},
    ]


def test_unfinished_stream_does_not_flush_pending_text_or_invent_finish() -> None:
    published: list[dict[str, Any]] = []
    stream = ProviderStream("openai", published.append)
    stream.receive(_content("first"))
    stream.receive(_content(" incomplete"))
    with pytest.raises(ValueError, match="did not complete"):
        stream.result()
    assert published == [{"type": "text_delta", "delta": "first"}]


@pytest.mark.parametrize(
    "invalid",
    [
        {"type": "text_delta", "delta": "あ" * (MAX_DELTA_BYTES // 3 + 1)},
        {"type": "text_delta", "delta": "text", "approved": True},
    ],
)
def test_invalid_raw_event_is_rejected_before_buffering(invalid: dict[str, Any]) -> None:
    published: list[dict[str, Any]] = []
    buffer = DisplayProgressBuffer(published.append)
    with pytest.raises(ValueError):
        buffer.publish(invalid)
    with pytest.raises(ValueError, match="terminal"):
        buffer.publish({"type": "finish", "finish_reason": "stop"})
    assert not published


def test_ambiguous_publish_failure_cannot_flush_or_retry_later() -> None:
    attempted: list[dict[str, Any]] = []

    def publish(event: dict[str, Any]) -> None:
        attempted.append(dict(event))
        if len(attempted) == 2:
            raise RuntimeError("publication outcome unknown")

    buffer = DisplayProgressBuffer(publish)
    buffer.publish({"type": "text_delta", "delta": "first"})
    buffer.publish({"type": "text_delta", "delta": " pending"})
    with pytest.raises(RuntimeError, match="unknown"):
        buffer.publish({"type": "finish", "finish_reason": "stop"})
    with pytest.raises(ValueError, match="terminal"):
        buffer.publish({"type": "finish", "finish_reason": "stop"})
    assert attempted == [
        {"type": "text_delta", "delta": "first"},
        {"type": "text_delta", "delta": " pending"},
    ]


def test_raw_event_limit_is_not_relaxed_by_fewer_display_publications() -> None:
    published: list[dict[str, Any]] = []
    stream = ProviderStream("openai", published.append)
    for _ in range(MAX_EVENTS):
        stream.receive(_content("x"))
    with pytest.raises(ValueError, match="exceeds its limit"):
        stream.receive(_content("x"))
    assert len(stream.events) == MAX_EVENTS
    assert published == [{"type": "text_delta", "delta": "x"}]
