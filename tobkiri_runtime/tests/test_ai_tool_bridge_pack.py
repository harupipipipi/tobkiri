"""External-QA-oriented AI tool bridge specifications."""

from __future__ import annotations

from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    create_tool_intent_operation,
)


def test_provider_tool_call_becomes_unapproved_nonexecuting_descriptor() -> None:
    result = create_tool_intent_operation(None)(
        "normalize",
        {
            "request_id": "request-1",
            "intents": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "workspace.write",
                        "arguments": "{\"path\":\"a.txt\"}",
                    },
                }
            ],
        },
    )

    intent = result["intents"][0]
    assert intent["operation"] == "workspace.write"
    assert intent["arguments"] == {"path": "a.txt"}
    assert intent["authority_granted"] is False
    assert intent["approved"] is False
    assert intent["executes"] is False

