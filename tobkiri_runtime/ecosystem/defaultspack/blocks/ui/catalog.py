import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _common import ok
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.frontend.registry import FrontendRegistry
from tobkiri_protocol.settings_state import SettingsOwnerPort


def _bool_with_default(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def run(
    input_data: dict[str, Any] | None,
    context: dict[str, Any] | None,
    *,
    settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    # A trusted in-process owner port can be supplied by the host caller.
    # Request data never selects a path, creates an owner or grants authority.
    registry = FrontendRegistry(
        settings_owner=(
            settings_owner
            if settings_owner is not None
            else (context or {}).get("_settings_owner_port")
        )
    )
    full = _bool_with_default(data.get("full"), False)
    include_skills = _bool_with_default(data.get("include_skills"), False)
    return ok(
        registry.build_catalog(
            profile_id=str(data.get("profile_id") or "").strip() or None,
            lightweight=not full,
            include_skills=include_skills,
        )
    )
