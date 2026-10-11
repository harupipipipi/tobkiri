"""Bounded declarative step-output binding and display-schema projection.

A Workflow step binds a prior step's committed output through a whole-string
reference inside ``request.input``::

    ${steps.<step_id>.output.<dotted-path>}

The grammar is deliberately small: no interpolation inside longer strings or
code evaluation. Explicit JSON Pointer references can select array elements.  References are parsed against the exact
declared step IDs of the same Definition rather than a guessed split, so
dotted step IDs (including IDs containing ``.output.``) either match exactly
one declared step or are rejected as ambiguous.  Every reference must be
backed by an explicit ``depends_on`` entry naming the referenced step.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.flow_values import FLOW_ROLE_KEY, VALUE_TYPE_KEY, value_type

from .models import WorkflowConflict, WorkflowValidationError

_INPUT_TEMPLATE = re.compile(r"^\$\{inputs\.([a-z][a-z0-9_.-]*)\}$")
_OUTPUT_PATH = re.compile(r"^[a-z][a-z0-9_.-]*$")
_REF_PREFIX = "${steps."
_REF_SUFFIX = "}"
_OUTPUT_SEPARATOR = ".output."

# Display-only schema projection.  Reduced documents are labelled so callers
# never verify their bytes against the captured canonical schema digests.
_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "definitions",
        "dependentRequired",
        "dependentSchemas",
        "deprecated",
        "description",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "if",
        "items",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "not",
        "nullable",
        "oneOf",
        "pattern",
        "patternProperties",
        "prefixItems",
        "properties",
        "propertyNames",
        "readOnly",
        "required",
        "then",
        "title",
        "type",
        "unevaluatedProperties",
        "uniqueItems",
        "writeOnly",
    }
)
_MAX_SCHEMA_DEPTH = 24
_MAX_SCHEMA_BYTES = 32 * 1024
_MAX_PORT_SCHEMA_BYTES = 8 * 1024
_MAX_PORTS = 256
# Display-only widget hints: a bounded enum of registered selector names kept
# verbatim on scalar schemas.  Never a credential, authority, or data path.
_SELECTOR_ENUM_VALUES = frozenset({"model-profile", "tool-definition"})
_SELECTOR_KEY = "x-tobkiri-selector"
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})
_MAX_SELECTOR_ENUM = 8

DISPLAY_PROJECTION = "display-reduced"
PORT_BINDING_FORMAT = "${steps.<step_id>.output.<path>}"


def is_input_template(value: Any) -> bool:
    """Return whether a string is a whole ``${inputs.*}`` template."""

    return isinstance(value, str) and bool(_INPUT_TEMPLATE.fullmatch(value))


def is_step_reference(value: Any) -> bool:
    """Return whether a string has the step-reference envelope shape."""

    return (
        isinstance(value, str)
        and value.startswith(_REF_PREFIX)
        and value.endswith(_REF_SUFFIX)
    )


def contains_template(value: Any) -> bool:
    """Return whether any string slot holds a template or step reference."""

    if isinstance(value, str):
        return is_input_template(value) or is_step_reference(value)
    if isinstance(value, Mapping):
        return any(contains_template(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_template(item) for item in value)
    return False


def contains_step_reference(value: Any) -> bool:
    """Return whether any string slot holds a ``${steps.*}`` reference."""

    if isinstance(value, str):
        return is_step_reference(value)
    if isinstance(value, Mapping):
        return any(contains_step_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_step_reference(item) for item in value)
    return False


def reference_path_parts(path: str) -> tuple[str, ...]:
    """Decode a bounded JSON Pointer or a legacy dotted property path."""
    if not path:
        return ()
    if path.startswith("/"):
        if len(path) > 4096 or re.search(r"~(?![01])", path):
            raise WorkflowValidationError("invalid output JSON Pointer")
        parts = tuple(p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/"))
        if len(parts) > 32 or any(any(ord(c) < 32 for c in p) for p in parts):
            raise WorkflowValidationError("output JSON Pointer exceeds bounds")
        return parts
    if not _OUTPUT_PATH.fullmatch(path):
        raise WorkflowValidationError("invalid output property path")
    return tuple(path.split("."))


def parse_step_reference(
    text: str, step_ids: frozenset[str] | set[str]
) -> tuple[str, str]:
    """Parse one ``${steps.<id>.output.<path>}`` reference deterministically.

    The reference body is matched as ``<declared step id>.output.<path>`` so
    legal dotted step IDs parse only against the exact declared ID set.  Zero
    matches mean an unknown step or malformed path; more than one means the
    reference is ambiguous and must be rejected rather than guessed.
    """

    if not is_step_reference(text):
        raise WorkflowValidationError("workflow step output reference is malformed")
    body = text[len(_REF_PREFIX) : -len(_REF_SUFFIX)]
    matches: list[tuple[str, str]] = []
    for step_id in step_ids:
        pointer_prefix = step_id + ".output@"
        if body.startswith(pointer_prefix):
            pointer = body[len(pointer_prefix):]
            if pointer and not pointer.startswith("/"):
                raise WorkflowValidationError("invalid output JSON Pointer")
            reference_path_parts(pointer)
            matches.append((step_id, pointer))
        if body == step_id + ".output":
            matches.append((step_id, ""))
        prefix = step_id + _OUTPUT_SEPARATOR
        if not body.startswith(prefix):
            continue
        path = body[len(prefix) :]
        if _OUTPUT_PATH.fullmatch(path):
            matches.append((step_id, path))
    if not matches:
        raise WorkflowValidationError(
            "workflow step output reference has no declared step match"
        )
    if len(matches) > 1:
        raise WorkflowValidationError(
            "workflow step output reference is ambiguous between declared steps"
        )
    return matches[0]


def resolve_templates(
    value: Any,
    *,
    inputs: Mapping[str, Any],
    step_ids: frozenset[str] | set[str],
    committed: Mapping[str, Mapping[str, Any]],
    provenance: list[Mapping[str, Any]] | None = None,
) -> Any:
    """Materialize ``${inputs.*}`` and ``${steps.*}`` templates without eval.

    ``committed`` maps a step ID to its committed same-run output record:
    ``{"attempt_id": ..., "output": ..., "output_digest": ...}``.  Only
    succeeded, non-skipped, committed attempts are eligible; anything else
    fails closed before authority is reserved.
    """

    if isinstance(value, str):
        match = _INPUT_TEMPLATE.fullmatch(value)
        if match is not None:
            current: Any = inputs
            for part in match.group(1).split("."):
                if not isinstance(current, Mapping) or part not in current:
                    raise WorkflowValidationError(
                        "workflow input template is unresolved"
                    )
                current = current[part]
            return current
        if is_step_reference(value):
            step_id, path = parse_step_reference(value, step_ids)
            record = committed.get(step_id)
            if record is None:
                raise WorkflowConflict(
                    "prior step output is unavailable: the same-run step has "
                    "no succeeded, non-skipped, committed attempt"
                )
            current = record["output"]
            for part in reference_path_parts(path):
                if isinstance(current, Mapping) and part in current:
                    current = current[part]
                elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(current):
                    current = current[int(part)]
                else:
                    raise WorkflowValidationError("workflow step output path is unresolved")
            if provenance is not None:
                provenance.append(
                    {
                        "step_id": step_id,
                        "attempt_id": record["attempt_id"],
                        "path": path,
                        "output_digest": record["output_digest"],
                    }
                )
            return current
        return value
    if isinstance(value, Mapping):
        return {
            key: resolve_templates(
                item,
                inputs=inputs,
                step_ids=step_ids,
                committed=committed,
                provenance=provenance,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_templates(
                item,
                inputs=inputs,
                step_ids=step_ids,
                committed=committed,
                provenance=provenance,
            )
            for item in value
        ]
    return value


def step_references(value: Any) -> list[str]:
    """Collect raw ``${steps.*}`` envelope strings inside an input value."""

    found: list[str] = []
    if isinstance(value, str):
        if is_step_reference(value):
            found.append(value)
        return found
    if isinstance(value, Mapping):
        for item in value.values():
            found.extend(step_references(item))
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(step_references(item))
        return found
    return found


_SUBSCHEMA_MAP_KEYS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)
_SUBSCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    }
)
_SUBSCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


def _reduce_schema(value: Any, depth: int) -> Any:
    """Reduce a JSON Schema value, keeping only whitelisted keywords."""

    if depth > _MAX_SCHEMA_DEPTH:
        raise WorkflowValidationError("schema exceeds display projection depth")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key not in _SCHEMA_KEYWORDS:
                continue
            result[key] = _reduce_keyword(key, item, depth)
        hint = _selector_hint(value)
        if hint is not None:
            result[_SELECTOR_KEY] = hint
        semantic = value_type(value)
        if semantic is not None:
            result[VALUE_TYPE_KEY] = semantic
        role = value.get(FLOW_ROLE_KEY)
        if isinstance(role, str) and role in {"data", "configuration", "metadata"}:
            result[FLOW_ROLE_KEY] = role
        return result
    return value


def _selector_hint(schema: Mapping[str, Any]) -> str | list[str] | None:
    """Keep a bounded explicit selector enum on scalar schemas only.

    ``x-tobkiri-selector`` is a display hint for host-registered widgets (for
    example a model-profile picker).  Unknown selectors are stripped so the
    projection can never smuggle authority, credentials, or free-form data.
    """

    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        scalar = schema_type in _SCALAR_TYPES
    elif isinstance(schema_type, list):
        scalar = bool(schema_type) and all(
            isinstance(item, str) and item in _SCALAR_TYPES
            for item in schema_type
        )
    else:
        scalar = False
    if not scalar or _SELECTOR_KEY not in schema:
        return None
    raw = schema[_SELECTOR_KEY]
    if isinstance(raw, str):
        return raw if raw in _SELECTOR_ENUM_VALUES else None
    if not isinstance(raw, list) or len(raw) > _MAX_SELECTOR_ENUM:
        return None
    kept = sorted({item for item in raw if item in _SELECTOR_ENUM_VALUES})
    return kept or None


def _reduce_keyword(key: str, item: Any, depth: int) -> Any:
    """Reduce one keyword value; map keys are names, not schema keywords."""

    if key in _SUBSCHEMA_MAP_KEYS and isinstance(item, Mapping):
        return {
            name: _reduce_schema(subschema, depth + 1)
            for name, subschema in item.items()
        }
    if key in _SUBSCHEMA_KEYS:
        return _reduce_schema(item, depth + 1)
    if key in _SUBSCHEMA_LIST_KEYS and isinstance(item, list):
        return [_reduce_schema(subschema, depth + 1) for subschema in item]
    if key == "required" and isinstance(item, list):
        return [name for name in item if isinstance(name, str)]
    return item


def project_display_schema(schema: Any) -> dict[str, Any] | None:
    """Return a bounded, keyword-reduced display copy of a captured schema.

    The result is labeled by callers as ``display-reduced``: its bytes are a
    presentation projection and cannot be verified against the captured
    canonical schema digest.  ``None`` means no safe projection exists, which
    degrades to an untyped display instead of guessing.
    """

    if not isinstance(schema, Mapping):
        return None
    try:
        reduced = _reduce_schema(schema, 0)
    except WorkflowValidationError:
        return None
    try:
        if len(canonical_json(reduced)) > _MAX_SCHEMA_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return reduced


def project_ports(schema: Any) -> list[dict[str, Any]]:
    """Derive fixed named ports from a captured schema's top-level properties.

    Port names come only from declared ``properties``; no type is inferred
    from operation names.  A port schema fragment that cannot be projected
    yields an untyped port (``schema: None``), never an invented type.
    """

    if not isinstance(schema, Mapping):
        return []
    properties = schema.get("properties")
    if schema.get("type") == "object" and (
        value_type(schema) or not isinstance(properties, Mapping) or not properties
    ):
        if not properties and schema.get("additionalProperties") is False:
            return []
        fragment = project_display_schema(schema)
        if fragment is not None and len(canonical_json(fragment)) > _MAX_PORT_SCHEMA_BYTES:
            fragment = None
        return [{"name": "$", "required": True, "schema": fragment, "path": []}]
    if not isinstance(properties, Mapping):
        return []
    required = schema.get("required")
    required_set = (
        {item for item in required if isinstance(item, str)}
        if isinstance(required, list)
        else set()
    )
    ports: list[dict[str, Any]] = []
    for name in sorted(properties):
        if not isinstance(name, str):
            continue
        if len(ports) >= _MAX_PORTS:
            break
        fragment = project_display_schema(properties[name])
        if fragment is not None and len(canonical_json(fragment)) > _MAX_PORT_SCHEMA_BYTES:
            fragment = None
        ports.append(
            {
                "name": name,
                "required": name in required_set,
                "schema": fragment,
            }
        )
    return ports
