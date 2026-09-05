import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _common import ok, error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.frontend.registry import FrontendRegistry
from domain.frontend_settings_store import MUTATION_RECEIPTS_KEY, STATE_REVISIONS_KEY


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
    registry = FrontendRegistry()
    method = (input_data or {}).get("_method", "GET").upper()
    if method == "GET":
        return ok(registry.get_settings(lightweight=not _bool_with_default((input_data or {}).get("full"), False)))
    if method == "PUT":
        patches = (input_data or {}).get("patches")
        if isinstance(patches, list):
            values = {}
            for item in patches:
                if not isinstance(item, dict):
                    return error("each settings patch must be an object", "INVALID_INPUT")
                section = str(item.get("section") or "").strip()
                field = str(item.get("field") or "").strip()
                if not section or not field or section.startswith("_") or field.startswith("_"):
                    return error("settings patch requires a public section and field", "INVALID_INPUT")
                values.setdefault(section, {})[field] = item.get("value")
        else:
            values = (input_data or {}).get("values")
            if not isinstance(values, dict):
                return error("values dict or patches list is required", "INVALID_INPUT")
        updated = registry.update_settings(values)
        updated.pop(MUTATION_RECEIPTS_KEY, None)
        updated.pop(STATE_REVISIONS_KEY, None)
        return ok({"values": updated})
    return error("unsupported method", "METHOD_NOT_ALLOWED")
