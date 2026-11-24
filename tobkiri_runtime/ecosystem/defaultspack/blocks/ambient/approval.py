from __future__ import annotations

from tobkiri_protocol.settings_state import SettingsOwnerPort

from blocks._common import ok
from domain.ambient.router import AmbientTriggerRouter


def run(input_data, context=None, *, settings_owner: SettingsOwnerPort | None = None):
    payload = input_data if isinstance(input_data, dict) else {}
    action = str(payload.get("action") or "approve").strip().lower()
    request_id = str(payload.get("request_id") or payload.get("approval_request_id") or "").strip()
    if settings_owner is None and isinstance(context, dict):
        settings_owner = context.get("_settings_owner_port")
    router = AmbientTriggerRouter()
    if action in {"deny", "reject", "cancel"}:
        return ok(router.deny_pending(request_id, reason=str(payload.get("reason") or "")))
    return ok(
        router.approve_pending(
            request_id,
            context or {},
            **({"settings_owner": settings_owner} if settings_owner is not None else {}),
        )
    )
