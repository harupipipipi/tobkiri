from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from domain.frontend_settings import frontend_settings_path
from domain.webhook.endpoint import WebhookEndpoint


_LINE_ADDRESSING_DEFAULT_TRIGGERS = ("#",)


def decide_line_addressing(
    event: dict[str, Any],
    external_event,
    *,
    endpoint: WebhookEndpoint,
    mentioned: bool,
) -> dict[str, Any]:
    scope_type = getattr(getattr(external_event, "scope", None), "type", "")
    if scope_type == "user":
        return {
            "addressed": True,
            "reason": "direct LINE user chat",
            "confidence": 1.0,
            "signals": ["direct_user_chat"],
        }
    if scope_type not in {"group", "room"}:
        return {
            "addressed": False,
            "reason": "unsupported LINE source scope",
            "confidence": 0.0,
            "signals": ["unsupported_scope"],
        }
    if mentioned:
        return {
            "addressed": True,
            "reason": "LINE mention targets the bot",
            "confidence": 1.0,
            "signals": ["line_mention"],
        }

    text = _line_message_text(event)
    trigger = _line_trigger_match(text, _line_addressing_trigger_words(endpoint))
    if trigger:
        return {
            "addressed": True,
            "reason": "message contains a configured LINE trigger word",
            "confidence": 0.95,
            "signals": ["trigger_word"],
            "trigger": trigger,
        }

    return {
        "addressed": False,
        "reason": "no LINE mention or configured trigger word",
        "confidence": 0.2,
        "signals": [],
    }


def _line_message_text(event: dict[str, Any]) -> str:
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if str(message.get("type") or "") != "text":
        return ""
    return str(message.get("text") or "").strip()


def _line_addressing_trigger_words(endpoint: WebhookEndpoint) -> list[str]:
    values: list[str] = []
    response = endpoint.response if isinstance(endpoint.response, dict) else {}
    conversation = endpoint.conversation if isinstance(endpoint.conversation, dict) else {}
    metadata = endpoint.metadata if isinstance(endpoint.metadata, dict) else {}
    for container in (metadata, response, conversation):
        for key in (
            "line_trigger_words",
            "group_room_trigger_words",
            "addressing_trigger_words",
            "trigger_words",
        ):
            values.extend(_listish(container.get(key)))
    if not values:
        try:
            data = json.loads(_frontend_settings_path().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        line_settings = data.get("line") if isinstance(data, dict) and isinstance(data.get("line"), dict) else {}
        for key in ("line_trigger_words", "group_room_trigger_words", "addressing_trigger_words", "trigger_words"):
            values.extend(_listish(line_settings.get(key)))
    values.extend(_LINE_ADDRESSING_DEFAULT_TRIGGERS)
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            unique.append(text)
            seen.add(key)
    return unique


def _listish(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    return [str(item or "").strip() for item in raw if str(item or "").strip()]


def _line_trigger_match(text: str, triggers: list[str]) -> str:
    normalized = str(text or "").casefold()
    if not normalized:
        return ""
    for trigger in triggers:
        candidate = str(trigger or "").strip()
        if not candidate:
            continue
        if candidate == "#":
            if "#" in str(text or ""):
                return candidate
            continue
        folded = candidate.casefold()
        if _ascii_word(folded):
            pattern = r"(?<![a-z0-9_])" + re.escape(folded) + r"(?![a-z0-9_])"
            if re.search(pattern, normalized):
                return candidate
            continue
        if folded in normalized:
            return candidate
    return ""


def _ascii_word(text: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]+", str(text or "")))


def _frontend_settings_path() -> Path:
    return frontend_settings_path()
