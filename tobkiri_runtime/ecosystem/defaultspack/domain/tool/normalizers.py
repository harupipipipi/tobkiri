"""Small JSON-boundary normalizers with no higher-level tool dependencies."""

from typing import Any


def mapping_or_empty(value: Any) -> dict[str, Any]:
    """Normalize an untrusted JSON value to an object."""
    return value if isinstance(value, dict) else {}


def list_or_empty(value: Any) -> list[Any]:
    """Normalize an untrusted JSON value to a list."""
    return value if isinstance(value, list) else []


def tool_name_from_definition(tool: Any) -> str:
    """Read a tool identity from finite definition shapes without policy access."""
    if isinstance(tool, str):
        return tool
    if not isinstance(tool, dict):
        return ""
    function_def = tool.get("function")
    if isinstance(function_def, dict) and function_def.get("name"):
        return str(function_def.get("name"))
    return str(tool.get("tool_id") or tool.get("name") or "")
