"""Expose the captured conversation store to the Defaults history list."""

from __future__ import annotations

from typing import Mapping


CONVERSATION_LIST_TARGET = (
    "defaults.conversations.list",
    "tobkiri.resource.conversation.v1",
    "rumi_conversation_store_pack.conversation-resource",
    "rumi_conversation_store_pack.conversation-store.resource",
    "rumi_conversation_store_pack.conversation-store.resource",
)


def present_conversation_list(result: Mapping[str, object]) -> dict[str, object]:
    """Return real records while keeping owner revision and migration private."""
    if result.get("state") == "error":
        return dict(result)
    conversations = result.get("conversations")
    if not isinstance(conversations, list) or any(
        not isinstance(item, Mapping) or not isinstance(item.get("id"), str)
        for item in conversations
    ):
        raise ValueError("conversation owner returned an invalid list")
    return {"conversations": conversations, "total": len(conversations)}
