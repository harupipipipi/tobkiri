"""Neutral immutable child slots and fresh public conversation context lineage."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Callable

from .canonical import canonical_digest, strict_loads, canonical_json
from .saved_tools import validate_tool_selection

LINK_VERSION = "tobkiri.conversation-context-link.v1"
BINDING_VERSION = "tobkiri.conversation-context-binding.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_CONTEXT_FIELDS = (
    "model_reference",
    "system_prompt_id",
    "agent_id",
    "conversation_kind",
    "group_id",
    "tags",
)
_METADATA_FIELDS = (
    "workspace_id",
    "workspaceId",
    "workspace_root",
    "workspaceRoot",
    "rootPath",
    "rumi_data_path",
    "rumiDataPath",
    "rumi_dp_path",
    "group_id",
    "groupId",
    "mode",
    "profile_id",
    "shared_read_only",
    "tool_selection",
    "strategy_reference",
    "thinking_level",
    "approval_policy_reference",
)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("conversation context identity is invalid")
    return value


def context_link(conversation: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate an owner-persisted immutable parent and unique child slot."""
    link = conversation.get("context_link")
    if link is None:
        return None
    if (
        not isinstance(link, Mapping)
        or set(link)
        != {"version", "parent_conversation_id", "slot", "created_from_parent_revision"}
        or link.get("version") != LINK_VERSION
        or type(link.get("created_from_parent_revision")) is not int
        or link["created_from_parent_revision"] < 1
    ):
        raise ValueError("conversation context link is invalid")
    _identifier(link["parent_conversation_id"])
    _identifier(link["slot"])
    if conversation.get("parent_conversation_id") != link["parent_conversation_id"]:
        raise ValueError("conversation context parent linkage differs")
    return dict(link)


def validate_context_binding(value: Any) -> dict[str, Any]:
    """Validate a revision/digest assertion; it grants no execution authority."""
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "version",
            "profile_id",
            "parent_conversation_id",
            "parent_revision",
            "context_digest",
        }
        or value.get("version") != BINDING_VERSION
        or type(value.get("parent_revision")) is not int
        or value["parent_revision"] < 1
        or not isinstance(value.get("context_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["context_digest"]) is None
    ):
        raise ValueError("conversation context binding is invalid")
    _identifier(value["profile_id"])
    _identifier(value["parent_conversation_id"])
    return dict(value)


def inherited_context(parent: Mapping[str, Any]) -> dict[str, Any]:
    """Project context references without history or caller approval flags."""
    metadata = parent.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("parent context metadata is invalid")
    result = {key: parent.get(key) for key in _CONTEXT_FIELDS}
    result["metadata"] = {
        key: metadata[key] for key in _METADATA_FIELDS if key in metadata
    }
    return strict_loads(canonical_json(result))


def context_binding(parent: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
    """Bind a fresh public parent projection to its captured Profile/revision."""
    profile_id = _identifier(profile_id)
    parent_id = _identifier(parent.get("id"))
    revision = parent.get("conversation_revision")
    if type(revision) is not int or revision < 1 or context_link(parent) is not None:
        raise ValueError("parent context cannot be resolved")
    context = inherited_context(parent)
    if context["metadata"].get("profile_id") not in (None, "", profile_id):
        raise ValueError("parent context selects another Profile")
    return {
        "version": BINDING_VERSION,
        "profile_id": profile_id,
        "parent_conversation_id": parent_id,
        "parent_revision": revision,
        "context_digest": canonical_digest(
            {"profile_id": profile_id, "context": context}
        ),
    }


def resolve_linked_conversation(
    child: Mapping[str, Any],
    parent: Mapping[str, Any],
    binding: Any,
    profile_id: str,
) -> dict[str, Any]:
    """Resolve only an exact fresh parent; never merge parent message history."""
    link = context_link(child)
    expected = validate_context_binding(binding)
    actual = context_binding(parent, profile_id)
    if (
        link is None
        or parent.get("id") != link["parent_conversation_id"]
        or parent.get("id") == child.get("id")
        or expected != actual
    ):
        raise ValueError("conversation parent context is stale or unavailable")
    if any(
        child.get(key)
        for key in ("model_reference", "system_prompt_id", "agent_id", "group_id")
    ):
        raise ValueError("linked conversation cannot override parent context")
    metadata = child.get("metadata") or {}
    if not isinstance(metadata, Mapping) or any(
        key in metadata for key in _METADATA_FIELDS
    ):
        raise ValueError("linked conversation cannot override context policy")
    resolved = strict_loads(canonical_json(dict(child)))
    inherited = inherited_context(parent)
    resolved.update({key: inherited[key] for key in _CONTEXT_FIELDS})
    resolved["metadata"] = {**metadata, **inherited["metadata"]}
    return resolved


def resolve_request_context(
    conversation: Mapping[str, Any],
    request: Mapping[str, Any],
    profile_id: str,
    read_parent: Callable[[str], Mapping[str, Any] | None],
) -> Mapping[str, Any]:
    """Resolve one selected public parent read or reject an unbound child turn."""
    link = context_link(conversation)
    if link is None:
        if "context_binding" in request:
            raise ValueError("unlinked conversation cannot select a parent context")
        return conversation
    parent = read_parent(link["parent_conversation_id"])
    if not isinstance(parent, Mapping):
        raise ValueError("conversation parent context is unavailable")
    options = inherited_turn_options(parent)
    requested = validate_tool_selection(request.get("tool_selection", {"mode": "none"}))
    if requested["mode"] != "none" and _tool_scope(requested) != _tool_scope(
        options["tool_selection"]
    ):
        raise ValueError("linked turn cannot expand parent tool selection")
    if request.get("strategy_reference") != options.get("strategy_reference"):
        raise ValueError("linked turn cannot change parent execution strategy")
    return resolve_linked_conversation(
        conversation,
        parent,
        request.get("context_binding"),
        profile_id,
    )


def inherited_turn_options(parent: Mapping[str, Any]) -> dict[str, Any]:
    """Read bounded parent options; stored approval flags never authorize actions."""
    metadata = parent.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("parent options are invalid")
    options: dict[str, Any] = {
        "tool_selection": validate_tool_selection(
            metadata.get("tool_selection", {"mode": "none"})
        )
    }
    if metadata.get("strategy_reference") is not None:
        options["strategy_reference"] = _identifier(metadata["strategy_reference"])
    if metadata.get("thinking_level") is not None:
        level = metadata["thinking_level"]
        if level not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("parent reasoning level is invalid")
        options["thinking_level"] = level
    return options


def _tool_scope(value: Mapping[str, Any]) -> bytes:
    return canonical_json(
        {
            "mode": value["mode"],
            "scope": value.get("scope", "turn"),
            "include": value.get("include", []),
            "exclude": value.get("exclude", []),
            "must_use": value.get("must_use", False),
        }
    )
