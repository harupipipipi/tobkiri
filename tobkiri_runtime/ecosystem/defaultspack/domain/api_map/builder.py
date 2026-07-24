from __future__ import annotations

import sys
from typing import Any, Dict, List
from pathlib import Path

_DEFAULTSPACK_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(_DEFAULTSPACK_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEFAULTSPACK_IMPORT_ROOT))

from core_runtime.profile_paths import active_profile_id
from core_runtime.profile_workspace import ProfileWorkspaceManager
from core_runtime.profile_runtime_selection import apply_profile_graph_selection

from domain.external.input_profile_registry import InputProfileRegistry
from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID, FunctionSpec
from domain.function_runtime.registry import function_id_for_block_module
from domain.tool.catalog_contract_client import ContractToolCatalog as ToolRegistry
from domain.webhook.endpoint_store import WebhookEndpointStore
from transport.registry import canonical_http_route_specs


def build_api_map(*, profile_id: str | None = None, focus: str | None = None) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}
    runtime_paths: List[Dict[str, Any]] = []
    function_lookup = _function_lookup()
    flow_defs = _load_flow_defs()

    for spec in canonical_http_route_specs(include_always_available=True):
        runtime_paths.append(_add_http_route(nodes, edges, spec, flow_defs, function_lookup))

    tool_registry = ToolRegistry()
    for tool in tool_registry.list_tools():
        tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip()
        if not tool_id:
            continue
        tool_node_id = f"tool:{tool_id}"
        _add_node(
            nodes,
            tool_node_id,
            "tool",
            str(tool.get("display_name") or tool.get("name") or tool_id),
            tool_id,
            _tool_metadata(tool),
        )
        _add_tool_execution(nodes, edges, tool_node_id, tool, function_lookup)

    endpoint_store = WebhookEndpointStore()
    input_profiles = {profile.id: profile for profile in InputProfileRegistry().list_profiles()}
    for endpoint in endpoint_store.list_endpoints():
        endpoint_id = str(endpoint.get("id") or "").strip()
        if not endpoint_id:
            continue
        webhook_node_id = f"webhook:{endpoint_id}"
        _add_node(
            nodes,
            webhook_node_id,
            "webhook",
            endpoint_id,
            endpoint_id,
            dict(endpoint),
        )
        inbound_route_id = "api:POST /api/webhooks/inbound/{webhook_id}"
        if inbound_route_id in nodes:
            _add_edge(
                edges,
                inbound_route_id,
                webhook_node_id,
                "dispatches_to_endpoint",
                {"path_param": "webhook_id", "endpoint_id": endpoint_id},
            )
        input_profile_key = str(endpoint.get("input_profile_id") or "").strip()
        if input_profile_key:
            input_profile = input_profiles.get(input_profile_key)
            input_node_id = f"node:{input_profile_key}"
            _add_node(
                nodes,
                input_node_id,
                "input_profile",
                str(getattr(input_profile, "display_name", "") or input_profile_key),
                input_profile_key,
                {"input_profile_id": input_profile_key},
            )
            _add_edge(edges, webhook_node_id, input_node_id, "uses_input_profile", {"input_profile_id": input_profile_key})
        delivery = endpoint.get("default_delivery") if isinstance(endpoint.get("default_delivery"), dict) else {}
        action_id = str(delivery.get("action_id") or "").strip()
        if action_id:
            delivery_node_id = f"delivery:{action_id}"
            _add_node(
                nodes,
                delivery_node_id,
                "delivery_action",
                action_id,
                action_id,
                {"action_id": action_id, "default_delivery": dict(delivery)},
            )
            _add_edge(edges, webhook_node_id, delivery_node_id, "delivers_to", {"action_id": action_id})

    diagnostics: List[Dict[str, Any]] = []
    profile_edges = _profile_selection_edges(profile_id)
    if profile_edges["diagnostics"]:
        diagnostics.extend(profile_edges["diagnostics"])
    for node in profile_edges["nodes"]:
        _add_node(
            nodes,
            str(node.get("id") or ""),
            str(node.get("kind") or ""),
            str(node.get("label") or ""),
            str(node.get("ref") or ""),
            node.get("metadata") if isinstance(node.get("metadata"), dict) else {},
        )
    for edge in profile_edges["edges"]:
        edges.setdefault(edge["id"], edge)

    filtered_nodes = list(nodes.values())
    filtered_edges = list(edges.values())
    if focus:
        focus_id = str(focus).strip()
        neighbor_ids = {focus_id}
        for edge in filtered_edges:
            if edge["from_id"] == focus_id:
                neighbor_ids.add(edge["to_id"])
            if edge["to_id"] == focus_id:
                neighbor_ids.add(edge["from_id"])
        filtered_nodes = [node for node in filtered_nodes if node["id"] in neighbor_ids]
        filtered_edges = [
            edge
            for edge in filtered_edges
            if edge["from_id"] in neighbor_ids and edge["to_id"] in neighbor_ids
        ]

    return {
        "nodes": filtered_nodes,
        "edges": filtered_edges,
        "summary": {
            "node_count": len(filtered_nodes),
            "edge_count": len(filtered_edges),
            "route_count": len([node for node in filtered_nodes if node["kind"] == "api"]),
            "tool_count": len([node for node in filtered_nodes if node["kind"] == "tool"]),
            "webhook_count": len([node for node in filtered_nodes if node["kind"] == "webhook"]),
            "flow_count": len([node for node in filtered_nodes if node["kind"] == "flow"]),
            "function_count": len([node for node in filtered_nodes if node["kind"] == "function"]),
            "operation_count": len([
                node
                for node in filtered_nodes
                if node["kind"] in {"function", "tool", "block", "handler", "tool_handler"}
            ]),
            "implementation_count": len([
                node
                for node in filtered_nodes
                if node["kind"] in {"block", "handler", "tool_handler"}
            ]),
            "selected_tool_count": len(profile_edges.get("selected", {}).get("tools", [])),
            "selected_route_count": len(profile_edges.get("selected", {}).get("api_routes", [])),
        },
        "runtime_paths": runtime_paths,
        "profile_runtime": profile_edges.get("profile_runtime", {}),
        "diagnostics": diagnostics,
    }


def _add_http_route(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    spec: Any,
    flow_defs: Dict[str, Dict[str, Any]],
    function_lookup: Dict[str, FunctionSpec],
) -> Dict[str, Any]:
    route_id = f"{spec.method} {spec.pattern}"
    route_node_id = f"api:{route_id}"
    source_type = (
        "flow"
        if spec.flow_id
        else "function"
        if spec.function_name
        else "block"
        if spec.block_module
        else "handler"
    )
    _add_node(
        nodes,
        route_node_id,
        "api",
        route_id,
        route_id,
        {
            "runtime_role": "entrypoint",
            "method": spec.method,
            "path": spec.pattern,
            "route_source": _route_source(spec),
            "source_type": source_type,
            "block_module": spec.block_module,
            "function_name": spec.function_name,
            "flow_id": spec.flow_id,
            "fallback_block_module": spec.fallback_block_module,
            "handler_name": spec.handler_name,
            "path_inject": dict(spec.path_inject),
            "defaults": dict(spec.defaults),
        },
    )
    path = {
        "id": route_node_id,
        "label": route_id,
        "entrypoint": {
            "node_id": route_node_id,
            "method": spec.method,
            "path": spec.pattern,
            "source": _route_source(spec),
            "source_type": source_type,
        },
        "primary": None,
        "fallback": None,
        "steps": [],
    }

    if spec.flow_id:
        flow_node_id = _flow_node_id(spec.flow_id)
        _add_node(
            nodes,
            flow_node_id,
            "flow",
            spec.flow_id,
            spec.flow_id,
            {"flow_id": spec.flow_id, **_flow_metadata(flow_defs.get(spec.flow_id))},
        )
        _add_edge(edges, route_node_id, flow_node_id, "enters_flow", {"flow_id": spec.flow_id})
        path["primary"] = {"kind": "flow", "id": spec.flow_id, "node_id": flow_node_id}
        path["steps"] = _add_flow_steps(nodes, edges, spec.flow_id, flow_defs, function_lookup)

    if spec.function_name:
        function_segment = _add_function_reference(
            nodes,
            edges,
            route_node_id,
            spec.function_name,
            "calls_function",
            function_lookup,
            {"route_id": route_id},
        )
        path["primary"] = function_segment

    if spec.block_module:
        path["primary"] = _add_block_route_adapter(
            nodes,
            edges,
            route_node_id,
            spec.block_module,
            "handled_by",
            function_lookup,
            {"route_id": route_id, "adapter": "http_block_route"},
        )

    if spec.handler_name:
        handler_node_id = f"handler:{spec.handler_name}"
        _add_node(
            nodes,
            handler_node_id,
            "handler",
            spec.handler_name,
            spec.handler_name,
            {"runtime_role": "implementation", "handler_name": spec.handler_name},
        )
        _add_edge(edges, route_node_id, handler_node_id, "handled_by", {"handler_name": spec.handler_name})
        path["primary"] = {"kind": "handler", "id": spec.handler_name, "node_id": handler_node_id}

    if spec.fallback_block_module:
        path["fallback"] = _add_block_route_adapter(
            nodes,
            edges,
            route_node_id,
            spec.fallback_block_module,
            "fallback",
            function_lookup,
            {"route_id": route_id, "adapter": "http_fallback_block"},
        )

    return path


def _add_flow_steps(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    flow_id: str,
    flow_defs: Dict[str, Dict[str, Any]],
    function_lookup: Dict[str, FunctionSpec],
) -> List[Dict[str, Any]]:
    flow_def = flow_defs.get(flow_id) if flow_id else None
    steps = flow_def.get("steps") if isinstance(flow_def, dict) and isinstance(flow_def.get("steps"), list) else []
    result: List[Dict[str, Any]] = []
    previous_step_node_id = ""
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or f"step_{index + 1}").strip()
        step_type = str(step.get("type") or "").strip()
        step_node_id = f"step:{flow_id}:{step_id}"
        _add_node(
            nodes,
            step_node_id,
            "flow_step",
            step_id,
            step_id,
            {
                "flow_id": flow_id,
                "runtime_role": "flow_step",
                "step_id": step_id,
                "step_type": step_type,
                "order": index + 1,
                "when": step.get("when"),
                "output": step.get("output"),
            },
        )
        _add_edge(edges, _flow_node_id(flow_id), step_node_id, "contains_step", {"order": index + 1, "step_type": step_type})
        if previous_step_node_id:
            _add_edge(edges, previous_step_node_id, step_node_id, "then", {"order": index + 1})
        previous_step_node_id = step_node_id
        segment: Dict[str, Any] = {
            "kind": "flow_step",
            "id": step_id,
            "node_id": step_node_id,
            "step_type": step_type,
            "order": index + 1,
            "target": None,
        }
        if step_type == "function":
            function_name = str(step.get("function") or "").strip()
            segment["target"] = _add_function_reference(
                nodes,
                edges,
                step_node_id,
                function_name,
                "calls_function",
                function_lookup,
                {"flow_id": flow_id, "step_id": step_id},
            )
        elif step_type == "subflow":
            subflow_id = str(step.get("flow") or step.get("flow_id") or step.get("subflow") or "").strip()
            if subflow_id.startswith("{{"):
                choice_node_id = f"runtime_choice:{flow_id}:{step_id}"
                _add_node(
                    nodes,
                    choice_node_id,
                    "runtime_choice",
                    "runtime subflow",
                    subflow_id,
                    {"expression": subflow_id, "flow_id": flow_id, "step_id": step_id},
                )
                _add_edge(edges, step_node_id, choice_node_id, "resolves_at_runtime", {"expression": subflow_id})
                segment["target"] = {"kind": "runtime_choice", "id": subflow_id, "node_id": choice_node_id}
            elif subflow_id:
                subflow_node_id = _flow_node_id(subflow_id)
                _add_node(
                    nodes,
                    subflow_node_id,
                    "flow",
                    subflow_id,
                    subflow_id,
                    {"flow_id": subflow_id, **_flow_metadata(flow_defs.get(subflow_id))},
                )
                _add_edge(edges, step_node_id, subflow_node_id, "runs_subflow", {"flow_id": subflow_id})
                segment["target"] = {"kind": "flow", "id": subflow_id, "node_id": subflow_node_id}
        result.append(segment)
    return result


def _add_tool_execution(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    tool_node_id: str,
    tool: Dict[str, Any],
    function_lookup: Dict[str, FunctionSpec],
) -> None:
    execution = tool.get("execution") if isinstance(tool.get("execution"), dict) else {}
    execution_type = str(execution.get("type") or "local").strip().lower()
    if execution_type == "rumi_function":
        qualified_name = str(execution.get("qualified_name") or "").strip()
        _add_function_reference(nodes, edges, tool_node_id, qualified_name, "calls_function", function_lookup, {"execution_type": execution_type})
        return
    if execution_type == "capability":
        permission_id = str(execution.get("permission_id") or "").strip()
        if permission_id:
            capability_node_id = f"capability:{permission_id}"
            _add_node(nodes, capability_node_id, "capability", permission_id, permission_id, {"permission_id": permission_id})
            _add_edge(edges, tool_node_id, capability_node_id, "executes_capability", {"permission_id": permission_id})
        return
    if execution_type == "mcp":
        server_name = str(execution.get("server_name") or "").strip()
        mcp_tool_name = str(execution.get("mcp_tool_name") or tool.get("name") or tool.get("tool_id") or "").strip()
        if server_name:
            server_node_id = f"mcp:{server_name}"
            _add_node(nodes, server_node_id, "mcp_server", server_name, server_name, {"server_name": server_name})
            _add_edge(edges, tool_node_id, server_node_id, "calls_mcp_server", {"server_name": server_name, "mcp_tool_name": mcp_tool_name})
        return
    handler = str(execution.get("handler") or "").strip()
    if handler:
        handler_node_id = f"handler:{handler}"
        _add_node(
            nodes,
            handler_node_id,
            "tool_handler",
            handler.rsplit(":", 1)[-1],
            handler,
            {"runtime_role": "implementation", "handler": handler, "execution_type": execution_type},
        )
        _add_edge(edges, tool_node_id, handler_node_id, "executes_handler", {"handler": handler, "execution_type": execution_type})
        return
    runtime_node_id = f"runtime:{execution_type or 'local'}"
    _add_node(nodes, runtime_node_id, "runtime", execution_type or "local", execution_type or "local", {"execution_type": execution_type or "local"})
    _add_edge(edges, tool_node_id, runtime_node_id, "executes_in_runtime", {"execution_type": execution_type or "local"})


def _add_block_route_adapter(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    from_node_id: str,
    block_module: str,
    edge_kind: str,
    function_lookup: Dict[str, FunctionSpec],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    block_module = str(block_module or "").strip()
    block_node_id = _block_node_id(block_module)
    function_id = function_id_for_block_module(block_module) if block_module else None
    if function_id:
        function_segment = _add_function_reference(
            nodes,
            edges,
            from_node_id,
            f"defaultspack:{function_id}",
            edge_kind,
            function_lookup,
            {**metadata, "block_module": block_module, "function_first": True},
        )
        _add_block_node(nodes, block_module, {"function_id": function_id})
        if function_segment.get("node_id"):
            _add_edge(edges, function_segment["node_id"], block_node_id, "implemented_by", {"block_module": block_module})
        return {
            "kind": "function",
            "id": f"defaultspack:{function_id}",
            "node_id": function_segment.get("node_id"),
            "block_node_id": block_node_id,
            "block_module": block_module,
        }
    _add_block_node(nodes, block_module, {})
    _add_edge(edges, from_node_id, block_node_id, edge_kind, {**metadata, "block_module": block_module})
    return {"kind": "block", "id": block_module, "node_id": block_node_id, "block_module": block_module}


def _add_function_reference(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    from_node_id: str,
    function_name: str,
    edge_kind: str,
    function_lookup: Dict[str, FunctionSpec],
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    function_name = str(function_name or "").strip()
    spec = _resolve_function(function_name, function_lookup)
    if spec is None:
        function_node_id = f"function:{function_name}"
        _add_node(nodes, function_node_id, "function", function_name, function_name, {"function_name": function_name, "resolved": False})
        _add_edge(edges, from_node_id, function_node_id, edge_kind, {**dict(metadata or {}), "function_name": function_name, "resolved": False})
        return {"kind": "function", "id": function_name, "node_id": function_node_id, "resolved": False}
    function_node_id = _function_node_id(spec)
    _add_node(nodes, function_node_id, "function", spec.function_id, f"defaultspack:{spec.function_id}", _function_metadata(spec))
    _add_edge(edges, from_node_id, function_node_id, edge_kind, {**dict(metadata or {}), "function_id": spec.function_id})
    if spec.block_module:
        _add_block_node(nodes, spec.block_module, {"function_id": spec.function_id})
        _add_edge(edges, function_node_id, _block_node_id(spec.block_module), "implemented_by", {"block_module": spec.block_module})
    return {"kind": "function", "id": f"defaultspack:{spec.function_id}", "node_id": function_node_id, "resolved": True}


def _profile_selection_edges(profile_id: str | None) -> Dict[str, Any]:
    resolved_profile_id = str(profile_id or active_profile_id() or "").strip()
    if not resolved_profile_id:
        return {"nodes": [], "edges": [], "diagnostics": [], "selected": {}, "profile_runtime": {}}
    profile = ProfileWorkspaceManager().load_profile_yaml(resolved_profile_id)
    if not profile:
        return {
            "nodes": [],
            "edges": [],
            "diagnostics": [{"level": "warning", "code": "profile_not_found", "message": f"Profile '{resolved_profile_id}' was not found."}],
            "selected": {},
            "profile_runtime": {"profile_id": resolved_profile_id, "found": False},
        }
    profile = apply_profile_graph_selection(profile)
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    selected = metadata.get("selected") if isinstance(metadata.get("selected"), dict) else {}
    policy = profile.get("policy") if isinstance(profile.get("policy"), dict) else {}
    profile_node = {
        "id": f"profile:{resolved_profile_id}",
        "kind": "profile",
        "label": str(profile.get("name") or resolved_profile_id),
        "ref": resolved_profile_id,
        "metadata": {
            "profile_id": resolved_profile_id,
            "system_prompt_id": str(profile.get("system_prompt_id") or ""),
            "default_prompt_id": str(profile.get("default_prompt_id") or ""),
            "policy": {
                "tool_allowlist": _string_list(policy.get("tool_allowlist")),
                "api_route_allowlist": _string_list(policy.get("api_route_allowlist")),
                "enforce_api_route_allowlist": bool(policy.get("enforce_api_route_allowlist", False)),
            },
        },
    }
    nodes = [profile_node]
    edges: List[Dict[str, Any]] = []
    for category, prefix, edge_kind in (
        ("tools", "tool", "selects"),
        ("webhooks", "webhook", "receives_from"),
        ("api_routes", "api", "allows_api"),
        ("prompts", "prompt", "uses_prompt"),
        ("frontend", "frontend", "uses_frontend"),
    ):
        for item in selected.get(category) if isinstance(selected.get(category), list) else []:
            item_id = str(item or "").strip()
            if not item_id:
                continue
            node_id = f"{prefix}:{item_id}"
            nodes.append({"id": node_id, "kind": prefix, "label": item_id, "ref": item_id, "metadata": {"selected": True}})
            edges.append(_edge(profile_node["id"], node_id, edge_kind, {"selected": True}))
    diagnostics: List[Dict[str, Any]] = []
    if policy.get("api_route_allowlist") and not policy.get("enforce_api_route_allowlist"):
        diagnostics.append(
            {
                "level": "info",
                "code": "api_route_allowlist_not_enforced",
                "message": "API routes are selected, but enforce_api_route_allowlist=false, so this is documentation and preview only.",
            }
        )
    normalized_selected = {
        "tools": _string_list(selected.get("tools")),
        "webhooks": _string_list(selected.get("webhooks")),
        "api_routes": _string_list(selected.get("api_routes")),
        "prompts": _string_list(selected.get("prompts")),
        "frontend": _string_list(selected.get("frontend")),
        "flows": _string_list(selected.get("flows")),
        "nodes": _string_list(selected.get("nodes")),
    }
    return {
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
        "selected": normalized_selected,
        "profile_runtime": {
            "profile_id": resolved_profile_id,
            "found": True,
            "name": str(profile.get("name") or resolved_profile_id),
            "selected": normalized_selected,
            "policy": {
                "tool_allowlist": _string_list(policy.get("tool_allowlist")),
                "api_route_allowlist": _string_list(policy.get("api_route_allowlist")),
                "enforce_api_route_allowlist": bool(policy.get("enforce_api_route_allowlist", False)),
            },
            "system_prompt_id": str(profile.get("system_prompt_id") or ""),
            "default_prompt_id": str(profile.get("default_prompt_id") or ""),
        },
    }


def _edge(from_id: str, to_id: str, kind: str, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "id": f"{from_id}->{to_id}:{kind}",
        "from_id": from_id,
        "to_id": to_id,
        "kind": kind,
        "active": True,
        "metadata": dict(metadata or {}),
    }


def _add_node(
    nodes: Dict[str, Dict[str, Any]],
    node_id: str,
    kind: str,
    label: str,
    ref: str,
    metadata: Dict[str, Any] | None = None,
) -> None:
    if not node_id:
        return
    next_node = {
        "id": node_id,
        "kind": kind,
        "label": str(label or ref or node_id),
        "ref": str(ref or ""),
        "metadata": dict(metadata or {}),
    }
    existing = nodes.get(node_id)
    if existing is None:
        nodes[node_id] = next_node
        return
    existing_metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    nodes[node_id] = {
        **existing,
        "kind": existing.get("kind") or next_node["kind"],
        "label": existing.get("label") or next_node["label"],
        "ref": existing.get("ref") or next_node["ref"],
        "metadata": {**existing_metadata, **next_node["metadata"]},
    }


def _add_edge(
    edges: Dict[str, Dict[str, Any]],
    from_id: str,
    to_id: str,
    kind: str,
    metadata: Dict[str, Any] | None = None,
) -> None:
    if not from_id or not to_id:
        return
    edge = _edge(from_id, to_id, kind, metadata)
    edges.setdefault(edge["id"], edge)


def _add_block_node(nodes: Dict[str, Dict[str, Any]], block_module: str, metadata: Dict[str, Any] | None = None) -> None:
    block_module = str(block_module or "").strip()
    if not block_module:
        return
    _add_node(
        nodes,
        _block_node_id(block_module),
        "block",
        block_module.rsplit(".", 1)[-1],
        block_module,
        {"runtime_role": "implementation", "block_module": block_module, **dict(metadata or {})},
    )


def _function_lookup() -> Dict[str, FunctionSpec]:
    lookup: Dict[str, FunctionSpec] = {}
    for spec in FUNCTION_SPECS_BY_ID.values():
        keys = [spec.function_id, f"defaultspack:{spec.function_id}", *list(spec.aliases)]
        for key in keys:
            normalized = str(key or "").strip()
            if normalized:
                lookup[normalized] = spec
    return lookup


def _resolve_function(function_name: str, function_lookup: Dict[str, FunctionSpec]) -> FunctionSpec | None:
    normalized = str(function_name or "").strip()
    if not normalized:
        return None
    if normalized in function_lookup:
        return function_lookup[normalized]
    if normalized.startswith("defaultspack:"):
        return function_lookup.get(normalized.split(":", 1)[1])
    return None


def _function_node_id(spec: FunctionSpec) -> str:
    return f"function:defaultspack:{spec.function_id}"


def _block_node_id(block_module: str) -> str:
    return f"block:{block_module}"


def _flow_node_id(flow_id: str) -> str:
    return f"flow:{flow_id}"


def _function_metadata(spec: FunctionSpec) -> Dict[str, Any]:
    return {
        "runtime_role": "operation",
        "function_id": spec.function_id,
        "qualified_name": f"defaultspack:{spec.function_id}",
        "aliases": list(spec.aliases),
        "tags": list(spec.tags),
        "risk": spec.risk,
        "requires": list(spec.requires),
        "caller_requires": list(spec.caller_requires),
        "block_module": spec.block_module or "",
    }


def _tool_metadata(tool: Dict[str, Any]) -> Dict[str, Any]:
    execution = tool.get("execution") if isinstance(tool.get("execution"), dict) else {}
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    return {
        **dict(tool),
        "runtime_role": "tool_facade",
        "execution_type": str(execution.get("type") or "local"),
        "handler": str(execution.get("handler") or ""),
        "qualified_name": str(execution.get("qualified_name") or ""),
        "permission_id": str(execution.get("permission_id") or ""),
        "server_name": str(execution.get("server_name") or ""),
        "source_pack_id": str(tool.get("source_pack_id") or metadata.get("source_pack_id") or ""),
        "risk": str(metadata.get("risk") or tool.get("risk") or "low"),
    }


def _load_flow_defs() -> Dict[str, Dict[str, Any]]:
    flows_dir = Path(__file__).resolve().parents[2] / "flows"
    flow_defs: Dict[str, Dict[str, Any]] = {}
    if not flows_dir.is_dir():
        return flow_defs
    for path in sorted(flows_dir.glob("*.flow.yaml")):
        flow_def = _read_flow_yaml(path)
        flow_id = str(flow_def.get("flow_id") or path.name[: -len(".flow.yaml")]).strip()
        if flow_id:
            flow_def["_yaml_path"] = str(path)
            flow_defs[flow_id] = flow_def
    for path in sorted(flows_dir.glob("*/flow.yaml")):
        flow_def = _read_flow_yaml(path)
        flow_id = str(flow_def.get("flow_id") or path.parent.name).strip()
        if flow_id:
            flow_def["_yaml_path"] = str(path)
            flow_defs[flow_id] = flow_def
    return flow_defs


def _read_flow_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _flow_metadata(flow_def: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(flow_def, dict):
        return {"resolved": False}
    return {
        "resolved": True,
        "description": str(flow_def.get("description") or ""),
        "path": str(flow_def.get("_yaml_path") or ""),
        "step_count": len(flow_def.get("steps") if isinstance(flow_def.get("steps"), list) else []),
    }


def _route_source(spec: Any) -> str:
    if str(getattr(spec, "handler_name", "") or "").strip():
        return "always_available"
    if str(getattr(spec, "flow_id", "") or "").strip():
        return "flow_yaml_or_registry"
    if str(getattr(spec, "function_name", "") or "").strip():
        return "function_route"
    if str(getattr(spec, "block_module", "") or "").strip():
        return "fallback_spec_or_component_manifest"
    return "unknown"


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        values = []
    result: List[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
