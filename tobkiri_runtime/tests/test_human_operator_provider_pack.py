"""External-QA-oriented human operator provider specifications."""

from __future__ import annotations

from ecosystem.rumi_human_operator_provider_pack.runtime.provider import (
    create_generate_operation,
)


def test_human_provider_emits_unapproved_identifier_only_intent() -> None:
    result = create_generate_operation(None)(
        "generate",
        {
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "model_id": "command-canvas",
            "messages": [{"role": "user", "content": "private"}],
        },
    )

    intent = result["tool_intents"][0]
    assert intent["name"] == "rumi.human.handoff.request"
    assert intent["approval_required"] is True
    assert intent["authority_granted"] is False
    assert "private" not in str(intent)
    assert result["finish_reason"] == "human_handoff_required"

