"""Validate the public saved-turn receipt and its owned assistant answer."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

from .ports import _VoiceFailure, _digest, _identifier, _required_text


def _message_id(conversation_id: str, turn_id: str, role: str) -> str:
    return "message:" + canonical_digest([conversation_id, turn_id, role]).removeprefix("sha256:")


def _valid_reference(value: Any, conversation_id: str, revision: int) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("conversation_id") == conversation_id
        and value.get("conversation_revision") == revision + 2
        and type(value.get("conversation_revision")) is int
        and _identifier(value.get("assistant_message_id"))
        and _identifier(value.get("user_message_id"))
        and _digest(value.get("outcome_digest"))
    )


def _completed_reply(value: Any, reference: Mapping[str, Any], turn_id: str) -> str:
    conversation = value.get("conversation") if isinstance(value, Mapping) else None
    if (
        not isinstance(conversation, Mapping)
        or conversation.get("id") != reference["conversation_id"]
        or type(conversation.get("conversation_revision")) is not int
        or conversation["conversation_revision"] < reference["conversation_revision"]
        or reference["assistant_message_id"]
        != _message_id(reference["conversation_id"], turn_id, "assistant")
        or reference["user_message_id"]
        != _message_id(reference["conversation_id"], turn_id, "user")
    ):
        raise _VoiceFailure("voice_answer_unconfirmed")
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        raise _VoiceFailure("voice_answer_unconfirmed")
    matches = [
        message
        for message in messages
        if isinstance(message, Mapping) and message.get("id") == reference["assistant_message_id"]
    ]
    if len(matches) != 1:
        raise _VoiceFailure("voice_answer_unconfirmed")
    message = matches[0]
    metadata = message.get("metadata")
    if (
        message.get("role") != "assistant"
        or message.get("status") != "complete"
        or message.get("parent_id") != reference["user_message_id"]
        or not isinstance(metadata, Mapping)
        or metadata.get("turn_id") != turn_id
    ):
        raise _VoiceFailure("voice_answer_unconfirmed")
    content = message.get("content")
    if isinstance(content, list) and all(
        isinstance(part, Mapping)
        and set(part) == {"type", "text"}
        and part["type"] == "text"
        and isinstance(part["text"], str)
        for part in content
    ):
        content = "\n".join(part["text"] for part in content)
    return _required_text(content)
