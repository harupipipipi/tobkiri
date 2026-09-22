"""Exercise reasoning normalization through the production SSE provider."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        ({"trace": "trace only"}, "trace only"),
        ({"reasoning_content": "preferred", "trace": "ignored"}, "preferred"),
        ({"reasoning": "reason", "trace": "ignored"}, "reason"),
        ({"thinking": "thought", "trace": "ignored"}, "thought"),
        ({"reasoning_content": "", "trace": "fallback"}, "fallback"),
    ],
)
def test_production_stream_separates_reasoning_from_content(
    monkeypatch: pytest.MonkeyPatch, delta: dict[str, str], expected: str
) -> None:
    """Trace must reach live reasoning events without entering answer text."""
    from domain.ai_client.providers.openai_provider import OpenAIProvider

    payload = {"choices": [{"delta": {"content": "answer", **delta}}]}
    end = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    response = io.BytesIO(
        f"data: {json.dumps(payload)}\n\ndata: {json.dumps(end)}\n\n"
        "data: [DONE]\n\n".encode()
    )
    provider = OpenAIProvider()
    monkeypatch.setattr(provider, "_request_stream", lambda *args, **kwargs: response)

    events = list(provider.stream("m", [], [], {}))

    assert [event["type"] for event in events] == [
        "content_delta", "reasoning_delta", "stream_end"
    ]
    assert events[0]["delta"] == {"type": "text", "text": "answer"}
    assert events[1]["delta"] == {"type": "text", "text": expected}
    assert response.closed
