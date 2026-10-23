"""External-QA-oriented specifications for global stream normalization."""

from __future__ import annotations

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_stream_pack.runtime.normalizer import (
    create_stream_normalize_operation,
)


def test_stream_types_sequence_and_request_binding_remain_distinct() -> None:
    result = create_stream_normalize_operation(None)(
        "normalize",
        {
            "request_id": "request-1",
            "provider_attempt": 2,
            "value": {
                "events": [
                    {"type": "thinking_delta", "delta": "private"},
                    {"type": "text_delta", "delta": "hello"},
                    {"type": "finish", "finish_reason": "stop"},
                ]
            },
        },
    )

    assert [item["type"] for item in result["events"]] == [
        "thinking_delta",
        "text_delta",
        "finish",
    ]
    assert [item["sequence"] for item in result["events"]] == [0, 1, 2]
    assert all(item["request_id"] == "request-1" for item in result["events"])
    assert all(item["provider_attempt"] == 2 for item in result["events"])


def test_stream_requires_terminal_event() -> None:
    with pytest.raises(GlobalContractInvocationError) as captured:
        create_stream_normalize_operation(None)(
            "normalize",
            {"request_id": "request-1", "value": [{"type": "text_delta"}]},
        )

    assert captured.value.code == "invalid_response"

