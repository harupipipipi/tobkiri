import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _common import ok
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.frontend.registry import FrontendRegistry


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


def run(input_data, context):
    data = input_data if isinstance(input_data, dict) else {}
    registry = FrontendRegistry()
    full = _bool_with_default(data.get("full"), False)
    include_skills = _bool_with_default(data.get("include_skills"), False)
    return ok(
        registry.build_catalog(
            profile_id=str(data.get("profile_id") or "").strip() or None,
            lightweight=not full,
            include_skills=include_skills,
        )
    )
