from __future__ import annotations

from typing import Any, Dict

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.input.envelope import RumiInputEnvelope
from domain.input.submit import submit_input


def dispatch_external_message(
    *,
    provider: str,
    text: str,
    external_key: str,
    title: str,
    event_id: str | None = None,
    model: str | None = None,
    metadata: Dict[str, Any] | None = None,
    context: Dict[str, Any] | None = None,
    settings_owner: SettingsOwnerPort | None = None,
) -> Dict[str, Any]:
    external_metadata = metadata if isinstance(metadata, dict) else {}
    envelope = RumiInputEnvelope(
        role="user",
        input=str(text or ""),
        chat={
            "conversation_id": None,
            "external_key": external_key,
            "title": title,
            "model": model,
        },
        source={
            "kind": "integration",
            "provider": provider,
            "event_id": event_id,
            "external_key": external_key,
        },
        metadata=external_metadata,
    )
    return submit_input(
        envelope,
        context or {},
        **({"settings_owner": settings_owner} if settings_owner is not None else {}),
    )


def extract_assistant_text(message: Dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part).strip()
