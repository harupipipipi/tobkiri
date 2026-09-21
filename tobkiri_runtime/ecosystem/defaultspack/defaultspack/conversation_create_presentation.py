"""Translate full-UI creation without choosing identity or revision at replay."""

from __future__ import annotations

import uuid
from typing import Mapping

CONVERSATION_CREATE_TARGET = (
    "defaults.conversations.create",
    "tobkiri.action.conversation.manage.v1",
    "rumi_conversation_store_pack.conversation-manage",
    "rumi_conversation_store_pack.conversation-store.manage",
    "rumi_conversation_store_pack.conversation-store.manage",
)
_OPTION_FIELDS = {
    "model",
    "system_prompt_id",
    "agent_id",
    "tags",
    "parent_conversation_id",
    "conversation_kind",
    "group_id",
    "metadata",
}


def normalize_conversation_create(
    payload: Mapping[str, object],
    *,
    profile_id: str,
) -> dict[str, object]:
    """Require a stable client UUID and explicit owner snapshot revision."""
    required = {"id", "expected_revision"}
    if not profile_id or not required <= set(payload) or set(payload) - required - _OPTION_FIELDS:
        raise ValueError("conversation create fields are invalid")
    identifier = payload["id"]
    try:
        valid_id = isinstance(identifier, str) and str(uuid.UUID(identifier)) == identifier
    except ValueError:
        valid_id = False
    revision = payload["expected_revision"]
    if not valid_id or type(revision) is not int or revision < 0:
        raise ValueError("conversation create identity or revision is invalid")
    for key in _OPTION_FIELDS - {"tags", "metadata"}:
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            raise ValueError("conversation option must be text or null")
    if "tags" in payload and (
        not isinstance(payload["tags"], list)
        or any(not isinstance(tag, str) for tag in payload["tags"])
    ):
        raise ValueError("conversation tags must be text entries")
    if "metadata" in payload and not isinstance(payload["metadata"], Mapping):
        raise ValueError("conversation metadata must be an object")
    return {
        "profile_id": profile_id,
        "operation": "create",
        "expected_revision": revision,
        "conversation": {
            key: value for key, value in payload.items() if key != "expected_revision"
        },
    }


def present_conversation_created(result: Mapping[str, object]) -> dict[str, object]:
    """Expose the actual created record in the existing ChatApp response shape."""
    if result.get("state") == "error":
        return dict(result)
    record = result.get("conversation")
    if result.get("action") != "created" or not isinstance(record, Mapping):
        raise ValueError("conversation owner did not return a created record")
    return {**record, "model": record.get("model_reference", "")}
