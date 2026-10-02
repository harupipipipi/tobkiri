"""Inert Surface Templates, typed intents and outcomes shared across renderers.

These declarations describe presentation. They never confer operation, renderer
or resource authority; consumers resolve those through their captured Host.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

TEMPLATE_VERSION = "tobkiri.ui.surface-template.v1"
RENDERER_VERSION = "tobkiri.ui.surface-renderer.v1"
INTENT_VERSION = "tobkiri.ui.surface-intent.v1"
OUTCOME_VERSION = "tobkiri.ui.surface-outcome.v1"
RESOURCE_VERSION = "tobkiri.ui.surface-resource.v1"
RESOURCE_ACTION_CONTRACT = "tobkiri.action.surface.resource.v1"
PATTERN_EVENTS = {
    "content": frozenset({"activate"}),
    "problem": frozenset({"retry", "dismiss"}),
    "notice": frozenset({"dismiss"}),
    "progress": frozenset({"cancel"}),
    "resource_input": frozenset({"submit"}),
    "choice": frozenset({"select"}),
    "form": frozenset({"submit"}),
    "confirmation": frozenset({"confirm", "decline"}),
    "collection": frozenset({"select"}),
    "detail": frozenset({"close"}),
}
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
        "permissions",
        "grants",
        "broker_context",
        "host_context",
        "resource_handle",
        "file_path",
    }
)
_SCHEMAS = Path(__file__).with_name("schemas")


@lru_cache(maxsize=6)
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((_SCHEMAS / name).read_text("utf-8")))


def _copy(value: Any) -> Any:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("surface declaration exceeds its byte bound")
    return json.loads(raw)


def _input(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("surface input exceeds its depth bound")
    if isinstance(value, Mapping):
        if len(value) > 32:
            raise ValueError("surface input has too many keys")
        for key, child in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 128
                or key in _RESERVED
                or key.startswith("_")
            ):
                raise ValueError("surface input contains an authority or private key")
            _input(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise ValueError("surface input has too many items")
        for child in value:
            _input(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 16384:
            raise ValueError("surface input string exceeds its bound")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("surface input must be finite JSON")


def _path(value: str) -> None:
    if any(
        token in {"__proto__", "prototype", "constructor"} for token in value.split(".")
    ):
        raise ValueError("surface path contains a prototype token")


def _unique(values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise ValueError("surface declaration has colliding identities")


def _request(request: Mapping[str, Any], fixed: str | None = None) -> None:
    _input(request.get("input", {}))
    claimed = set(request.get("input", {}))
    for key in ("source_bindings", "context_bindings"):
        bindings = request.get(key, {})
        _input({name: None for name in bindings})
        if claimed & set(bindings):
            raise ValueError("surface input binding collides")
        claimed.update(bindings)
        if key == "source_bindings":
            for path in bindings.values():
                _path(path)
    if fixed in claimed:
        raise ValueError("surface fixed input binding collides")


def validate_surface_template(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate all ten finite patterns without selecting executable code."""
    normalized = _copy(value)
    _validator("surface_template_v1.schema.json").validate(normalized)
    _unique([node["id"] for node in normalized["nodes"]])
    specific = {
        "progress": {"total_path"},
        "choice": {"choices", "multiple"},
        "form": {"fields"},
        "collection": {"items_path", "id_path", "label_path"},
        "resource_input": {"resource"},
    }
    common = {"id", "pattern", "label", "body", "value_path", "intents"}
    for node in normalized["nodes"]:
        if set(node) - common - specific.get(node["pattern"], set()):
            raise ValueError("surface pattern has unrelated fields")
        for key in ("value_path", "total_path", "items_path", "id_path", "label_path"):
            if key in node:
                _path(node[key])
        intents = node.get("intents", [])
        _unique([intent["id"] for intent in intents])
        for intent in intents:
            if intent["event"] not in PATTERN_EVENTS[node["pattern"]]:
                raise ValueError("surface intent event does not match its pattern")
            _request(intent["request"], "surface_intent")
            if "outcome_path" in intent:
                _path(intent["outcome_path"])
        for items in (node.get("choices", []), node.get("fields", [])):
            _unique([item["id"] for item in items])
        for field in node.get("fields", []):
            _input({field["id"]: None})
            if field.get("min", 0) > field.get("max", float("inf")):
                raise ValueError("surface field bounds conflict")
            if field["type"] != "integer" and ("min" in field or "max" in field):
                raise ValueError("surface bounds require an integer field")
        if "resource" in node:
            _request(node["resource"]["acquire"])
            _request(node["resource"]["exchange"], "selection_id")
    return normalized


def validate_surface_renderer(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a finite renderer manifest; selected artifact trust is external."""
    normalized = _copy(value)
    _validator("surface_renderer_v1.schema.json").validate(normalized)
    return normalized


def validate_surface_resource(
    value: Mapping[str, Any], *, kind: str, stage: str
) -> dict[str, Any]:
    """Validate public ticket syntax without treating it as resource authority."""
    _validator("surface_resource_v1.schema.json").validate(value)
    if set(value) != {
        "version",
        "selection_id",
        "kind",
        "display_name",
        "expires_at_ms",
        "stage",
    } or (
        value.get("version") != RESOURCE_VERSION
        or value.get("kind") != kind
        or kind not in {"file", "image", "audio"}
        or value.get("stage") != stage
        or stage not in {"selected", "exchanged"}
        or not isinstance(value.get("selection_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value["selection_id"])
        or not isinstance(value.get("display_name"), str)
        or not 1 <= len(value["display_name"]) <= 256
        or type(value.get("expires_at_ms")) is not int
        or not 0 < value["expires_at_ms"] <= 9007199254740991
    ):
        raise ValueError("surface resource ticket is malformed")
    return _copy(value)


def validate_surface_resource_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate finite acquisition/exchange data, without approving an effect."""
    normalized = _copy(value)
    _validator("surface_resource_request_v1.schema.json").validate(normalized)
    return normalized


def normalize_surface_intent(
    template: Mapping[str, Any],
    node_id: str,
    intent_id: str,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Produce a renderer-independent typed intent from declared controls."""
    template = validate_surface_template(template)
    node = next((item for item in template["nodes"] if item["id"] == node_id), None)
    intent = (
        next(
            (item for item in node.get("intents", []) if item["id"] == intent_id), None
        )
        if node
        else None
    )
    if node is None or intent is None:
        raise ValueError("surface intent is not declared")
    normalized = _copy(values)
    _input(normalized)
    pattern = node["pattern"]
    if pattern == "choice":
        selected = normalized.get("selected")
        choices = {choice["id"] for choice in node["choices"]}
        if (
            set(normalized) != {"selected"}
            or not isinstance(selected, list)
            or not selected
            or len(selected) > len(choices)
            or any(
                not isinstance(item, str) or item not in choices for item in selected
            )
            or len(set(selected)) != len(selected)
            or (not node.get("multiple", False) and len(selected) != 1)
        ):
            raise ValueError("surface choice is outside the declared set")
    elif pattern == "form":
        fields = normalized.get("fields")
        declared = {field["id"]: field for field in node["fields"]}
        if (
            set(normalized) != {"fields"}
            or not isinstance(fields, dict)
            or set(fields) - set(declared)
        ):
            raise ValueError("surface form fields differ")
        for key, field in declared.items():
            if key not in fields:
                if field.get("required", False):
                    raise ValueError("surface form requires a field")
                continue
            value = fields[key]
            valid = {
                "text": isinstance(value, str),
                "integer": type(value) is int,
                "boolean": type(value) is bool,
            }[field["type"]]
            if not valid or (
                field["type"] == "integer"
                and not field.get("min", -9007199254740991)
                <= value
                <= field.get("max", 9007199254740991)
            ):
                raise ValueError("surface form field type or bounds differ")
    elif pattern == "resource_input":
        if set(normalized) != {"resource"} or not isinstance(
            normalized["resource"], Mapping
        ):
            raise ValueError("surface intent requires an exchanged resource")
        validate_surface_resource(
            normalized["resource"], kind=node["resource"]["kind"], stage="exchanged"
        )
    elif pattern == "collection":
        if (
            set(normalized) != {"selected_id"}
            or not isinstance(normalized["selected_id"], str)
            or not 1 <= len(normalized["selected_id"]) <= 256
        ):
            raise ValueError("surface collection selection is malformed")
    elif normalized:
        raise ValueError("surface intent takes no values")
    result = {
        "version": INTENT_VERSION,
        "template_id": template["template_id"],
        "node_id": node_id,
        "intent_id": intent_id,
        "event": intent["event"],
        "values": normalized,
    }
    _validator("surface_intent_v1.schema.json").validate(result)
    return result


def validate_surface_intent(
    value: Mapping[str, Any], template: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate received Logic input against its immutable semantic template."""
    _validator("surface_intent_v1.schema.json").validate(value)
    expected = normalize_surface_intent(
        template,
        value["node_id"],
        value["intent_id"],
        value["values"],
    )
    if dict(value) != expected:
        raise ValueError("surface intent differs from its declared template")
    return expected


def validate_surface_outcome(
    value: Mapping[str, Any], intent: Mapping[str, Any]
) -> dict[str, Any]:
    """Require an outcome for the exact typed intent; acknowledgement is data."""
    _validator("surface_outcome_v1.schema.json").validate(value)
    required = {"version", "template_id", "node_id", "intent_id", "event", "status"}
    if (
        not required <= set(value)
        or set(value) - required - {"message"}
        or value.get("version") != OUTCOME_VERSION
        or value.get("status") not in {"accepted", "rejected", "pending"}
        or any(
            value.get(key) != intent.get(key)
            for key in ("template_id", "node_id", "intent_id", "event")
        )
        or (
            "message" in value
            and (not isinstance(value["message"], str) or len(value["message"]) > 4096)
        )
    ):
        raise ValueError("surface outcome does not match its intent")
    return _copy(value)
