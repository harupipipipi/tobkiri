from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.chat.store import ChatStore
from domain.input.envelope import RumiInputEnvelope


def handle(
    envelope: RumiInputEnvelope,
    context: dict[str, Any] | None = None,
    *,
    settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    model_id = str(
        envelope.params.get("model")
        or envelope.params.get("profile_id")
        or envelope.input
        or ""
    ).strip()
    if not model_id:
        return {"status": "error", "code": "MISSING_INPUT", "error": "model is required", "assistant_text": ""}
    target = envelope.target if isinstance(envelope.target, dict) else {}
    conversation_id = str(target.get("conversation_id") or envelope.chat.get("conversation_id") or "").strip()
    if conversation_id:
        conversation = ChatStore().update_conversation(conversation_id, {"model": model_id}) or {}
        return {"status": "ok", "assistant_text": "", "conversation_id": conversation_id, "model": model_id, "conversation": conversation}
    result = ModelRuntimeSettingsService(
        settings_owner=settings_owner
    ).set_preferred_model(model_id)
    return {"status": "ok", "assistant_text": "", "model": result.get("profile_id"), "settings": result.get("settings")}
