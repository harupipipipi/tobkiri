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
_THREAD_OVERRIDES = frozenset(
    {
        "profile_id", "execution_profile_id", "principal", "principal_ref",
        "broker_context", "host_context", "model", "model_id", "model_profile",
        "model_profile_id", "model_reference", "model_policy", "model_override", "provider", "provider_id",
        "system_prompt", "tools", "tool_selection", "thinking_level", "reasoning_effort",
        "strategy", "strategy_reference", "approval_mode", "approval_policy",
        "permissions", "grants", "capabilities", "workspace", "workspace_id",
        "workspace_root", "cwd",
    }
)


def validate_public_input(
    value: Any, *, depth: int = 0, allow_domain_profile_ids: bool = False
) -> None:
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
                or (
                    key in _RESERVED
                    and not (key == "profile_id" and depth > 0 and allow_domain_profile_ids)
                )
                or key.startswith("_")
            ):
                raise ValueError("view input contains a reserved key")
            validate_public_input(
                item,
                depth=depth + 1,
                allow_domain_profile_ids=allow_domain_profile_ids,
            )
    elif isinstance(value, list):
        if len(value) > 256:
            raise ValueError("view input contains too many items")
        for item in value:
            validate_public_input(
                item,
                depth=depth + 1,
                allow_domain_profile_ids=allow_domain_profile_ids,
            )
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
        validate_public_input(request.get("input", {}), allow_domain_profile_ids=True)
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
    editor = view.get("record_editor")
    if isinstance(editor, Mapping):
        for key in ("records_path", "id_path", "title_path"):
            _validate_path(editor[key])
        for path in editor.get("search_paths", []):
            _validate_path(path)
        for field in [*editor.get("columns", []), *editor["fields"]]:
            _validate_path(field["path"])
            if field.get("min", 0) > field.get("max", float("inf")):
                raise ValueError("record field bounds conflict")
        field_ids = [field["id"] for field in editor["fields"]]
        field_paths = [field["path"] for field in editor["fields"]]
        action_ids = [action["id"] for action in editor.get("actions", [])]
        if any(len(values) != len(set(values)) for values in (field_ids, field_paths, action_ids)):
            raise ValueError("record editor identity collision")
        if any(
            path.startswith(other + ".")
            for path in field_paths
            for other in field_paths
            if path != other
        ):
            raise ValueError("record editor field paths overlap")
        for action in [editor["save"], *editor.get("actions", [])]:
            validate_public_input(action.get("input", {}), allow_domain_profile_ids=True)
            claimed = set(action.get("input", {}))
            for binding in ("source_bindings", "record_bindings", "context_bindings"):
                mapping = action.get(binding, {})
                validate_public_input({key: None for key in mapping})
                if claimed & set(mapping):
                    raise ValueError("record editor input binding conflicts")
                claimed.update(mapping)
                if binding != "context_bindings":
                    for path in mapping.values():
                        _validate_path(path)
            draft_key = action.get("draft_key")
            if draft_key:
                validate_public_input({draft_key: None})
                if draft_key in claimed:
                    raise ValueError("record editor draft binding conflicts")
            for condition in ("available_when", "disabled_when"):
                if condition in action:
                    _validate_path(action[condition]["path"])
    thread = view.get("conversation_thread")
    if isinstance(thread, Mapping):
        _validate_thread_input(view.get("data_source", {}).get("input", {}))
        for key in (
            "conversation_path", "messages_path", "pending_turn_path", "model_reference_path"
        ):
            _validate_path(thread.get(key))
        for key in ("send", "stop", "events", "reconcile"):
            request = thread.get(key)
            if not isinstance(request, Mapping):
                continue
            validate_public_input(request.get("input", {}))
            _validate_thread_input(request.get("input", {}))
            claimed = set(request.get("input", {}))
            for binding in ("source_bindings", "context_bindings"):
                mapping = request.get(binding, {})
                validate_public_input({name: None for name in mapping})
                _validate_thread_input({name: None for name in mapping})
                if claimed & set(mapping):
                    raise ValueError("thread input binding conflicts")
                claimed.update(mapping)
                if binding == "source_bindings":
                    for path in mapping.values():
                        _validate_path(path)
            if "turn_id" in claimed or "content" in claimed:
                raise ValueError("thread fixed input binding conflicts")


def _validate_thread_input(value: Any) -> None:
    if isinstance(value, Mapping):
        if set(value) & _THREAD_OVERRIDES:
            raise ValueError("thread input cannot override execution authority")
        for child in value.values():
            _validate_thread_input(child)
    elif isinstance(value, list):
        for child in value:
            _validate_thread_input(child)


def _validate_path(path: Any) -> None:
    if path is not None and any(
        segment in {"__proto__", "prototype", "constructor"} for segment in str(path).split(".")
    ):
        raise ValueError("view path contains a prototype key")


def validate_schema_declared_profile_targets(value: Any, schema: Mapping[str, Any]) -> None:
    """Permit nested target Profile IDs only at explicit operation-schema paths.

    Execution identity remains the server-injected root Profile. A nested
    model-policy target is ordinary domain data and never an invocation scope.
    This check does not replace complete JSON-schema validation.
    """

    def variants(candidates: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Visit each local schema node once, including cyclic local references."""
        pending = list(candidates)
        seen: set[int] = set()
        found: list[Mapping[str, Any]] = []
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if len(seen) > 4096:
                raise ValueError("domain Profile schema exceeds its bound")
            found.append(current)
            reference = current.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                referred: Any = schema
                for token in reference[2:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    if not isinstance(referred, Mapping) or token not in referred:
                        referred = None
                        break
                    referred = referred[token]
                if isinstance(referred, Mapping):
                    pending.append(referred)
            for keyword in ("allOf", "anyOf", "oneOf"):
                pending.extend(branch for branch in current.get(keyword, [])
                               if isinstance(branch, Mapping))
        return found

    def visit(item: Any, candidates: list[Mapping[str, Any]], depth: int = 0) -> None:
        if depth > 16:
            raise ValueError("domain Profile path exceeds its bound")
        expanded = variants(candidates)
        if isinstance(item, Mapping):
            for key, child in item.items():
                child_schemas = [
                    properties[key]
                    for branch in expanded
                    if isinstance(properties := branch.get("properties"), Mapping)
                    and key in properties
                    and isinstance(properties[key], Mapping)
                ]
                if key == "profile_id" and depth > 0 and not child_schemas:
                    raise ValueError("nested Profile target is not schema-declared")
                visit(child, child_schemas, depth + 1)
        elif isinstance(item, list):
            item_schemas = [
                branch["items"] for branch in expanded if isinstance(branch.get("items"), Mapping)
            ]
            for child in item:
                visit(child, item_schemas, depth + 1)

    visit(value, [schema])
