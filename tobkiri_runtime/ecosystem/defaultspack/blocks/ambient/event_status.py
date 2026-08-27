from __future__ import annotations

from blocks._common import error, ok
from domain.ambient.router import AmbientTriggerRouter


def run(input_data, context=None):
    """Return local settlement state for an idempotent ambient event."""
    payload = input_data if isinstance(input_data, dict) else {}
    try:
        return ok(AmbientTriggerRouter().event_status(str(payload.get("event_id") or "")))
    except ValueError as exc:
        return error(str(exc), "INVALID_AMBIENT_EVENT_STATUS")
    except Exception as exc:
        return error(str(exc), "AMBIENT_EVENT_STATUS_FAILED")
