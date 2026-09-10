"""Pure saved-Chat tool selection and transcript shapes; never execution grants."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .canonical import canonical_json, strict_loads

MAX_SAVED_TOOL_CALLS = 8
MAX_SAVED_TOOL_HOPS = 2 * MAX_SAVED_TOOL_CALLS + 4
_TOOL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


def validate_tool_selection(value: Any) -> dict[str, Any]:
    """Validate explicit discovery preferences without turning them into authority."""
    if not isinstance(value, dict) or set(value) - {
        "mode", "include", "exclude", "scope", "must_use",
    } or value.get("mode") not in {"auto", "manual", "none"}:
        raise ValueError("saved tool selection is invalid")
    if value.get("scope", "turn") not in {"turn", "conversation"}:
        raise ValueError("saved tool selection scope is invalid")
    if type(value.get("must_use", False)) is not bool:
        raise ValueError("saved tool requirement is invalid")
    for key in ("include", "exclude"):
        values = value.get(key, [])
        if not isinstance(values, list) or len(values) > 256:
            raise ValueError("saved tool selection exceeds its bound")
        for item in values:
            if isinstance(item, str):
                identifier = item
            elif isinstance(item, dict) and set(item) == {"kind", "id"} and item["kind"] in {"tool", "service"}:
                identifier = item["id"]
            else:
                raise ValueError("saved tool target is invalid")
            if not isinstance(identifier, str) or not _TOOL_ID.fullmatch(identifier):
                raise ValueError("saved tool identity is invalid")
    if value["mode"] == "none" and (value.get("include") or value.get("must_use")):
        raise ValueError("disabled saved tools cannot be required")
    return strict_loads(canonical_json(value))


def saved_tool_messages(value: Any) -> list[dict[str, Any]]:
    """Validate complete assistant/tool pairs from an owned saved transcript."""
    if not isinstance(value, list) or len(value) > 2 * MAX_SAVED_TOOL_CALLS:
        raise ValueError("saved tool transcript exceeds its bound")
    messages = strict_loads(canonical_json(value), max_bytes=40 * 1024, max_depth=12)
    pending: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("saved tool transcript message is invalid")
        if message.get("role") == "assistant":
            if pending or set(message) != {"role", "content", "tool_calls"}:
                raise ValueError("saved tool assistant sequence is invalid")
            content = message["content"]
            if not isinstance(content, str) and not (
                isinstance(content, list) and all(
                    isinstance(part, dict) and set(part) == {"type", "text"}
                    and part["type"] == "text" and isinstance(part["text"], str)
                    for part in content
                )
            ):
                raise ValueError("saved tool assistant content is invalid")
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls:
                raise ValueError("saved assistant tool calls are invalid")
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if (
                    not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                    or call["type"] != "function"
                    or not isinstance(call["id"], str) or not _TOOL_ID.fullmatch(call["id"])
                    or call["id"] in seen or len(seen) >= MAX_SAVED_TOOL_CALLS
                    or not isinstance(function, dict) or set(function) != {"name", "arguments"}
                    or not isinstance(function["name"], str) or not _TOOL_ID.fullmatch(function["name"])
                    or not isinstance(function["arguments"], str)
                ):
                    raise ValueError("saved assistant tool call is invalid")
                if not isinstance(strict_loads(function["arguments"], max_bytes=16 * 1024), dict):
                    raise ValueError("saved tool arguments are invalid")
                seen.add(call["id"])
                pending.append(call["id"])
        elif (
            message.get("role") == "tool" and set(message) == {"role", "tool_call_id", "content"}
            and pending and message["tool_call_id"] == pending[0]
            and isinstance(message["content"], str)
        ):
            pending.pop(0)
        else:
            raise ValueError("saved tool result sequence is invalid")
    if pending:
        raise ValueError("saved tool transcript is incomplete")
    return messages


def saved_tool_logs(messages: Any) -> list[dict[str, Any]]:
    """Project the verified transcript into the existing Chat tool-log shape."""
    calls: dict[str, Mapping[str, Any]] = {}
    logs = []
    for message in saved_tool_messages(messages):
        if message["role"] == "assistant":
            calls.update({item["id"]: item["function"] for item in message["tool_calls"]})
        else:
            call = calls[message["tool_call_id"]]
            logs.append({
                "tool_name": call["name"], "tool_call_id": message["tool_call_id"],
                "arguments": json.loads(call["arguments"]), "result": message["content"],
            })
    return logs
