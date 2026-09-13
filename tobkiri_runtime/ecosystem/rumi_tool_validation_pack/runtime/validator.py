"""Deterministic fail-closed JSON Schema subset for tool arguments."""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Mapping

from core_runtime.host_provider_function_v4 import SingleOperationHostFactoryV4


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
        _check_schema(schema)
        errors: list[dict[str, str]] = []
        _validate(schema, arguments, "$", errors)
        return {
            "valid": not errors,
            "arguments": _json_copy(arguments) if not errors else None,
            "errors": errors,
            "coerced": False,
        }

    return operation


def _check_schema(schema: Mapping[str, Any], depth: int = 0) -> None:
    """Reject unimplemented constraints instead of silently validating them."""
    supported = {
        "type", "enum", "properties", "required", "additionalProperties",
        "minProperties", "maxProperties", "items", "minItems", "maxItems",
        "minLength", "maxLength", "minimum", "maximum",
        "title", "description", "default", "examples", "$comment",
    }
    if depth > 32 or set(schema) - supported:
        raise ValueError("tool schema contains unsupported constraints")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, list) or ("type" in schema and not kinds) or any(
        not isinstance(kind, str)
        or kind not in {"object", "array", "string", "boolean", "integer", "number", "null"}
        for kind in kinds
    ):
        raise ValueError("tool schema type is invalid")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise ValueError("tool schema enum is invalid")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list) or any(
        not isinstance(key, str) for key in required
    ):
        raise ValueError("tool schema properties are invalid")
    children = list(properties.values())
    if "items" in schema:
        children.append(schema["items"])
    additional = schema.get("additionalProperties", True)
    if type(additional) is not bool:
        children.append(additional)
    for child in children:
        if not isinstance(child, Mapping):
            raise ValueError("tool schema child is invalid")
        _check_schema(child, depth + 1)
    for key in (
        "minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength",
    ):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("tool schema length constraint is invalid")
    for key in ("minimum", "maximum"):
        if key in schema and (
            type(schema[key]) not in (int, float) or not math.isfinite(schema[key])
        ):
            raise ValueError("tool schema numeric constraint is invalid")


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
        if not isinstance(choices, list) or not any(
            _equal(value, item) for item in choices
        ):
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


def _equal(left: Any, right: Any) -> bool:
    """Use JSON equality, where booleans are never numbers at any depth."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equal(first, second) for first, second in zip(left, right)
        )
    return left == right


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _host_bind(context: Any) -> Callable[..., Mapping[str, Any]]:
    del context
    validate = create_validate_operation(None)

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        if set(payload) != {"schema", "arguments"}:
            raise ValueError("tool validation payload is invalid")
        return validate("validate", payload)

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id="rumi_tool_validation_pack.tool-validation.arguments",
    contract_id="tobkiri.service.tool.arguments.validate.v1",
    operation_id="rumi_tool_validation_pack.tool-arguments-validate",
    bind=_host_bind,
)
