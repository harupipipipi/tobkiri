"""Pure UI metadata projection shared by canonical and legacy consumers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


def project_tool_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project canonical definition data without locating or invoking services."""
    value = deepcopy(dict(value))
    tool_id = str(value.get("tool_id") or "")
    input_schema = dict(value.get("input_schema") or {})
    aliases = list(value.get("aliases") or [])
    widget = dict(value.get("widget") or {})
    return {
        "tool_id": tool_id,
        "name": tool_id,
        "display_name": str(value.get("display_name") or tool_id),
        "summary": str(value.get("description") or ""),
        "description": str(value.get("description") or ""),
        "schema": {"parameters": input_schema},
        "execution": dict(value.get("execution") or {}),
        "risk": str(value.get("risk") or "unknown"),
        "tags": list(value.get("policy_tags") or []),
        "ui": widget,
        "widget": widget,
        "aliases": aliases,
        "requires_approval": str(value.get("risk") or "") in {"high", "critical"},
        "capability_grants": [str(value.get("authority") or "")],
        "metadata": {
            "source": "global_contract",
            "aliases": aliases,
            "definition_hash": value.get("definition_hash"),
            "source_adapter_id": value.get("source_adapter_id"),
        },
    }
