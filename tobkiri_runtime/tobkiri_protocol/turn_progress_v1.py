"""Finite provisional Turn progress, separate from canonical saved receipts."""

from __future__ import annotations

import re
import json
import hashlib
from typing import Any, Mapping

ACTION = "tobkiri.action.turn.progress.v1"
RESOURCE = "tobkiri.resource.turn.progress.v1"
ACTION_OPERATION = "rumi_turn_runtime_pack.turn-progress"
RESOURCE_OPERATION = "rumi_turn_runtime_pack.turn-progress-resource"
AI_STREAM = ("tobkiri.service.ai.stream.v1", "rumi_ai_gateway_pack.ai-gateway.stream")
PROVIDER_STREAM = "tobkiri.service.ai.provider.stream.v1"
VERSION = "tobkiri.turn-progress.v1"
MAX_EVENTS = 4096
MAX_BYTES = 4 * 1024 * 1024
MAX_DELTA_BYTES = 16 * 1024
TTL_SECONDS = 120
BEGIN_FIELDS = frozenset(
    {
        "turn_id",
        "conversation_id",
        "conversation_revision",
        "parent_id",
        "input_digest",
        "request_id",
        "ai_input_digest",
    }
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def payload_digest(value: Mapping[str, Any]) -> str:
    """Bind exact finite JSON provider input, including supported sampling numbers."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("progress producer input exceeds limit")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_begin(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only the immutable Host-derived saved input and branch bindings."""
    if set(value) != BEGIN_FIELDS:
        raise ValueError("progress begin fields are invalid")
    for key in ("turn_id", "conversation_id", "parent_id", "request_id"):
        if not isinstance(value[key], str) or not _ID.fullmatch(value[key]):
            raise ValueError("progress identity is invalid")
    for key in ("input_digest", "ai_input_digest"):
        if not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key]):
            raise ValueError("progress digest is invalid")
    if type(value["conversation_revision"]) is not int or value["conversation_revision"] < 0:
        raise ValueError("progress revision is invalid")
    return dict(value)


def validate_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Bound display-only text and terminal provider status; never accept authority."""
    kind = value.get("type")
    if kind in {"text_delta", "thinking_delta"}:
        if set(value) != {"type", "delta"} or not isinstance(value["delta"], str):
            raise ValueError("progress delta is invalid")
        if len(value["delta"].encode("utf-8")) > MAX_DELTA_BYTES:
            raise ValueError("progress delta exceeds its limit")
    elif kind == "finish":
        if set(value) != {"type", "finish_reason"} or value["finish_reason"] not in {
            "stop",
            "length",
            "tool_calls",
            "end_turn",
            "max_tokens",
            "tool_use",
        }:
            raise ValueError("progress finish is invalid")
    else:
        raise ValueError("progress event type is invalid")
    return dict(value)
