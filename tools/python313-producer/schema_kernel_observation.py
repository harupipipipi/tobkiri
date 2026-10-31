"""Shared observation of actual original/Rust-port schema errors in CI tests.

These helpers inspect real objects; they create no validation result or error.
They execute inside the actual guest request when used for Wasm comparison.
"""

from __future__ import annotations

import hashlib
import json


def encoded(value: object, *, sort_keys: bool = False) -> bytes:
    """Encode reference DATA without losing integer/float/bool distinctions."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
    ).encode()


def node_digest(value: object) -> str:
    """Fingerprint complete related values; this never grants authority."""
    return hashlib.sha256(encoded(value, sort_keys=True)).hexdigest()


def at(root: object, path: list) -> object:
    """Follow an original error path through the actual original input."""
    for key in path:
        root = root[key]
    return root


def meta_nodes(selected=None) -> dict:
    from jsonschema import Draft202012Validator
    from jsonschema_specifications import REGISTRY

    known_nodes: dict[int, list[dict]] = {}

    def index(value: object, uri: str, path: list) -> None:
        if isinstance(value, (dict, list)):
            known_nodes.setdefault(id(value), []).append({"uri": uri, "path": path})
            members = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in members:
                index(child, uri, [*path, key])

    provider = Draft202012Validator if selected is None else selected
    registry = getattr(provider, "_SPECIFICATIONS", REGISTRY)
    # This retained label describes the selected class-root role in both lanes;
    # it does not require sharing the unselected implementation's actual object.
    index(provider.META_SCHEMA, "original-class-meta-schema", [])
    for uri in sorted(registry):
        index(registry.contents(uri), uri, [])

    return known_nodes


def observe(error, original: object, known_nodes: dict, depth: int = 0) -> dict:
    from jsonschema import exceptions
    if depth > 64:
        raise ValueError("original context exceeds bounded reference observation")
    path = list(error.path)
    absolute = list(error.absolute_path)
    record = {
        "type": type(error).__module__ + "." + type(error).__qualname__,
        "type_is_original": type(error)
        in (exceptions.SchemaError, exceptions.ValidationError),
        "message": error.message,
        "verbose_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
        "path": path,
        "absolute_path": absolute,
        "schema_path": list(error.schema_path),
        "absolute_schema_path": list(error.absolute_schema_path),
        "validator": error.validator,
        "validator_value": error.validator_value,
        "validator_value_is_original_schema_child": (
            error.schema.get(error.validator) is error.validator_value
            if isinstance(error.schema, dict) and error.validator in error.schema
            else None
        ),
        "instance_sha256": node_digest(error.instance),
        "schema_sha256": node_digest(error.schema),
        "instance_is_original_at_absolute_path": at(original, absolute)
        is error.instance,
        "schema_reference_locations": known_nodes.get(id(error.schema), []),
        "children_point_to_original_parent": all(
            child.parent is error for child in error.context
        ),
        "context": [observe(child, original, known_nodes, depth + 1) for child in error.context],
    }
    if error.cause is not None:
        record["cause"] = {
            "type": type(error.cause).__module__
            + "."
            + type(error.cause).__qualname__,
            "message": str(error.cause),
        }
    else:
        record["cause"] = None
    return record



def first_difference(expected, actual, path=()):
    """Compare observed JSON recursively without conflating bool/int/float."""
    def difference():
        return {"path": list(path), "expected": repr(expected)[:220],
                "actual": repr(actual)[:220]}

    if type(expected) is not type(actual):
        return difference()
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return difference()
        for key in expected:
            found = first_difference(expected[key], actual[key], (*path, key))
            if found is not None:
                return found
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return difference()
        for index, (left, right) in enumerate(zip(expected, actual)):
            found = first_difference(left, right, (*path, index))
            if found is not None:
                return found
    elif expected != actual:
        return difference()
    return None
