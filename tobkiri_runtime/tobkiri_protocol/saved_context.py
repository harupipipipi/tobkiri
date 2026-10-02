"""Finite saved-turn prompt data checks, without storage access or authority."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .canonical import canonical_digest, canonical_json

PROMPT_TARGET = (
    "tobkiri.resource.prompt.studio.v1",
    "rumi_prompt_studio_pack.prompt-studio-resource",
)
_PROMPT_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,255}\Z")


def saved_prompt_reference(conversation: Mapping[str, Any]) -> str | None:
    """Return an explicit authored prompt identity; reject malformed references."""
    value = conversation.get("system_prompt_id")
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _PROMPT_ID.fullmatch(value) is None:
        raise ValueError("saved conversation prompt reference is invalid")
    return value


def resolved_saved_prompt(
    value: Mapping[str, Any],
    *,
    prompt_id: str,
    profile_id: str,
) -> dict[str, str]:
    """Constrain one authenticated owner's result to its enabled text and digest."""
    if not isinstance(value, Mapping):
        raise ValueError("saved conversation prompt owner response is invalid")
    prompt = value.get("prompt")
    if (
        value.get("profile_id") != profile_id
        or not isinstance(prompt, Mapping)
        or prompt.get("prompt_id") != prompt_id
        or prompt.get("enabled") is not True
        or not isinstance(prompt.get("body"), str)
    ):
        raise ValueError("saved conversation prompt is unavailable")
    body = prompt["body"]
    digest = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    if prompt.get("body_hash") != digest:
        raise ValueError("saved conversation prompt digest is invalid")
    result = {"prompt_id": prompt_id, "body": body, "body_hash": digest}
    if len(canonical_json(result)) > 60 * 1024:
        raise ValueError("saved conversation prompt exceeds the byte limit")
    return result


def saved_prompt_digest(prompt: Mapping[str, str] | None) -> str | None:
    """Bind the selected identity and body without duplicating prompt text."""
    if prompt is None:
        return None
    return canonical_digest({key: prompt[key] for key in ("prompt_id", "body_hash")})
