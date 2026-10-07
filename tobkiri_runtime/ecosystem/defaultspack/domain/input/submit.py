from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.input.dispatcher import dispatch_input
from domain.input.actions.chat_message import (
    apply_external_runtime_prompt,
    apply_external_source_context,
)
from domain.input.envelope import RumiInputEnvelope


def submit_input(envelope: RumiInputEnvelope | dict[str, Any], context: dict[str, Any] | None = None, *, settings_owner: SettingsOwnerPort | None = None) -> dict[str, Any]:
    if isinstance(envelope, dict):
        envelope = RumiInputEnvelope.from_dict(envelope)
    envelope.delivery = dict(envelope.delivery if isinstance(envelope.delivery, dict) else {})
    envelope.delivery.setdefault("action_id", str(envelope.delivery.get("action_id") or "chat.message"))
    return dispatch_input(envelope, context or {}, settings_owner=settings_owner)
