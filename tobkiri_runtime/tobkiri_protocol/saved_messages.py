"""Pure owner-context message assembly shared by saved chat and Flow nodes.

No storage, network, Provider call, approval or grant is available here.
Caller-owned history must already have been read through its captured owner.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import canonical_json
from .saved_context import saved_prompt_reference
from .saved_conversation import (
    MAX_SAVED_IMAGE_COUNT, SavedWorkspaceResolution, is_saved_user_content, saved_user_text,
    validate_saved_conversation_context,
)
from .conversation_lifecycle import active_task_gap_context, task_gap_prompt
from .saved_tools import saved_tool_logs, saved_tool_messages


class SavedMessageContextError(ValueError):
    """A saved owner context cannot be represented without resolving more data."""


def _require_resolved_context(
    conversation: Mapping[str, Any],
    workspace_resolution: SavedWorkspaceResolution | None,
) -> None:
    try:
        validate_saved_conversation_context(
            conversation, workspace_resolution=workspace_resolution,
        )
    except ValueError as error:
        raise SavedMessageContextError(str(error)) from error


def build_saved_model_messages(
    conversation: Mapping[str, Any],
    *,
    flatten_text_blocks: bool = False,
    system_prompt: Mapping[str, str] | None = None,
    workspace_resolution: SavedWorkspaceResolution | None = None,
) -> list[dict[str, Any]]:
    """Independently constrain the selected owner history to resolved text."""
    # This optional capability is passed directly by the Host. It must never
    # be reconstructed from a serialized Flow node payload.
    _require_resolved_context(conversation, workspace_resolution)
    prompt_id = saved_prompt_reference(conversation)
    if (system_prompt is None) != (prompt_id is None) or (
        system_prompt is not None and system_prompt["prompt_id"] != prompt_id
    ):
        raise SavedMessageContextError("saved bridge system prompt is unresolved")
    prefix = (
        [{"role": "system", "content": system_prompt["body"]}]
        if system_prompt is not None and system_prompt["body"]
        else []
    )
    task_gap = active_task_gap_context(conversation)
    if task_gap is not None:
        prefix.append({"role": "system", "content": task_gap_prompt(task_gap)})
    messages = conversation.get("messages")
    if not isinstance(messages, list) or len(messages) > 200:
        raise SavedMessageContextError("saved bridge owner history is invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, Mapping):
            raise SavedMessageContextError("saved bridge owner message is invalid")
        identifier = message.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise SavedMessageContextError("saved bridge owner message identity is invalid")
        by_id[identifier] = message
    current = conversation.get("current_node_id")
    if not messages:
        if current is not None:
            raise SavedMessageContextError("saved bridge empty history has a current node")
        return prefix
    if not isinstance(current, str) or current not in by_id:
        raise SavedMessageContextError("saved bridge current message is unavailable")
    if all(message.get("parent_id") is None for message in messages):
        selected = messages[: messages.index(by_id[current]) + 1]
    else:
        selected = []
        seen: set[str] = set()
        node: object = current
        while node is not None:
            if not isinstance(node, str) or node not in by_id or node in seen:
                raise SavedMessageContextError("saved bridge history is cyclic or incomplete")
            seen.add(node)
            selected.append(by_id[node])
            node = by_id[node].get("parent_id")
        selected.reverse()
    result: list[dict[str, Any]] = list(prefix)
    for message in selected:
        content = message.get("content")
        text_only = isinstance(content, str) or (
            isinstance(content, list)
            and bool(content)
            and all(
                isinstance(part, Mapping)
                and set(part) == {"type", "text"}
                and part["type"] == "text"
                and isinstance(part["text"], str)
                for part in content
            )
        )
        bounded_user_content = (
            message.get("role") == "user" and is_saved_user_content(content)
        )
        if (
            message.get("role") not in {"system", "user", "assistant"}
            or message.get("status") != "complete"
            or not (text_only or bounded_user_content)
            or message.get("parts")
            or message.get("widget")
        ):
            raise SavedMessageContextError("saved bridge additional content resolution is required")
        metadata = message.get("metadata") or {}
        trace = saved_tool_messages(metadata.get("saved_tool_messages", []))
        logs = message.get("tool_logs")
        if (trace and message["role"] != "assistant") or canonical_json(
            [] if logs is None else logs
        ) != canonical_json(saved_tool_logs(trace)):
            raise SavedMessageContextError("saved bridge owned tool transcript is invalid")
        if flatten_text_blocks:
            # Readiness accepts text-only messages. Preserve every tool argument
            # and result as text there; generation receives the exact structures.
            result.extend(
                {"role": "assistant", "content": canonical_json(item).decode()} for item in trace
            )
        else:
            result.extend(trace)
        # Retain the owner's exact text blocks for guest equality checks and
        # generation. Only the text-only readiness API receives joined text.
        if flatten_text_blocks and bounded_user_content:
            content = saved_user_text(content)
        elif flatten_text_blocks and isinstance(content, list):
            content = "".join(part["text"] for part in content)
        result.append({"role": message["role"], "content": content})
    if flatten_text_blocks:
        return result
    return _bounded_inline_image_history(result)


def _bounded_inline_image_history(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only the newest bounded saved image set in owner generation context."""
    images: list[tuple[int, int]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, list):
            images.extend(
                (message_index, block_index)
                for block_index, block in enumerate(content)
                if isinstance(block, Mapping) and block.get("type") == "image_url"
            )
    retained = set(images[-MAX_SAVED_IMAGE_COUNT:])
    if len(retained) == len(images):
        return messages
    bounded: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            bounded.append(message)
            continue
        omitted = 0
        blocks: list[dict[str, Any]] = []
        for block_index, block in enumerate(content):
            if (
                isinstance(block, Mapping)
                and block.get("type") == "image_url"
                and (message_index, block_index) not in retained
            ):
                omitted += 1
                continue
            blocks.append(dict(block))
        if omitted:
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        "[Earlier inline image omitted from this saved-turn context "
                        "because of the bounded image limit.]"
                    ),
                }
            )
        bounded.append({**message, "content": blocks})
    return bounded
