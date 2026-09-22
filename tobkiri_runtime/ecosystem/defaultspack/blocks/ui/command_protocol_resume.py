import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.frontend.command_protocol import CommandProtocolRegistry
from tobkiri_protocol.settings_state import SettingsOwnerPort


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    """Run with a trusted caller-supplied owner, never one from request data."""
    payload = input_data if isinstance(input_data, dict) else {}
    registry = CommandProtocolRegistry(settings_owner=settings_owner)
    if str(payload.get("action") or "resume") in {"cancel", "deny", "expire"}:
        return ok(
            registry.cancel_invocation(payload, context or {})
        )
    if not (
        str(payload.get("approval_token") or "").strip()
        or str(payload.get("authority_approval_token") or "").strip()
    ):
        return error(
            "approval_token or authority_approval_token is required",
            "APPROVAL_TOKEN_MISSING",
        )
    return ok(registry.invoke(payload, context or {}))
