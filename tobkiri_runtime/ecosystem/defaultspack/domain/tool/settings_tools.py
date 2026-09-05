from __future__ import annotations

import math
import re
from typing import Any

from domain.frontend.registry import FrontendRegistry

from ._agent_os_common import err, ok
from .schema_adapter import list_or_empty


_BLOCKED_FIELD_TYPES = {
    "action",
    "api_key_setup",
    "external_tokens",
    "mobile_pairing_review",
    "readonly",
    "secret",
}
_SECRET_KEY_PATTERN = re.compile(
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|(?:^|[_-])token(?:$|[_-])|password|secret|credential|private[_-]?key",
    re.IGNORECASE,
)


def _redact(value: Any, key: str = "", depth: int = 0) -> Any:
    if _SECRET_KEY_PATTERN.search(key):
        return "[redacted]"
    if depth > 6:
        return "[omitted]"
    if isinstance(value, dict):
        return {
            str(child_key): _redact(child_value, str(child_key), depth + 1)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, key, depth + 1) for item in value[:100]]
    return value


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _SECRET_KEY_PATTERN.search(str(key)) or _contains_secret_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _safe_field(section_id: str, field: dict[str, Any]) -> bool:
    field_id = str(field.get("id") or "").strip()
    field_type = str(field.get("type") or "").strip()
    return bool(
        section_id
        and field_id
        and field_type not in _BLOCKED_FIELD_TYPES
        and not _SECRET_KEY_PATTERN.search(f"{section_id}.{field_id}")
    )


def _catalog() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    settings = FrontendRegistry().get_settings(lightweight=False)
    sections = settings.get("sections") if isinstance(settings, dict) else []
    values = settings.get("values") if isinstance(settings, dict) else {}
    return (
        [section for section in sections if isinstance(section, dict)]
        if isinstance(sections, list)
        else [],
        values if isinstance(values, dict) else {},
    )


def _field_index(sections: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for section in sections:
        section_id = str(section.get("id") or "").strip()
        fields = list_or_empty(section.get("fields"))
        for field in fields:
            if isinstance(field, dict) and _safe_field(section_id, field):
                index[(section_id, str(field.get("id") or "").strip())] = field
    return index


def _option_values(field: dict[str, Any]) -> list[str]:
    options = list_or_empty(field.get("options"))
    return [str(option.get("value")) for option in options if isinstance(option, dict) and "value" in option]


def _normalize_value(field: dict[str, Any], value: Any) -> tuple[bool, Any]:
    field_type = str(field.get("type") or "")
    if field_type == "toggle":
        if isinstance(value, bool):
            return True, value
        if value in ("true", 1):
            return True, True
        if value in ("false", 0):
            return True, False
        return False, value
    if field_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False, value
        if not math.isfinite(number):
            return False, value
        minimum = field.get("min")
        maximum = field.get("max")
        if isinstance(minimum, (int, float)):
            number = max(float(minimum), number)
        if isinstance(maximum, (int, float)):
            number = min(float(maximum), number)
        return True, int(number) if number.is_integer() else number
    if field_type == "select":
        normalized = str(value)
        options = _option_values(field)
        return (not options or normalized in options), normalized
    if field_type == "color":
        normalized = str(value or "").strip().upper()
        return bool(re.fullmatch(r"#[0-9A-F]{6}", normalized)), normalized
    if isinstance(value, (str, bool, int, float, list, dict)):
        return True, value
    return False, value


def settings_inspect(
    arguments: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del context
    data = arguments if isinstance(arguments, dict) else {}
    query = str(data.get("query") or "").strip().casefold()
    requested_sections = {
        str(item).strip()
        for item in data.get("section_ids", [])
        if str(item).strip()
    } if isinstance(data.get("section_ids"), list) else set()
    sections, values = _catalog()
    result = []
    for section in sections:
        section_id = str(section.get("id") or "").strip()
        if requested_sections and section_id not in requested_sections:
            continue
        safe_fields = []
        for field in section.get("fields", []):
            if not isinstance(field, dict) or not _safe_field(section_id, field):
                continue
            field_id = str(field.get("id") or "").strip()
            searchable = " ".join(
                str(value or "")
                for value in (
                    section_id,
                    section.get("label"),
                    section.get("description"),
                    field_id,
                    field.get("label"),
                    field.get("help"),
                )
            ).casefold()
            if query and query not in searchable:
                continue
            safe_fields.append({
                "field_id": field_id,
                "label": str(field.get("label") or field_id),
                "description": str(field.get("help") or ""),
                "type": str(field.get("type") or ""),
                "options": field.get("options") if isinstance(field.get("options"), list) else [],
                "min": field.get("min"),
                "max": field.get("max"),
                "current": _redact(
                    values.get(section_id, {}).get(field_id, field.get("default"))
                    if isinstance(values.get(section_id), dict)
                    else field.get("default"),
                    field_id,
                ),
            })
        if safe_fields:
            result.append({
                "section_id": section_id,
                "label": str(section.get("label") or section_id),
                "description": str(section.get("description") or ""),
                "fields": safe_fields,
            })
    return ok({"sections": result, "count": sum(len(section["fields"]) for section in result)})


def settings_update(
    arguments: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del context
    data = arguments if isinstance(arguments, dict) else {}
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        return err("changes must be a non-empty array", "INVALID_SETTINGS_CHANGES")
    if len(changes) > 50:
        return err("at most 50 setting changes may be applied at once", "TOO_MANY_SETTINGS_CHANGES")

    registry = FrontendRegistry()
    current = registry.get_settings(lightweight=False)
    sections = current.get("sections") if isinstance(current, dict) else []
    values = current.get("values") if isinstance(current, dict) else {}
    safe_sections = [section for section in sections if isinstance(section, dict)] if isinstance(sections, list) else []
    index = _field_index(safe_sections)
    next_values = {
        str(section_id): dict(section_values)
        for section_id, section_values in values.items()
        if isinstance(section_values, dict)
    } if isinstance(values, dict) else {}
    applied = []
    seen: set[tuple[str, str]] = set()
    for change in changes:
        if not isinstance(change, dict):
            return err("each setting change must be an object", "INVALID_SETTINGS_CHANGE")
        section_id = str(change.get("section_id") or "").strip()
        field_id = str(change.get("field_id") or "").strip()
        key = (section_id, field_id)
        field = index.get(key)
        if field is None:
            return err(f"setting is unknown or protected: {section_id}.{field_id}", "PROTECTED_SETTINGS_CHANGE")
        if key in seen:
            return err(f"duplicate setting change: {section_id}.{field_id}", "DUPLICATE_SETTINGS_CHANGE")
        seen.add(key)
        proposed_value = change.get("value")
        if _contains_secret_key(proposed_value):
            return err(f"setting value contains protected keys: {section_id}.{field_id}", "PROTECTED_SETTINGS_VALUE")
        accepted, normalized = _normalize_value(field, proposed_value)
        if not accepted:
            return err(f"invalid value for setting: {section_id}.{field_id}", "INVALID_SETTINGS_VALUE")
        section_values = next_values.setdefault(section_id, {})
        previous = section_values.get(field_id, field.get("default"))
        section_values[field_id] = normalized
        applied.append({
            "section_id": section_id,
            "field_id": field_id,
            "label": str(field.get("label") or field_id),
            "previous": previous,
            "value": normalized,
        })

    registry.update_settings(next_values)
    return ok({"applied": applied, "count": len(applied)})
