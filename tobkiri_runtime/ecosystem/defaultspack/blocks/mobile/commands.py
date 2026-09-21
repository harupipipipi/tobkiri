"""blocks.mobile.commands — mobile bridge for PC slash commands.

The mobile app must not call the desktop UI routes directly with a device
token. This block exposes the same command registry behind the scoped mobile
route contract so newly-added PC commands appear and execute through one path.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error
from domain.frontend.command_registry import SlashCommandRegistry


def _merged(input_data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return {}
    merged: dict[str, Any] = {}
    for container_key in ("body", "params", "query_params", "query"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in input_data.items():
        if key in {"body", "params", "query_params", "query"}:
            continue
        merged[key] = value
    return merged


def run(input_data, context=None):
    payload = _merged(input_data if isinstance(input_data, dict) else {})
    command = str(payload.get("command") or payload.get("name") or "").strip()
    if not command:
        return error("command is required", "INVALID_INPUT")

    args = payload.get("args")
    if args is not None and not isinstance(args, dict):
        return error("args must be an object", "INVALID_INPUT")

    registry_payload: dict[str, Any] = {
        "command": command,
        "args": args or {},
        "mode": str(payload.get("mode") or "chat"),
    }
    for key in ("conversation_id", "messages"):
        value = payload.get(key)
        if value not in (None, ""):
            registry_payload[key] = value

    return SlashCommandRegistry().execute(registry_payload, context or {})
