from __future__ import annotations

import copy
import json
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import unquote

from domain.capability.tool_scope import normalize_tool_scope
from domain.tool.security import requires_approval_for_security
from .normalizers import list_or_empty as list_or_empty
from .normalizers import mapping_or_empty as mapping_or_empty


_APPROVAL_REQUIRED_NAME_PARTS = ("write", "create", "update", "delete", "patch", "commit", "push")
_DEFAULT_PARAMETERS_SCHEMA = {"type": "object", "properties": {}, "required": []}
_JSON_SCHEMA_PRIMITIVE_TYPES = {"string", "number", "boolean", "integer", "object", "array", "null"}
_DEFINITION_TABLE_KEYS = ("$defs", "definitions")
_SCHEMA_CHILD_KEYS = ("items", "anyOf")
_NUMERIC_SCHEMA_HINTS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
_MAX_COMPACT_TOOL_SCHEMA_BYTES = 4_000
_MAX_COMPACT_TOOL_SCHEMA_DEPTH = 2
_SUPPORTED_SCHEMA_KEYS = {
    "$ref",
    "$defs",
    "additionalProperties",
    "anyOf",
    "definitions",
    "description",
    "enum",
    "items",
    "properties",
    "required",
    "type",
}


class ToolSchemaError(ValueError):
    """Raised when a tool parameter schema cannot be safely adapted."""


def tool_name_from_definition(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    if not isinstance(tool, dict):
        return ""
    function_def = tool.get("function")
    if isinstance(function_def, dict) and function_def.get("name"):
        return str(function_def.get("name"))
    return str(tool.get("tool_id") or tool.get("name") or "")


def adapt_tool_definition(tool: Any) -> Any:
    """Normalize defaultspack tool records to provider function-tool shape."""
    if not isinstance(tool, dict):
        return tool
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        adapted = dict(tool)
        function_def = dict(tool["function"])
        function_def["parameters"] = provider_tool_parameters(function_def.get("parameters"))
        adapted["function"] = function_def
        return adapted

    name = tool_name_from_definition(tool)
    if not name:
        return tool
    schema_value = tool.get("schema")
    if not isinstance(schema_value, dict):
        schema_value = tool.get("input_schema")
    schema: Dict[str, Any] = schema_value if isinstance(schema_value, dict) else {}
    schema_parameters = schema.get("parameters")
    parameters = schema_parameters if isinstance(schema_parameters, dict) else schema
    parameters = provider_tool_parameters(parameters)
    adapted = {
        "type": "function",
        "function": {
            "name": name,
            "description": str(tool.get("summary") or tool.get("description") or ""),
            "parameters": parameters,
        },
    }
    for key in (
        "metadata",
        "category",
        "action_type",
        "write_action",
        "capability_requirements",
        "requires_model_capabilities",
        "requires_input_modalities",
        "requires_runtime_capabilities",
        "attachment_policy",
        "supports_attachments",
    ):
        if key in tool:
            adapted[key] = tool[key]
    return adapted


def adapt_tool_definitions(tools: Iterable[Any]) -> List[Any]:
    return [adapt_tool_definition(tool) for tool in tools]


def provider_tool_parameters(parameters: Any) -> Dict[str, Any]:
    """Return a provider-safe JSON Schema object for function parameters."""
    if not isinstance(parameters, dict) or not parameters:
        return copy.deepcopy(_DEFAULT_PARAMETERS_SCHEMA)
    return sanitize_provider_tool_schema(parameters)


def sanitize_provider_tool_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize tool JSON Schema for model/provider function-calling payloads.

    This mirrors the useful shape of Codex's tool-schema adapter: keep the
    argument surface, preserve local refs that can still resolve, and prune
    dead definition tables. Semantic loss fails closed instead of silently
    turning a Tool into an empty-argument function.
    """
    if not isinstance(schema, dict):
        raise ToolSchemaError("tool schema must be a JSON object")
    value = _sanitize_json_schema(copy.deepcopy(schema))
    if not isinstance(value, dict):
        raise ToolSchemaError("tool schema must normalize to a JSON object")
    _prune_unreachable_definitions(value)
    _compact_large_tool_schema(value)
    if value.get("type") == "null":
        raise ToolSchemaError("tool input schema must not be a singleton null type")
    return value


def _sanitize_json_schema(value: Any) -> Any:
    if isinstance(value, bool):
        return {"type": "string"}
    if isinstance(value, list):
        return [_sanitize_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized: Dict[str, Any] = {}
    for key, item in value.items():
        if key in _SUPPORTED_SCHEMA_KEYS or key in _NUMERIC_SCHEMA_HINTS or key in {"const", "format", "prefixItems"}:
            sanitized[key] = copy.deepcopy(item)

    properties = sanitized.get("properties")
    if isinstance(properties, dict):
        sanitized["properties"] = {
            str(name): _schema_object_or_empty(_sanitize_json_schema(child))
            for name, child in properties.items()
        }
    elif "properties" in sanitized:
        sanitized.pop("properties", None)

    if "items" in sanitized:
        sanitized["items"] = _schema_object_or_default_string(_sanitize_json_schema(sanitized["items"]))

    additional_properties = sanitized.get("additionalProperties")
    if isinstance(additional_properties, dict) or isinstance(additional_properties, bool):
        if isinstance(additional_properties, dict):
            sanitized["additionalProperties"] = _sanitize_json_schema(additional_properties)
    elif "additionalProperties" in sanitized:
        sanitized.pop("additionalProperties", None)

    any_of = sanitized.get("anyOf")
    if isinstance(any_of, list):
        sanitized["anyOf"] = [_schema_object_or_empty(_sanitize_json_schema(child)) for child in any_of]
    elif "anyOf" in sanitized:
        sanitized.pop("anyOf", None)

    prefix_items = sanitized.pop("prefixItems", None)
    if "items" not in sanitized and isinstance(prefix_items, list) and prefix_items:
        sanitized["items"] = _schema_object_or_default_string(_sanitize_json_schema(prefix_items[0]))

    for table in _DEFINITION_TABLE_KEYS:
        _sanitize_schema_table(sanitized, table)

    if "const" in sanitized:
        sanitized["enum"] = [sanitized.pop("const")]

    schema_types = _normalized_schema_types(sanitized.get("type"))
    if not schema_types and ("$ref" in sanitized or "anyOf" in sanitized):
        sanitized.pop("type", None)
        return sanitized

    if not schema_types:
        if any(key in sanitized for key in ("properties", "required", "additionalProperties")):
            schema_types = ["object"]
        elif "items" in sanitized:
            schema_types = ["array"]
        elif "enum" in sanitized or "format" in sanitized:
            schema_types = ["string"]
        elif any(key in sanitized for key in _NUMERIC_SCHEMA_HINTS):
            schema_types = ["number"]
        elif not sanitized:
            return {}
        else:
            return {}

    _write_schema_types(sanitized, schema_types)
    _ensure_default_children_for_schema_types(sanitized, schema_types)
    _clean_schema_keywords(sanitized)
    return sanitized


def _sanitize_schema_table(schema: Dict[str, Any], key: str) -> None:
    table = schema.get(key)
    if table is None:
        return
    if not isinstance(table, dict):
        schema.pop(key, None)
        return
    schema[key] = {
        str(name): _schema_object_or_empty(_sanitize_json_schema(definition))
        for name, definition in table.items()
    }


def _schema_object_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _schema_object_or_default_string(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {"type": "string"}


def _normalized_schema_types(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value in _JSON_SCHEMA_PRIMITIVE_TYPES else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item in _JSON_SCHEMA_PRIMITIVE_TYPES]
    return []


def _write_schema_types(schema: Dict[str, Any], schema_types: List[str]) -> None:
    if not schema_types:
        schema.pop("type", None)
    elif len(schema_types) == 1:
        schema["type"] = schema_types[0]
    else:
        schema["type"] = schema_types


def _ensure_default_children_for_schema_types(schema: Dict[str, Any], schema_types: List[str]) -> None:
    if "object" in schema_types and not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    if "array" in schema_types and "items" not in schema:
        schema["items"] = {"type": "string"}


def _clean_schema_keywords(schema: Dict[str, Any]) -> None:
    schema.pop("format", None)
    for key in _NUMERIC_SCHEMA_HINTS:
        schema.pop(key, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [str(item) for item in required if isinstance(item, str)]
    elif "required" in schema:
        schema.pop("required", None)


def _for_each_schema_child(value: Dict[str, Any], include_definitions: bool = False) -> List[Any]:
    children: List[Any] = []
    properties = value.get("properties")
    if isinstance(properties, dict):
        children.extend(properties.values())
    for key in _SCHEMA_CHILD_KEYS:
        child = value.get(key)
        if child is not None:
            children.append(child)
    additional_properties = value.get("additionalProperties")
    if isinstance(additional_properties, dict):
        children.append(additional_properties)
    if include_definitions:
        for table in _DEFINITION_TABLE_KEYS:
            definitions = value.get(table)
            if isinstance(definitions, dict):
                children.extend(definitions.values())
    return children


def _prune_unreachable_definitions(schema: Dict[str, Any]) -> None:
    reachable = _collect_reachable_definitions(schema)
    for table in _DEFINITION_TABLE_KEYS:
        definitions = schema.get(table)
        if not isinstance(definitions, dict):
            continue
        schema[table] = {
            name: definition
            for name, definition in definitions.items()
            if (table, name) in reachable
        }
        if not schema[table]:
            schema.pop(table, None)


def _collect_reachable_definitions(schema: Any) -> Set[tuple[str, str]]:
    reachable: Set[tuple[str, str]] = set()
    pending = _collect_refs_outside_definitions(schema)
    while pending:
        pointer = pending.pop()
        if pointer in reachable:
            continue
        reachable.add(pointer)
        definition = _definition_for_pointer(schema, pointer)
        if definition is not None:
            pending.extend(_collect_refs(definition))
    return reachable


def _collect_refs_outside_definitions(value: Any) -> List[tuple[str, str]]:
    refs: List[tuple[str, str]] = []
    if isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs_outside_definitions(item))
    elif isinstance(value, dict):
        pointer = _parse_local_definition_ref(str(value.get("$ref") or ""))
        if pointer:
            refs.append(pointer)
        for child in _for_each_schema_child(value, include_definitions=False):
            refs.extend(_collect_refs_outside_definitions(child))
    return refs


def _collect_refs(value: Any) -> List[tuple[str, str]]:
    refs: List[tuple[str, str]] = []
    if isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs(item))
    elif isinstance(value, dict):
        pointer = _parse_local_definition_ref(str(value.get("$ref") or ""))
        if pointer:
            refs.append(pointer)
        for item in value.values():
            refs.extend(_collect_refs(item))
    return refs


def _definition_for_pointer(schema: Any, pointer: tuple[str, str]) -> Any:
    if not isinstance(schema, dict):
        return None
    table, name = pointer
    definitions = schema.get(table)
    if not isinstance(definitions, dict):
        return None
    return definitions.get(name)


def _parse_local_definition_ref(schema_ref: str) -> tuple[str, str] | None:
    if not schema_ref.startswith("#"):
        return None
    pointer = unquote(schema_ref[1:])
    if not pointer.startswith("/"):
        return None
    parts = [_decode_json_pointer_token(part) for part in pointer.split("/")[1:]]
    if len(parts) < 2 or parts[0] not in _DEFINITION_TABLE_KEYS:
        return None
    return (parts[0], parts[1])


def _decode_json_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _compact_large_tool_schema(schema: Dict[str, Any]) -> None:
    for transform in (_strip_schema_descriptions, _drop_schema_definitions, _collapse_deep_schema_objects_from_root):
        if _compact_schema_fits_budget(schema):
            break
        transform(schema)
    if not _compact_schema_fits_budget(schema):
        raise ToolSchemaError(
            "tool schema exceeds the provider budget after safe compaction"
        )


def _compact_schema_fits_budget(schema: Dict[str, Any]) -> bool:
    return len(json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) <= _MAX_COMPACT_TOOL_SCHEMA_BYTES


def _strip_schema_descriptions(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _strip_schema_descriptions(item)
    elif isinstance(value, dict):
        value.pop("description", None)
        for child in _for_each_schema_child(value, include_definitions=True):
            _strip_schema_descriptions(child)


def _drop_schema_definitions(value: Any) -> None:
    _rewrite_definition_refs_to_empty_schemas(value)
    if isinstance(value, dict):
        for table in _DEFINITION_TABLE_KEYS:
            value.pop(table, None)


def _rewrite_definition_refs_to_empty_schemas(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _rewrite_definition_refs_to_empty_schemas(item)
    elif isinstance(value, dict):
        if _parse_local_definition_ref(str(value.get("$ref") or "")):
            value.clear()
            return
        for child in _for_each_schema_child(value, include_definitions=False):
            _rewrite_definition_refs_to_empty_schemas(child)


def _collapse_deep_schema_objects_from_root(value: Any) -> None:
    _collapse_deep_schema_objects(value, 0)


def _collapse_deep_schema_objects(value: Any, depth: int) -> None:
    if isinstance(value, list):
        for item in value:
            _collapse_deep_schema_objects(item, depth)
    elif isinstance(value, dict):
        if depth >= _MAX_COMPACT_TOOL_SCHEMA_DEPTH and _is_complex_schema_object(value):
            value.clear()
            return
        for child in _for_each_schema_child(value, include_definitions=False):
            _collapse_deep_schema_objects(child, depth + 1)


def _is_complex_schema_object(value: Dict[str, Any]) -> bool:
    return any(key in value for key in ("$ref", "additionalProperties", "anyOf", "items", "properties"))


def filter_tool_definitions_for_runtime_profile(
    tools: Iterable[Any],
    runtime_profile: Optional[Dict[str, Any]] = None,
    agent_id: Optional[str] = None,
    policy_context: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    normalized = list(tools)
    enforced = runtime_profile_enforced_tool_names(
        runtime_profile,
        agent_id,
        normalized,
    )
    context_for_policy = dict(policy_context or {})
    if runtime_profile and "runtime_profile" not in context_for_policy:
        context_for_policy["runtime_profile"] = runtime_profile
    policy = policy_from_context(context_for_policy)
    if enforced is None:
        return [
            tool for tool in normalized
            if not is_tool_rejected_by_policy(tool, policy)
        ]
    filtered = [
        tool
        for tool in normalized
        if tool_name_from_definition(tool) in enforced
    ]
    return [
        tool for tool in filtered
        if not is_tool_rejected_by_policy(tool, policy)
    ]


def resolve_runtime_profile_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve runtime_profile_key into runtime_profile when possible."""
    resolved = dict(context or {})
    if isinstance(resolved.get("capability_profile"), dict):
        resolved.setdefault("runtime_profile", resolved["capability_profile"])
        return resolved
    if isinstance(resolved.get("runtime_profile"), dict):
        return resolved
    key = resolved.get("runtime_profile_key") or resolved.get("_runtime_profile_key")
    registry = resolved.get("interface_registry")
    if isinstance(key, str) and key and registry is not None:
        getter = getattr(registry, "get", None)
        if callable(getter):
            profile = getter(key)
            if isinstance(profile, dict):
                resolved["runtime_profile"] = profile
                resolved["_runtime_profile_key"] = key
                return resolved
    try:
        from core_runtime.runtime_profile_resolver import resolve_runtime_profile_context as core_resolve

        return core_resolve(resolved, interface_registry=registry)
    except Exception:
        return resolved


def connected_tool_names(
    tools: Iterable[Any],
    runtime_profile: Optional[Dict[str, Any]] = None,
    agent_id: Optional[str] = None,
) -> Set[str]:
    names = {name for name in (tool_name_from_definition(tool) for tool in tools) if name}
    names.update(_runtime_profile_tool_names(runtime_profile, agent_id, tools))
    return names


def runtime_profile_enforced_tool_names(
    runtime_profile: Optional[Dict[str, Any]],
    agent_id: Optional[str] = None,
    tools: Optional[Iterable[Any]] = None,
) -> Optional[Set[str]]:
    if not runtime_profile:
        return None
    explicit_scope = _runtime_profile_tool_scope(runtime_profile)
    if explicit_scope is not None:
        scope = normalize_tool_scope(explicit_scope)
        if scope.mode == "inherit":
            return None
        if scope.mode == "none":
            return set()
        return set(scope.ids)
    refs = _runtime_profile_tool_refs(runtime_profile, agent_id)
    if not refs:
        # A missing field inherits. An explicitly present empty list means
        # none; treating it as inherit would silently widen authority.
        if _runtime_profile_has_explicit_empty_tool_refs(runtime_profile, agent_id):
            return set()
        return None
    return _runtime_profile_tool_names(runtime_profile, agent_id, tools)


def build_tool_execution_context(
    base_context: Dict[str, Any],
    tool_name: str,
    connected_tools: Iterable[str],
) -> Dict[str, Any]:
    context = dict(base_context or {})
    graph_id = context.get("graph_id") or context.get("capability_graph_id")
    profile_id = context.get("profile_id") or context.get("capability_profile_id")
    principal_id = context.get("principal_id") or context.get("principal")
    context["capability_graph"] = {
        "graph_id": graph_id,
        "profile_id": profile_id,
        "principal_id": principal_id,
        "tool_name": tool_name,
        "connected_tools": sorted(str(name) for name in connected_tools if name),
    }
    return context


def max_tool_calls(context: Dict[str, Any]) -> Optional[int]:
    policy = context.get("profile_policy")
    if not isinstance(policy, dict):
        runtime_profile = context.get("runtime_profile")
        if isinstance(runtime_profile, dict):
            policy = runtime_profile.get("policy")
    if not isinstance(policy, dict) or "max_tool_calls" not in policy:
        policy = policy_from_context(context)
    if not isinstance(policy, dict):
        return None
    value = policy.get("max_tool_calls")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def is_tool_rejected_by_policy(tool: Any, policy: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(policy, dict):
        return False
    tool_name = tool_name_from_definition(tool)
    denylist = _normalize_policy_tool_list(
        policy.get("tool_denylist")
        or policy.get("disabled_tools")
        or policy.get("tool_blocklist")
    )
    if tool_name and tool_name in denylist:
        return True
    allowlist, has_allowlist = _normalize_policy_tool_list_from_first_present(
        policy,
        ("tool_allowlist", "enabled_tools", "allowed_tools"),
    )
    if has_allowlist and tool_name not in allowlist:
        return True
    category = _tool_metadata_value(tool, "category")
    action_type = _tool_metadata_value(tool, "action_type")
    if policy.get("allow_shell") is False and (category == "shell" or action_type == "shell"):
        return True
    if policy.get("allow_file_write") is False and (
        category in {"file_write", "filesystem_write"}
        or action_type in {"write", "file_write", "delete", "create", "update"}
    ):
        return True
    return False


def _normalize_policy_tool_list(value: Any) -> Set[str]:
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",")]
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _normalize_policy_tool_list_from_first_present(
    policy: Dict[str, Any],
    keys: tuple[str, ...],
) -> tuple[Set[str], bool]:
    for key in keys:
        if key in policy:
            return _normalize_policy_tool_list(policy.get(key)), True
    return set(), False


def tool_requires_approval_by_policy(tool: Any, policy: Optional[Dict[str, Any]]) -> bool:
    if isinstance(tool, dict) and requires_approval_for_security(tool):
        return True
    if _is_write_like_tool_name(tool_name_from_definition(tool)):
        return True
    if _tool_metadata_value(tool, "write_action") is True or _tool_metadata_value(tool, "action_type") in {
        "write",
        "file_write",
        "delete",
        "create",
        "update",
        "patch",
        "commit",
        "push",
        "send",
        "publish",
        "credential",
    }:
        return True
    if isinstance(policy, dict) and (
        _truthy_policy_value(policy.get("yolo_mode"))
        or _truthy_policy_value(policy.get("full_access"))
        or str(policy.get("action_approval_mode") or "").strip().lower() == "full"
    ):
        return False
    if not isinstance(policy, dict) or policy.get("write_actions_require_approval") is not True:
        return False
    return _tool_metadata_value(tool, "write_action") is True or _tool_metadata_value(tool, "action_type") in {
        "write",
        "file_write",
        "delete",
        "create",
        "update",
    }


def _truthy_policy_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _is_write_like_tool_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(part in lowered for part in _APPROVAL_REQUIRED_NAME_PARTS)


def policy_from_context(context: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from domain.runtime_config import merged_tool_policy

        policy = merged_tool_policy(context)
    except Exception:
        policy = context.get("profile_policy")
        if isinstance(policy, dict):
            policy = dict(policy)
        else:
            runtime_profile = context.get("runtime_profile")
            policy = _policy_from_runtime_profile(runtime_profile)
    if not isinstance(policy, dict):
        policy = {}
    effective_allowlist = _normalize_policy_tool_list(context.get("effective_tool_allowlist"))
    if effective_allowlist:
        policy = dict(policy)
        policy["tool_allowlist"] = sorted(effective_allowlist)
    return policy


def _policy_from_runtime_profile(runtime_profile: Any) -> Dict[str, Any]:
    if isinstance(runtime_profile, dict) and isinstance(runtime_profile.get("policy"), dict):
        return dict(runtime_profile["policy"])
    return {}


def _tool_metadata_value(tool: Any, key: str) -> Any:
    if not isinstance(tool, dict):
        return None
    if key in tool:
        return tool.get(key)
    metadata = tool.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get(key)
    execution = tool.get("execution")
    if isinstance(execution, dict):
        return execution.get(key)
    return None


def _runtime_profile_tool_names(
    runtime_profile: Optional[Dict[str, Any]],
    agent_id: Optional[str],
    tools: Optional[Iterable[Any]] = None,
) -> Set[str]:
    refs = _runtime_profile_tool_refs(runtime_profile, agent_id)
    if not refs:
        return set()

    supplied_names = {
        name for name in (tool_name_from_definition(tool) for tool in (tools or [])) if name
    }
    defaultspack = runtime_profile.get("defaultspack") if isinstance(runtime_profile, dict) else None
    bundles = defaultspack.get("tools") if isinstance(defaultspack, dict) else None
    bundles = bundles if isinstance(bundles, dict) else {}

    names: Set[str] = set()
    for ref in refs:
        if ref in supplied_names:
            names.add(ref)
            continue
        bundle_record = bundles.get(ref)
        if isinstance(bundle_record, dict):
            bundle_names = _tool_names_from_bundle_record(bundle_record)
            if bundle_names:
                names.update(bundle_names)
            elif not _bundle_record_has_concrete_tool_list(bundle_record):
                names.update(supplied_names)
            continue
        names.add(ref)
    return names


def _runtime_profile_tool_scope(runtime_profile: Dict[str, Any]) -> Any:
    if "tool_scope" in runtime_profile:
        return runtime_profile.get("tool_scope")
    defaultspack = runtime_profile.get("defaultspack")
    if isinstance(defaultspack, dict) and "tool_scope" in defaultspack:
        return defaultspack.get("tool_scope")
    return None


def _bundle_record_has_concrete_tool_list(record: Dict[str, Any]) -> bool:
    return any(isinstance(record.get(key), list) for key in ("tools", "tool_ids", "tool_names", "definitions"))


def _runtime_profile_tool_refs(
    runtime_profile: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> Set[str]:
    if not isinstance(runtime_profile, dict):
        return set()
    defaultspack = runtime_profile.get("defaultspack")
    if not isinstance(defaultspack, dict):
        return set()
    agents = defaultspack.get("agents")
    if not isinstance(agents, dict):
        return set()
    selected = agents.get(agent_id) if agent_id else None
    if not isinstance(selected, dict) and len(agents) == 1:
        selected = next(iter(agents.values()))
    if not isinstance(selected, dict):
        return set()
    tools = selected.get("tools", [])
    if not isinstance(tools, list):
        return set()
    return {str(tool) for tool in tools if tool}


def _runtime_profile_has_explicit_empty_tool_refs(
    runtime_profile: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> bool:
    if not isinstance(runtime_profile, dict):
        return False
    defaultspack = runtime_profile.get("defaultspack")
    agents = defaultspack.get("agents") if isinstance(defaultspack, dict) else None
    if not isinstance(agents, dict):
        return False
    selected = agents.get(agent_id) if agent_id else None
    if not isinstance(selected, dict) and len(agents) == 1:
        selected = next(iter(agents.values()))
    return (
        isinstance(selected, dict)
        and "tools" in selected
        and isinstance(selected.get("tools"), list)
        and not selected["tools"]
    )


def _tool_names_from_bundle_record(record: Dict[str, Any]) -> Set[str]:
    names: Set[str] = set()
    for key in ("tools", "tool_ids", "tool_names", "definitions"):
        values = record.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            name = tool_name_from_definition(value)
            if name:
                names.add(name)
    return names
