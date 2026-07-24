"""Deterministic fail-closed JSON Schema subset for tool arguments."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping


def create_validate_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a validator that never coerces caller-supplied arguments."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"validate", "check"}:
            raise ValueError(f"unknown tool validation operation: {name}")
        schema = payload.get("schema")
        arguments = payload.get("arguments")
        if not isinstance(schema, Mapping):
            raise ValueError("tool input schema is required")
        errors: list[dict[str, str]] = []
        _validate(schema, arguments, "$", errors)
        return {
            "valid": not errors,
            "arguments": _json_copy(arguments) if not errors else None,
            "errors": errors,
            "coerced": False,
        }

    return operation


def _validate(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    declared_type = schema.get("type")
    allowed_types = (
        list(declared_type)
        if isinstance(declared_type, list)
        else [declared_type]
        if isinstance(declared_type, str)
        else []
    )
    if allowed_types and not any(_matches(item, value) for item in allowed_types):
        errors.append({"path": path, "code": "type", "message": "type mismatch"})
        return
    if "enum" in schema:
        choices = schema.get("enum")
        if not isinstance(choices, list) or value not in choices:
            errors.append(
                {"path": path, "code": "enum", "message": "value is not allowed"}
            )
            return
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        for key in required:
            if str(key) not in value:
                errors.append(
                    {
                        "path": f"{path}.{key}",
                        "code": "required",
                        "message": "required property is missing",
                    }
                )
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate(child, item, f"{path}.{key}", errors)
            elif additional is False:
                errors.append(
                    {
                        "path": f"{path}.{key}",
                        "code": "additional_property",
                        "message": "additional property is forbidden",
                    }
                )
            elif isinstance(additional, Mapping):
                _validate(additional, item, f"{path}.{key}", errors)
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(
                {
                    "path": path,
                    "code": "min_properties",
                    "message": "too few properties",
                }
            )
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(
                {
                    "path": path,
                    "code": "max_properties",
                    "message": "too many properties",
                }
            )
    elif isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(
                {"path": path, "code": "min_items", "message": "too few items"}
            )
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(
                {"path": path, "code": "max_items", "message": "too many items"}
            )
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate(items, item, f"{path}[{index}]", errors)
    elif isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(
                {
                    "path": path,
                    "code": "min_length",
                    "message": "string is too short",
                }
            )
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(
                {
                    "path": path,
                    "code": "max_length",
                    "message": "string is too long",
                }
            )
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(
                {"path": path, "code": "minimum", "message": "number is too small"}
            )
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(
                {"path": path, "code": "maximum", "message": "number is too large"}
            )


def _matches(expected: str, value: Any) -> bool:
    checks = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float))
        and not isinstance(value, bool),
        "null": lambda: value is None,
    }
    check = checks.get(expected)
    if check is None:
        raise ValueError(f"unsupported schema type: {expected}")
    return check()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))

