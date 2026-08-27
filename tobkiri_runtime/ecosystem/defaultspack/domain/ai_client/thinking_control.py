from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


LEGACY_THINKING_LEVELS = ("none", "low", "medium", "high", "xhigh")
_NUMERIC_INPUT = re.compile(r"^(?P<number>(?:\d+(?:\.\d*)?|\.\d+))(?P<suffix>[kKmMbB]?)$")
_SAFE_BINDING_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_BINDING_ROOTS = {
    "reasoning",
    "reasoning_effort",
    "thinking",
    "thinking_budget",
    "thinking_level",
}
_SI_MULTIPLIERS = {
    "": Decimal(1),
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
}


def parse_numeric_shorthand(raw_value: Any) -> int | float:
    """Parse a finite decimal value with an optional decimal SI suffix."""
    text = str(raw_value or "").strip()
    match = _NUMERIC_INPUT.fullmatch(text)
    if match is None:
        raise ValueError("value must be a number with an optional k, m, or b suffix")
    try:
        value = Decimal(match.group("number")) * _SI_MULTIPLIERS[match.group("suffix").lower()]
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("value must be a finite decimal number") from exc
    if not value.is_finite():
        raise ValueError("value must be a finite decimal number")
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def normalize_thinking_control(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Return the profile-owned thinking contract with legacy enum fallback."""
    source = profile if isinstance(profile, dict) else {}
    thinking = source.get("thinking") if isinstance(source.get("thinking"), dict) else {}
    explicit = source.get("thinking_control")
    if not isinstance(explicit, dict):
        explicit = thinking.get("control")
    if isinstance(explicit, dict):
        contract = deepcopy(explicit)
        contract["supported"] = contract.get("supported", True) is not False
        schema = contract.get("input_schema")
        contract["input_schema"] = deepcopy(schema) if isinstance(schema, dict) else {}
        binding = contract.get("request_binding")
        contract["request_binding"] = deepcopy(binding) if isinstance(binding, dict) else {}
        contract["source"] = "profile"
        return contract

    levels = thinking.get("levels")
    if not isinstance(levels, list):
        levels = source.get("thinking_levels")
    normalized_levels = (
        [str(level).strip() for level in levels if str(level or "").strip()]
        if isinstance(levels, list)
        else []
    )
    if not normalized_levels:
        normalized_levels = list(LEGACY_THINKING_LEVELS)
    return {
        "supported": bool(
            source.get("supports_thinking") or thinking.get("supported") or normalized_levels
        ),
        "input_schema": {"type": "enum", "values": normalized_levels},
        "request_binding": {},
        "source": "legacy",
    }


def validate_thinking_control(contract: dict[str, Any], raw_value: Any) -> dict[str, Any]:
    """Validate and normalize a raw thinking value against a profile contract."""
    raw = str(raw_value or "").strip()
    if contract.get("supported") is False:
        return _invalid(raw, "thinking control is not supported by this profile")
    schema = contract.get("input_schema")
    if not isinstance(schema, dict):
        return _invalid(raw, "thinking control input_schema is required")
    input_type = str(schema.get("type") or "").strip().lower()
    if raw == "auto":
        if schema.get("allow_auto") is not True:
            return _invalid(raw, "auto is not supported by this profile", input_type)
        if "auto_value" not in schema:
            return _invalid(raw, "profile does not declare an auto value", input_type)
        raw_for_validation = schema.get("auto_value")
    else:
        raw_for_validation = raw

    if input_type == "number":
        return _validate_number(schema, raw, raw_for_validation)
    if input_type == "enum":
        values = [
            str(value) for value in schema.get("values", []) if isinstance(value, (str, int, float))
        ]
        aliases = schema.get("aliases") if isinstance(schema.get("aliases"), dict) else {}
        normalized = aliases.get(raw_for_validation, raw_for_validation)
        if str(normalized) not in values:
            return _invalid(
                raw,
                f"value must be one of {', '.join(values)}",
                input_type,
            )
        return _valid(raw, str(normalized), input_type)
    if input_type == "text":
        normalized = str(raw_for_validation)
        maximum = _integer_bound(schema.get("max_length"), default=64)
        if not normalized or len(normalized) > maximum:
            return _invalid(
                raw,
                f"value must contain between 1 and {maximum} characters",
                input_type,
            )
        pattern = str(schema.get("pattern") or "").strip()
        if pattern:
            if len(pattern) > 256:
                return _invalid(raw, "profile thinking pattern is too long", input_type)
            try:
                matches = re.fullmatch(pattern, normalized)
            except re.error:
                return _invalid(raw, "profile thinking pattern is invalid", input_type)
            if matches is None:
                return _invalid(raw, "value does not match the profile pattern", input_type)
        return _valid(raw, normalized, input_type)
    return _invalid(raw, "thinking control type must be number, enum, or text")


def serialize_thinking_control(contract: dict[str, Any], normalized_value: Any) -> dict[str, Any]:
    """Serialize a normalized value using a safe, profile-declared request path."""
    binding = contract.get("request_binding")
    if not isinstance(binding, dict) or not binding:
        return {}
    path = str(binding.get("path") or "").strip()
    segments = path.split(".") if path else []
    if (
        not segments
        or segments[0] not in _ALLOWED_BINDING_ROOTS
        or len(segments) > 6
        or any(_SAFE_BINDING_SEGMENT.fullmatch(segment) is None for segment in segments)
    ):
        raise ValueError("thinking request_binding path is not allowed")
    template = binding.get("value", "$input")
    if template != "$input":
        raise ValueError("thinking request_binding value must be $input")
    value = normalized_value
    result: dict[str, Any] = {}
    cursor = result
    for segment in segments[:-1]:
        nested: dict[str, Any] = {}
        cursor[segment] = nested
        cursor = nested
    cursor[segments[-1]] = value
    return result


def _validate_number(schema: dict[str, Any], raw: str, raw_for_validation: Any) -> dict[str, Any]:
    try:
        normalized = parse_numeric_shorthand(raw_for_validation)
    except ValueError as exc:
        return _invalid(raw, str(exc), "number")
    value = Decimal(str(normalized))
    minimum = _decimal_bound(schema.get("min"))
    maximum = _decimal_bound(schema.get("max"))
    if minimum is not None and value < minimum:
        return _invalid(raw, f"value must be at least {minimum}", "number")
    if maximum is not None and value > maximum:
        return _invalid(raw, f"value must be at most {maximum}", "number")
    step = _decimal_bound(schema.get("step"))
    origin = minimum or Decimal(0)
    if step is not None and step > 0 and (value - origin) % step != 0:
        return _invalid(raw, f"value must use step {step}", "number")
    return _valid(raw, normalized, "number", unit=str(schema.get("unit") or ""))


def _decimal_bound(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _integer_bound(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _valid(
    raw: str,
    normalized: Any,
    input_type: str,
    *,
    unit: str = "",
) -> dict[str, Any]:
    return {
        "valid": True,
        "raw": raw,
        "normalized": normalized,
        "input_type": input_type,
        "unit": unit,
        "message": "",
    }


def _invalid(raw: str, message: str, input_type: str = "") -> dict[str, Any]:
    return {
        "valid": False,
        "raw": raw,
        "normalized": None,
        "input_type": input_type,
        "unit": "",
        "message": message,
    }
