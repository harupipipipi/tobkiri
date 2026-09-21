from __future__ import annotations

from tobkiri_protocol.settings_state import SettingsOwnerPort

from blocks._common import error, ok
from domain.ambient.router import AmbientTriggerRouter


def run(input_data, context=None, *, settings_owner: SettingsOwnerPort | None = None):
    payload = input_data if isinstance(input_data, dict) else {}
    if settings_owner is None and isinstance(context, dict):
        settings_owner = context.get("_settings_owner_port")
    try:
        return ok(
            AmbientTriggerRouter().submit_event(
                payload,
                context or {},
                **({"settings_owner": settings_owner} if settings_owner is not None else {}),
            )
        )
    except ValueError as exc:
        return error(str(exc), "INVALID_AMBIENT_EVENT")
    except Exception as exc:
        return error(str(exc), "AMBIENT_EVENT_FAILED")
