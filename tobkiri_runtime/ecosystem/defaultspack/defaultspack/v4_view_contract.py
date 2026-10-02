"""Strict declarative view validation, separate from operation authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

VIEW_VERSION = "tobkiri.ui.view.v1"
VIEW_SLOTS = frozenset(
    {
        "workspace_tab",
        "sidebar",
        "settings",
        "chat_header",
        "composer_above",
        "composer_below",
    }
)
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "tobkiri_protocol" / "schemas" / "ui_view_v1.schema.json"
)
_VALIDATOR = Draft202012Validator(json.loads(_SCHEMA_PATH.read_text("utf-8")))
_RESERVED = frozenset(
    {
        "__proto__",
        "prototype",
        "constructor",
        "approved",
        "approval",
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "plan_hash",
        "principal_id",
        "owner_pack_id",
        "catalog_hash",
    }
)


def validate_public_input(value: Any, *, depth: int = 0) -> None:
    """Reject authority hints and oversized/non-JSON values at every depth."""
    if depth > 16:
        raise ValueError("view input is too deeply nested")
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise ValueError("view input has too many keys")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 128
                or key in _RESERVED
                or key.startswith("_")
            ):
                raise ValueError("view input contains a reserved key")
            validate_public_input(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise ValueError("view input contains too many items")
        for item in value:
            validate_public_input(item, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 16384:
            raise ValueError("view input string is too long")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("view input is not JSON")
    if depth == 0:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("view input is too large")


def validate_catalog_view(view: Mapping[str, Any]) -> None:
    """Validate a fixed-renderer view without admitting any capability."""
    _VALIDATOR.validate(view)
    controls = view.get("controls", [])
    ids = [item["id"] for item in controls]
    if len(ids) != len(set(ids)):
        raise ValueError("view controls have duplicate IDs")
    for request in [view.get("data_source", {}), *controls]:
        validate_public_input(request.get("input", {}))
        bindings = request.get("input_bindings", {})
        contexts = request.get("context_bindings", {})
        validate_public_input({key: None for key in bindings})
        validate_public_input({key: None for key in contexts})
        value_key = request.get("value_key")
        if value_key:
            validate_public_input({value_key: None})
        if (
            set(bindings) & set(request.get("input", {}))
            or set(contexts) & (set(bindings) | set(request.get("input", {})))
        ) or (
            value_key
            and (
                value_key in bindings
                or value_key in contexts
                or value_key in request.get("input", {})
            )
        ):
            raise ValueError("view input binding conflicts")
    for field in view.get("fields", []):
        for key in ("path", "total_path"):
            _validate_path(field.get(key))
    for control in controls:
        for key in ("value_path", "options_path", "id_path", "label_path", "disabled_path"):
            _validate_path(control.get(key))
        for path in control.get("input_bindings", {}).values():
            _validate_path(path)


def _validate_path(path: Any) -> None:
    if path is not None and any(
        segment in {"__proto__", "prototype", "constructor"} for segment in str(path).split(".")
    ):
        raise ValueError("view path contains a prototype key")
