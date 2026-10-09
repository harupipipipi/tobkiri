"""Validate named value connections using captured schemas, never Pack names."""

from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.flow_values import value_type

from .binding import is_step_reference, parse_step_reference, reference_path_parts
from .models import WorkflowValidationError

_UNSUPPORTED = {"$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not",
                "if", "then", "else", "patternProperties", "contains",
                "unevaluatedProperties", "unevaluatedItems", "dependentSchemas"}

_IDENTITY = ("contract_id", "contract_revision_digest", "operation_id",
             "function_principal_id")


def _key(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(value.get(name) or "") for name in _IDENTITY)


def _nominal_sensitive(value: Any, depth: int = 0) -> bool:
    if depth > 32:
        return True
    if isinstance(value, Mapping):
        return (bool(value_type(value)) or "$ref" in value or "$dynamicRef" in value
                or any(_nominal_sensitive(child, depth + 1) for child in value.values()))
    if isinstance(value, list):
        return any(_nominal_sensitive(child, depth + 1) for child in value)
    return False


def _unsafe_applicators(schema: Mapping[str, Any]) -> bool:
    return any(key in {"$ref", "$dynamicRef"} or _nominal_sensitive(schema[key])
               for key in _UNSUPPORTED.intersection(schema))


def _at(schema: Any, path: tuple[str | int, ...]) -> Mapping[str, Any]:
    for part in path:
        if not isinstance(schema, Mapping):
            return {}
        if _unsafe_applicators(schema):
            return {"$ref": "unresolved-captured-path"}
        kind = schema.get("type")
        if kind == ["array"]:
            kind = "array"
        if kind != "array" and ("items" in schema or "prefixItems" in schema):
            return {"$ref": "unresolved-captured-path"}
        if kind == "array":
            index = str(part)
            if not index.isascii() or not index.isdecimal() or (
                len(index) > 1 and index.startswith("0")
            ):
                return {}
            prefix = schema.get("prefixItems")
            position = int(index)
            schema = (prefix[position] if isinstance(prefix, list)
                      and position < len(prefix) else schema.get("items"))
        elif isinstance(part, str):
            properties = schema.get("properties")
            schema = (properties[part] if isinstance(properties, Mapping)
                      and part in properties else schema.get("additionalProperties"))
        else:
            return {}
    return schema if isinstance(schema, Mapping) else {}


def _semantic_match(source: Any, target: Any, depth: int = 0) -> bool:
    """Preserve nested nominal identities when a complete container is wired.

    Unsupported applicators are not a proof of nominal compatibility. Ordinary
    unannotated schemas still use the captured runtime JSON Schema validator.
    """
    if depth > 32:
        return False
    source = source if isinstance(source, Mapping) else {}
    target = target if isinstance(target, Mapping) else {}
    if value_type(source) != value_type(target):
        return False
    if _unsafe_applicators(source) or _unsafe_applicators(target):
        return False
    left = source.get("properties", {})
    right = target.get("properties", {})
    left = left if isinstance(left, Mapping) else {}
    right = right if isinstance(right, Mapping) else {}
    for key in left.keys() | right.keys():
        if not _semantic_match(
            left.get(key, source.get("additionalProperties")),
            right.get(key, target.get("additionalProperties")), depth + 1,
        ):
            return False
    for keyword in ("additionalProperties", "items"):
        if not _semantic_match_leaf(source.get(keyword), target.get(keyword), depth):
            return False
    lp, rp = source.get("prefixItems", []), target.get("prefixItems", [])
    lp, rp = lp if isinstance(lp, list) else [], rp if isinstance(rp, list) else []
    for index in range(max(len(lp), len(rp))):
        if not _semantic_match(
            lp[index] if index < len(lp) else source.get("items"),
            rp[index] if index < len(rp) else target.get("items"), depth + 1,
        ):
            return False
    return True


def _semantic_match_leaf(source: Any, target: Any, depth: int) -> bool:
    if source is None and target is None:
        return True
    return _semantic_match(source, target, depth + 1)


def value_binding_errors(
    steps: list[Any], snapshot: Mapping[str, Any],
) -> list[str]:
    """Reject incompatible named values before reserving or invoking anything.

    Unannotated legacy schemas retain their existing runtime validation. A
    named target needs the same named producer; JSON object shape alone does
    not turn an arbitrary object into a Sound. Bytes are checked again after
    resolving the committed output by the captured input validator.
    """
    schemas = snapshot.get("schemas")
    if not isinstance(schemas, Mapping):
        return []
    operations = snapshot.get("operations")
    declared_outputs = snapshot.get("operation_output_schemas")
    inputs = {_key(op): op.get("input_schema_digest")
              for op in (operations if isinstance(operations, list) else [])
              if isinstance(op, Mapping)}
    outputs = {_key(op): op.get("output_schema_digest")
               for op in (declared_outputs if isinstance(declared_outputs, list) else [])
               if isinstance(op, Mapping)}
    by_id = {s.get("id"): s for s in steps if isinstance(s, Mapping)}
    ids = {s for s in by_id if isinstance(s, str)}
    errors: list[str] = []

    def captured(step: Mapping[str, Any], direction: Mapping[Any, Any]) -> Any:
        request = step.get("request")
        if not isinstance(request, Mapping):
            return {}
        digest = direction.get(_key(request))
        schema = schemas.get(digest) if isinstance(digest, str) else None
        if not isinstance(schema, Mapping) or canonical_digest(schema) != digest:
            return {}
        return schema

    def walk(value: Any, target: Any, path: tuple[str | int, ...]) -> None:
        if is_step_reference(value):
            try:
                producer, source_path = parse_step_reference(value, ids)
            except WorkflowValidationError:
                return  # The existing binding grammar reports this error.
            source = _at(captured(by_id[producer], outputs), reference_path_parts(source_path))
            destination = _at(target, path)
            if not _semantic_match(source, destination):
                errors.append("connected value types do not match")
        elif isinstance(value, Mapping):
            for name, child in value.items():
                walk(child, target, (*path, str(name)))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, target, (*path, index))

    for step in by_id.values():
        request = step.get("request")
        if isinstance(request, Mapping):
            walk(request.get("input", {}), captured(step, inputs), ())
    return errors
