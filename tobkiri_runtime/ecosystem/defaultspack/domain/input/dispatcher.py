from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.input.action_registry import get_input_action_registry
from domain.input.envelope import RumiInputEnvelope


def dispatch_input(
    envelope: RumiInputEnvelope | dict[str, Any],
    context: dict[str, Any] | None = None,
    *, settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    if isinstance(envelope, dict):
        envelope = RumiInputEnvelope.from_dict(envelope)
    delivery = envelope.delivery if isinstance(envelope.delivery, dict) else {}
    action_id = str(delivery.get("action_id") or "chat.message").strip() or "chat.message"
    registry = (get_input_action_registry(settings_owner=settings_owner)
                if settings_owner is not None else get_input_action_registry())
    handler = registry.resolve(action_id)
    if handler is None:
        return {
            "status": "error",
            "code": "UNKNOWN_INPUT_ACTION",
            "error": "unknown input action",
            "assistant_text": "",
            "action_id": action_id,
            "delivery": {"action_id": action_id},
            "available_actions": registry.list_actions(),
        }
    result = handler(envelope, context or {})
    if isinstance(result, dict):
        result.setdefault("action_id", action_id)
    return result
