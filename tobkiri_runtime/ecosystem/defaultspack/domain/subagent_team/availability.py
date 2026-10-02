"""Owner-backed availability for starting new subagent work."""

from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.frontend_settings import read_optional_frontend_settings


SUBAGENTS_DISABLED_CODE = "SUBAGENTS_DISABLED"
SUBAGENTS_DISABLED_MESSAGE = "サブエージェントは設定で無効になっています。"


def settings_owner_from_context(
    settings_owner: SettingsOwnerPort | None,
    context: dict[str, Any] | None,
) -> SettingsOwnerPort | None:
    """Prefer an explicit owner, then the trusted runtime context owner."""
    if settings_owner is not None:
        return settings_owner
    if not isinstance(context, dict):
        return None
    candidate = context.get("_settings_owner_port")
    return candidate if candidate is not None else None


def subagent_delegation_enabled(
    *, settings_owner: SettingsOwnerPort | None = None,
) -> bool:
    """Return whether a new subagent delegation may start.

    The preference is owned by the frontend settings store. Missing or malformed
    values preserve the established enabled-by-default behavior. A request body
    never participates in this decision.
    """
    if settings_owner is None:
        return True
    settings = read_optional_frontend_settings(settings_owner=settings_owner)
    automation = (
        settings.get("automation")
        if isinstance(settings.get("automation"), dict)
        else {}
    )
    return automation.get("subagent_teams_enabled") is not False


def subagents_disabled_result() -> dict[str, Any]:
    """Return the stable, safe result used by subagent entry points."""
    return {
        "code": SUBAGENTS_DISABLED_CODE,
        "message": SUBAGENTS_DISABLED_MESSAGE,
    }
