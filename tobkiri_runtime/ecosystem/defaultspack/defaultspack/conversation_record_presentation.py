"""Fixed-route conversation reads and revision-bound metadata mutations."""

from __future__ import annotations

from typing import Mapping

from .conversation_list_presentation import CONVERSATION_LIST_TARGET
from .conversation_create_presentation import CONVERSATION_CREATE_TARGET

CONVERSATION_RECORD_TARGETS = {
    (f"defaults.conversations.{action}", *source[1:]): action
    for action, source in (
        ("get", CONVERSATION_LIST_TARGET),
        ("update", CONVERSATION_CREATE_TARGET),
        ("delete", CONVERSATION_CREATE_TARGET),
    )
}
_PATCH_FIELDS = {
    "title",
    "model",
    "system_prompt_id",
    "agent_id",
    "tags",
    "is_starred",
    "is_pinned",
    "pinned_at",
    "pin_scope",
    "is_archived",
    "current_node_id",
    "parent_conversation_id",
    "conversation_kind",
    "group_id",
    "metadata",
}


def normalize_conversation_record(
    action: str,
    payload: Mapping[str, object],
    *,
    profile_id: str,
) -> dict[str, object]:
    """Capture Profile identity and reject implicit revisions or writable IDs."""
    fields = {"conversation_id"}
    if action in {"update", "delete"}:
        fields.add("expected_conversation_revision")
    if action == "update":
        fields.add("updates")
    if action not in {"get", "update", "delete"} or set(payload) != fields or not profile_id:
        raise ValueError("conversation record fields are invalid")
    identifier = payload["conversation_id"]
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("conversation identity is required")
    normalized = {"profile_id": profile_id, "operation": action, **payload}
    if action != "get":
        revision = payload["expected_conversation_revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("conversation revision must be an exact positive integer")
    if action == "update":
        patch = payload["updates"]
        if not isinstance(patch, Mapping) or not patch or set(patch) - _PATCH_FIELDS:
            raise ValueError("conversation patch fields are invalid")
        for key, value in patch.items():
            if key in {"is_starred", "is_pinned", "is_archived"}:
                valid = type(value) is bool
            elif key == "tags":
                valid = isinstance(value, list) and all(isinstance(tag, str) for tag in value)
            elif key == "metadata":
                valid = isinstance(value, Mapping)
            elif key == "pinned_at":
                valid = value is None or (type(value) is int and value >= 0)
            else:
                valid = isinstance(value, str) or (
                    value is None
                    and key
                    in {
                        "system_prompt_id",
                        "agent_id",
                        "current_node_id",
                        "parent_conversation_id",
                        "group_id",
                    }
                )
            if not valid:
                raise ValueError("conversation patch value is invalid")
        normalized.pop("updates")
        normalized["patch"] = {
            "model_reference" if key == "model" else key: value for key, value in patch.items()
        }
    return normalized


def present_conversation_record(result: Mapping[str, object]) -> dict[str, object]:
    """Return an owned record with the existing UI model alias."""
    if result.get("state") == "error":
        return dict(result)
    record = result.get("conversation")
    if not isinstance(record, Mapping) or not isinstance(record.get("id"), str):
        raise ValueError("conversation owner did not return a record")
    return {**record, "model": record.get("model_reference", "")}


def present_conversation_deleted(result: Mapping[str, object]) -> dict[str, object]:
    """Only an actual owner deletion is presented as success."""
    if result.get("state") == "error":
        return dict(result)
    if result.get("action") != "deleted":
        raise ValueError("conversation owner did not confirm deletion")
    return {"deleted": True}
