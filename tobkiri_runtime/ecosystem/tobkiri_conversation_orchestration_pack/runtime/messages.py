"""Pure Flow stage: resolved conversation data becomes model message data.

This operation neither reads owners nor authorizes generation or persistence.
Saved-chat callers must verify the producing owner's evidence separately.
Tool transcripts and the newest two inline images retain their structured
shapes; older images receive omission markers. Downstream model nodes must
support those actual shapes rather than silently treating them as text-only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_json, strict_loads
from tobkiri_protocol.saved_messages import build_saved_model_messages

OPERATION_ID = "messages_build"
MAX_BYTES = 512 * 1024


def tobkiri_packvm_invoke(
    operation_id: str, payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble a bounded, detached message list without any external effects."""
    if operation_id != OPERATION_ID:
        raise ValueError("conversation message operation is invalid")
    if not isinstance(payload, Mapping) or set(payload) != {
        "conversation", "system_prompt",
    }:
        raise ValueError("conversation message fields are invalid")
    checked = strict_loads(canonical_json(dict(payload)), max_bytes=MAX_BYTES, max_depth=32)
    conversation = checked["conversation"]
    prompt = checked["system_prompt"]
    if not isinstance(conversation, dict) or (
        conversation.get("metadata") is not None
        and not isinstance(conversation["metadata"], dict)
    ):
        raise ValueError("resolved conversation is required")
    if prompt is not None:
        if (
            not isinstance(prompt, dict)
            or set(prompt) != {"prompt_id", "body", "body_hash"}
            or not all(isinstance(value, str) for value in prompt.values())
            or prompt["body_hash"] != "sha256:" + hashlib.sha256(
                prompt["body"].encode("utf-8"),
            ).hexdigest()
            or len(canonical_json(prompt)) > 60 * 1024
        ):
            raise ValueError("resolved system prompt is invalid")
    messages = conversation.get("messages")
    if not isinstance(messages, list) or any(
        not isinstance(message, dict)
        or not isinstance(message.get("role"), str)
        or (message.get("metadata") is not None
            and not isinstance(message["metadata"], dict))
        for message in messages
    ):
        raise ValueError("resolved conversation messages are invalid")
    result = {"messages": build_saved_model_messages(conversation, system_prompt=prompt)}
    if len(canonical_json(result)) > MAX_BYTES:
        raise ValueError("assembled messages exceed the byte limit")
    return result
