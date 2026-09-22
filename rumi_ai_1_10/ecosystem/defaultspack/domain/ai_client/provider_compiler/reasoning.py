"""Normalize reasoning fields shared by OpenAI-compatible stream consumers."""

from collections.abc import Mapping
from typing import Any


def reasoning_text_from_delta(delta: Mapping[str, Any]) -> str:
    """Return the first populated reasoning field, preserving provider priority."""
    for key in ("reasoning_content", "reasoning", "thinking", "trace"):
        value = delta.get(key)
        if value:
            return str(value)
    return ""
