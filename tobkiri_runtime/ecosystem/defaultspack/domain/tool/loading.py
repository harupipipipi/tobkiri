from __future__ import annotations

from typing import Any


TOOL_LOADING_ALWAYS = "always"
TOOL_LOADING_VECTOR = "vector"
TOOL_LOADING_MODES = {TOOL_LOADING_ALWAYS, TOOL_LOADING_VECTOR}

_ALWAYS_ALIASES = {"always", "eager", "preload", "preloaded", "resident", "startup"}


def normalize_tool_loading_mode(value: Any) -> str:
    """Normalize a tool-declared loading strategy.

    Tools may opt into always being exposed to the model. Everything else,
    including missing or legacy values, stays vector-selected by default.
    """

    if isinstance(value, dict):
        value = value.get("mode") or value.get("strategy") or value.get("type")
    mode = str(value or "").strip().lower().replace("-", "_")
    if mode in _ALWAYS_ALIASES:
        return TOOL_LOADING_ALWAYS
    return TOOL_LOADING_VECTOR


def tool_loading_mode(tool: dict[str, Any] | None) -> str:
    if not isinstance(tool, dict):
        return TOOL_LOADING_VECTOR
    raw_metadata = tool.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    return normalize_tool_loading_mode(tool.get("loading") or metadata.get("loading"))


def split_tools_by_loading(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    always: list[dict[str, Any]] = []
    vector: list[dict[str, Any]] = []
    for tool in tools:
        if tool_loading_mode(tool) == TOOL_LOADING_ALWAYS:
            always.append(tool)
        else:
            vector.append(tool)
    return always, vector
