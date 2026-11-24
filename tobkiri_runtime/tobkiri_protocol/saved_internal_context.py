"""Typed timing-context output checks; caller data never establishes owner trust."""

from __future__ import annotations

from typing import Any, Mapping

from .conversation_lifecycle import active_task_gap_context
from .canonical import canonical_json

CONTEXT_VERSION = "io.tobkiri.saved-internal-context.v1"


def validate_timing_output(
    output: Mapping[str, Any],
    *,
    profile_id: str,
    conversation: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Bind data to the already authenticated owner's exact current user receipt.

    Callers must obtain ``conversation`` through the captured owner Contract;
    this function does not authenticate mappings or authorize Workflow execution.
    """
    lifecycle = conversation.get("lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    timing = active_task_gap_context(conversation)
    expected = {
        "internal_context_api_version": CONTEXT_VERSION,
        "profile_id": profile_id,
        "conversation_id": conversation.get("id"),
        "conversation_revision": conversation.get("conversation_revision"),
        "active_user_message_id": lifecycle.get("active_user_message_id"),
        "context": timing,
    }
    if canonical_json(dict(output)) != canonical_json(expected):
        raise ValueError("selected internal context differs from the captured owner")
    return timing
