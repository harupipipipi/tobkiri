from __future__ import annotations

import copy
from importlib import import_module
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml  # type: ignore[import-untyped]

from .profile_graph_models import (
    ProfileGraphDocument,
    ProfileGraphEdge,
    ProfileGraphNode,
    empty_profile_graph_document,
    normalize_profile_graph_document,
    normalize_profile_graph_selected,
)
from .profile_workspace import ProfileWorkspaceManager
from .profile_runtime_selection import apply_profile_graph_selection

_DEFAULTSPACK_IMPORT_ROOT = Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"
if str(_DEFAULTSPACK_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEFAULTSPACK_IMPORT_ROOT))

CapabilityCatalog = import_module(
    "ecosystem.defaultspack.domain.capability.catalog"
).CapabilityCatalog
InputProfileRegistry = import_module(
    "ecosystem.defaultspack.domain.external.input_profile_registry"
).InputProfileRegistry
FrontendRegistry = import_module(
    "ecosystem.defaultspack.domain.frontend.registry"
).FrontendRegistry
resolve_effective_prompt = import_module(
    "ecosystem.defaultspack.domain.prompt.effective"
).resolve_effective_prompt
ToolRegistry = import_module(
    "ecosystem.defaultspack.domain.tool.catalog_contract_client"
).ContractToolCatalog
WebhookEndpointStore = import_module(
    "ecosystem.defaultspack.domain.webhook.endpoint_store"
).WebhookEndpointStore
_transport_registry = import_module("ecosystem.defaultspack.transport.registry")
HttpRouteSpec = _transport_registry.HttpRouteSpec
canonical_http_route_specs = _transport_registry.canonical_http_route_specs


def build_startup_profile_graph_response(
    profile: Dict[str, Any],
    *,
    startup_catalog: Dict[str, Any] | None = None,
    profile_workspace_manager: ProfileWorkspaceManager | None = None,
    ecosystem_dir: str | None = None,
) -> Dict[str, Any]:
    normalized_profile = apply_profile_graph_selection(profile)
    profile_id = str(normalized_profile.get("profile_id") or "").strip()
    metadata = normalized_profile.get("metadata") if isinstance(normalized_profile.get("metadata"), dict) else {}
    document, document_diagnostics = normalize_profile_graph_document(
        profile_id,
        metadata.get("profile_graph"),
        metadata.get("selected"),
    )
    available = _available_catalog(
        normalized_profile,
        startup_catalog=startup_catalog,
        profile_workspace_manager=profile_workspace_manager,
        ecosystem_dir=ecosystem_dir,
    )
    hydrated_graph, hydration_diagnostics = _hydrate_graph_document(
        normalized_profile,
        document,
        available,
        ecosystem_dir=ecosystem_dir,
    )
    diagnostics = [*document_diagnostics, *hydration_diagnostics]
    diagnostics.extend(_selection_diagnostics(normalized_profile, available))

    return {
        "profile_id": profile_id,
        "profile": copy.deepcopy(normalized_profile),
        "graph": hydrated_graph.to_dict(),
        "available": available,
        "summary": {
            "selected_tool_count": len(hydrated_graph.selected.get("tools") or []),
            "available_tool_count": len(available.get("tools") or []),
            "selected_webhook_count": len(hydrated_graph.selected.get("webhooks") or []),
            "available_webhook_count": len(available.get("webhooks") or []),
            "api_route_count": len(available.get("api_routes") or []),
            "selected_frontend_count": len(hydrated_graph.selected.get("frontend") or []),
            "selected_prompt_count": len(hydrated_graph.selected.get("prompts") or []),
        },
        "ai_input": {
            "available": True,
            "endpoint": f"/api/panel/startup/profiles/{profile_id}/ai-input",
        },
        "diagnostics": diagnostics,
    }


def build_profile_graph_runtime_preview(
    profile: Dict[str, Any],
    *,
    available: Dict[str, Any] | None = None,
    profile_workspace_manager: ProfileWorkspaceManager | None = None,
    ecosystem_dir: str | None = None,
) -> Dict[str, Any]:
    normalized_profile = apply_profile_graph_selection(profile)
    metadata = normalized_profile.get("metadata") if isinstance(normalized_profile.get("metadata"), dict) else {}
    selected = normalize_profile_graph_selected(metadata.get("selected"))
    policy = normalized_profile.get("policy") if isinstance(normalized_profile.get("policy"), dict) else {}
    workspace_manager = profile_workspace_manager or ProfileWorkspaceManager()
    available_catalog = available or _available_catalog(
        normalized_profile,
        startup_catalog=None,
        profile_workspace_manager=workspace_manager,
        ecosystem_dir=ecosystem_dir,
    )

    prompt_resolution = _prompt_resolution_preview(normalized_profile, workspace_manager)
    frontend_preview = _selected_items_with_catalog(selected.get("frontend"), available_catalog.get("frontend"), "id")
    webhook_preview = _webhook_runtime_preview(selected.get("webhooks"), available_catalog.get("webhooks"))
    api_preview = _selected_items_with_catalog(selected.get("api_routes"), available_catalog.get("api_routes"), "id")
    tool_preview = _tool_selection_preview(selected.get("tools"), available_catalog.get("tools"))

    diagnostics: List[Dict[str, Any]] = []
    if (policy.get("api_route_allowlist") or selected.get("api_routes")) and not policy.get("enforce_api_route_allowlist"):
        diagnostics.append(
            _diagnostic(
                "info",
                "api_route_allowlist_not_enforced",
                "API route allowlist is stored on the profile, but strict enforcement is disabled.",
            )
        )

    return {
        "selected": selected,
        "policy": copy.deepcopy(policy),
        "tool_filter_result": tool_preview,
        "prompt_resolution": prompt_resolution,
        "webhook_status": list(webhook_preview.get("effective") or []),
        "webhook_runtime": webhook_preview,
        "api_route_policy": {
            "allowlist": list(policy.get("api_route_allowlist") or []),
            "enforce": bool(policy.get("enforce_api_route_allowlist", False)),
            "selected_routes": api_preview,
        },
        "frontend_selection": frontend_preview,
        "diagnostics": diagnostics,
    }


def _available_catalog(
    profile: Dict[str, Any],
    *,
    startup_catalog: Dict[str, Any] | None,
    profile_workspace_manager: ProfileWorkspaceManager | None,
    ecosystem_dir: str | None,
) -> Dict[str, Any]:
    workspace_manager = profile_workspace_manager or ProfileWorkspaceManager()
    defaultspack_root = _defaultspack_root(ecosystem_dir)
    tool_registry = ToolRegistry()
    tools = sorted(tool_registry.list_tools(), key=lambda item: str(item.get("tool_id") or item.get("name") or ""))
    webhooks = WebhookEndpointStore(defaultspack_root / "user_data" / "shared" / "webhooks" / "endpoints.json").list_endpoints()
    input_profiles = InputProfileRegistry(defaultspack_root).list_profiles()
    frontend_catalog = FrontendRegistry(defaultspack_root).build_catalog()
    capability_catalog = CapabilityCatalog(defaultspack_root)
    startup_nodes = _startup_catalog_nodes(startup_catalog)

    return {
        "tools": [_tool_candidate(tool) for tool in tools],
        "webhooks": [_webhook_candidate(endpoint) for endpoint in webhooks],
        "api_routes": [_api_route_candidate(spec) for spec in canonical_http_route_specs(include_always_available=True)],
        "prompts": _prompt_candidates(profile, capability_catalog, workspace_manager, defaultspack_root),
        "frontend": _frontend_candidates(frontend_catalog),
        "flows": _flow_candidates(ecosystem_dir),
        "capability_nodes": startup_nodes,
        "input_profiles": [_input_profile_candidate(profile_item) for profile_item in input_profiles],
    }


def _hydrate_graph_document(
    profile: Dict[str, Any],
    document: ProfileGraphDocument,
    available: Dict[str, Any],
    *,
    ecosystem_dir: str | None,
) -> Tuple[ProfileGraphDocument, List[Dict[str, Any]]]:
    diagnostics: List[Dict[str, Any]] = []
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return empty_profile_graph_document(""), diagnostics

    nodes = {node.id: node for node in document.nodes}
    edges = {edge.id: edge for edge in document.edges}

    profile_node = ProfileGraphNode(
        id=f"profile:{profile_id}",
        kind="profile",
        label=str(profile.get("name") or profile_id),
        ref=profile_id,
        metadata={"profile_id": profile_id, "base_pack": str(profile.get("base_pack") or "")},
    )
    nodes[profile_node.id] = profile_node

    available_maps = {
        "tools": _candidate_map(available.get("tools"), "id"),
        "webhooks": _candidate_map(available.get("webhooks"), "id"),
        "api_routes": _candidate_map(available.get("api_routes"), "id"),
        "prompts": _candidate_map(available.get("prompts"), "id"),
        "frontend": _candidate_map(available.get("frontend"), "id"),
        "flows": _candidate_map(available.get("flows"), "id"),
        "nodes": _candidate_map(available.get("capability_nodes"), "id"),
        "input_profiles": _candidate_map(available.get("input_profiles"), "id"),
    }

    selected = document.selected
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("tools"),
        "tool",
        "selects",
        available_maps["tools"],
        nodes,
        edges,
        diagnostics,
    )
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("webhooks"),
        "webhook",
        "receives_from",
        available_maps["webhooks"],
        nodes,
        edges,
        diagnostics,
    )
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("api_routes"),
        "api",
        "allows_api",
        available_maps["api_routes"],
        nodes,
        edges,
        diagnostics,
    )
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("prompts"),
        "prompt",
        "uses_prompt",
        available_maps["prompts"],
        nodes,
        edges,
        diagnostics,
    )
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("frontend"),
        "frontend",
        "uses_frontend",
        available_maps["frontend"],
        nodes,
        edges,
        diagnostics,
    )
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("flows"),
        "flow",
        "launches_flow",
        available_maps["flows"],
        nodes,
        edges,
        diagnostics,
    )
    _ensure_selected_nodes_and_edges(
        profile_node,
        selected.get("nodes"),
        "node",
        "uses_node",
        available_maps["nodes"],
        nodes,
        edges,
        diagnostics,
    )

    _ensure_tool_execution_edges(nodes, edges, available_maps["tools"])
    _ensure_webhook_edges(nodes, edges, available_maps["webhooks"], available_maps["input_profiles"])
    _ensure_api_route_edges(nodes, edges, available_maps["api_routes"])
    _ensure_prompt_storage_edges(nodes, edges, available_maps["prompts"])

    capability_graph_id = str(profile.get("default_graph") or profile.get("graph_id") or "").strip()
    if capability_graph_id and bool(profile.get("launch_capability_graph")):
        graph_node = ProfileGraphNode(
            id=f"flow:{capability_graph_id}",
            kind="capability_graph",
            label=capability_graph_id,
            ref=capability_graph_id,
            metadata={"graph_id": capability_graph_id},
        )
        nodes[graph_node.id] = graph_node
        edge = ProfileGraphEdge(
            id=_edge_id(profile_node.id, graph_node.id, "launches_graph"),
            from_id=profile_node.id,
            to_id=graph_node.id,
            kind="launches_graph",
            metadata={"graph_id": capability_graph_id},
        )
        edges[edge.id] = edge

    hydrated = ProfileGraphDocument(
        version=document.version,
        profile_id=profile_id,
        nodes=sorted(nodes.values(), key=lambda item: (item.kind, item.label or item.ref or item.id)),
        edges=sorted(edges.values(), key=lambda item: (item.kind, item.from_id, item.to_id)),
        selected=document.selected,
    )
    return hydrated, diagnostics


def _ensure_selected_nodes_and_edges(
    profile_node: ProfileGraphNode,
    selected_values: Any,
    kind_prefix: str,
    edge_kind: str,
    available_map: Dict[str, Dict[str, Any]],
    nodes: Dict[str, ProfileGraphNode],
    edges: Dict[str, ProfileGraphEdge],
    diagnostics: List[Dict[str, Any]],
) -> None:
    for item_id in _string_list(selected_values):
        candidate = available_map.get(item_id, {})
        label = str(candidate.get("label") or candidate.get("name") or item_id)
        node_kind = str(candidate.get("kind") or kind_prefix)
        node = ProfileGraphNode(
            id=f"{kind_prefix}:{item_id}",
            kind=node_kind,
            label=label,
            ref=item_id,
            metadata=dict(candidate),
        )
        nodes[node.id] = node
        edge = ProfileGraphEdge(
            id=_edge_id(profile_node.id, node.id, edge_kind),
            from_id=profile_node.id,
            to_id=node.id,
            kind=edge_kind,
            metadata={"selected_ref": item_id},
        )
        edges[edge.id] = edge
        if not candidate:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "profile_graph_selection_missing",
                    f"Selected {kind_prefix} '{item_id}' is not in the current catalog.",
                )
            )


def _ensure_tool_execution_edges(
    nodes: Dict[str, ProfileGraphNode],
    edges: Dict[str, ProfileGraphEdge],
    tool_map: Dict[str, Dict[str, Any]],
) -> None:
    for tool_id, candidate in tool_map.items():
        tool_node_id = f"tool:{tool_id}"
        if tool_node_id not in nodes:
            continue
        handler = str(candidate.get("handler") or "").strip()
        if not handler:
            continue
        handler_node = ProfileGraphNode(
            id=f"node:{handler}",
            kind="handler",
            label=handler.rsplit(":", 1)[-1],
            ref=handler,
            metadata={"handler": handler, "source": candidate.get("path")},
        )
        nodes[handler_node.id] = handler_node
        edges[_edge_id(tool_node_id, handler_node.id, "executes")] = ProfileGraphEdge(
            id=_edge_id(tool_node_id, handler_node.id, "executes"),
            from_id=tool_node_id,
            to_id=handler_node.id,
            kind="executes",
            metadata={"handler": handler},
        )


def _ensure_webhook_edges(
    nodes: Dict[str, ProfileGraphNode],
    edges: Dict[str, ProfileGraphEdge],
    webhook_map: Dict[str, Dict[str, Any]],
    input_profile_map: Dict[str, Dict[str, Any]],
) -> None:
    for webhook_id, candidate in webhook_map.items():
        webhook_node_id = f"webhook:{webhook_id}"
        if webhook_node_id not in nodes:
            continue
        input_profile_id = str(candidate.get("input_profile_id") or "").strip()
        if input_profile_id:
            input_profile = input_profile_map.get(input_profile_id, {})
            input_node = ProfileGraphNode(
                id=f"node:{input_profile_id}",
                kind="input_profile",
                label=str(input_profile.get("label") or input_profile_id),
                ref=input_profile_id,
                metadata=dict(input_profile),
            )
            nodes[input_node.id] = input_node
            edges[_edge_id(webhook_node_id, input_node.id, "uses_input_profile")] = ProfileGraphEdge(
                id=_edge_id(webhook_node_id, input_node.id, "uses_input_profile"),
                from_id=webhook_node_id,
                to_id=input_node.id,
                kind="uses_input_profile",
                metadata={"input_profile_id": input_profile_id},
            )
        delivery = candidate.get("default_delivery") if isinstance(candidate.get("default_delivery"), dict) else {}
        action_id = str(delivery.get("action_id") or "").strip()
        if action_id:
            action_node = ProfileGraphNode(
                id=f"node:{action_id}",
                kind="delivery_action",
                label=action_id,
                ref=action_id,
                metadata={"action_id": action_id},
            )
            nodes[action_node.id] = action_node
            edges[_edge_id(webhook_node_id, action_node.id, "delivers_to")] = ProfileGraphEdge(
                id=_edge_id(webhook_node_id, action_node.id, "delivers_to"),
                from_id=webhook_node_id,
                to_id=action_node.id,
                kind="delivers_to",
                metadata={"action_id": action_id},
            )


def _ensure_api_route_edges(
    nodes: Dict[str, ProfileGraphNode],
    edges: Dict[str, ProfileGraphEdge],
    api_route_map: Dict[str, Dict[str, Any]],
) -> None:
    for route_id, candidate in api_route_map.items():
        api_node_id = f"api:{route_id}"
        if api_node_id not in nodes:
            continue
        flow_id = str(candidate.get("flow_id") or "").strip()
        if flow_id:
            flow_node = ProfileGraphNode(
                id=f"flow:{flow_id}",
                kind="flow",
                label=flow_id,
                ref=flow_id,
                metadata={"flow_id": flow_id},
            )
            nodes[flow_node.id] = flow_node
            edges[_edge_id(api_node_id, flow_node.id, "handled_by")] = ProfileGraphEdge(
                id=_edge_id(api_node_id, flow_node.id, "handled_by"),
                from_id=api_node_id,
                to_id=flow_node.id,
                kind="handled_by",
                metadata={"flow_id": flow_id},
            )
        block_module = str(candidate.get("block_module") or candidate.get("fallback_block_module") or "").strip()
        if block_module:
            block_node = ProfileGraphNode(
                id=f"node:{block_module}",
                kind="block",
                label=block_module.rsplit(".", 1)[-1],
                ref=block_module,
                metadata={"block_module": block_module},
            )
            nodes[block_node.id] = block_node
            edges[_edge_id(api_node_id, block_node.id, "handled_by")] = ProfileGraphEdge(
                id=_edge_id(api_node_id, block_node.id, "handled_by"),
                from_id=api_node_id,
                to_id=block_node.id,
                kind="handled_by",
                metadata={"block_module": block_module},
            )
        function_name = str(candidate.get("function_name") or "").strip()
        if function_name:
            function_node = ProfileGraphNode(
                id=f"node:{function_name}",
                kind="function",
                label=function_name.rsplit(".", 1)[-1],
                ref=function_name,
                metadata={"function_name": function_name},
            )
            nodes[function_node.id] = function_node
            edges[_edge_id(api_node_id, function_node.id, "handled_by")] = ProfileGraphEdge(
                id=_edge_id(api_node_id, function_node.id, "handled_by"),
                from_id=api_node_id,
                to_id=function_node.id,
                kind="handled_by",
                metadata={"function_name": function_name},
            )


def _ensure_prompt_storage_edges(
    nodes: Dict[str, ProfileGraphNode],
    edges: Dict[str, ProfileGraphEdge],
    prompt_map: Dict[str, Dict[str, Any]],
) -> None:
    for prompt_id, candidate in prompt_map.items():
        prompt_node_id = f"prompt:{prompt_id}"
        if prompt_node_id not in nodes:
            continue
        source_path = str(candidate.get("path") or "").strip()
        if not source_path:
            continue
        storage_node = ProfileGraphNode(
            id=f"storage:{source_path}",
            kind="storage",
            label=Path(source_path).name,
            ref=source_path,
            metadata={"path": source_path},
        )
        nodes[storage_node.id] = storage_node
        edges[_edge_id(prompt_node_id, storage_node.id, "reads_from")] = ProfileGraphEdge(
            id=_edge_id(prompt_node_id, storage_node.id, "reads_from"),
            from_id=prompt_node_id,
            to_id=storage_node.id,
            kind="reads_from",
            metadata={"path": source_path},
        )


def _selection_diagnostics(profile: Dict[str, Any], available: Dict[str, Any]) -> List[Dict[str, Any]]:
    diagnostics: List[Dict[str, Any]] = []
    policy = profile.get("policy") if isinstance(profile.get("policy"), dict) else {}
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    selected = normalize_profile_graph_selected(metadata.get("selected"))
    if (policy.get("api_route_allowlist") or selected.get("api_routes")) and not policy.get("enforce_api_route_allowlist"):
        diagnostics.append(
            _diagnostic(
                "info",
                "api_route_allowlist_not_enforced",
                "API route allowlist is configured, but enforce_api_route_allowlist is false.",
            )
        )
    available_ids = {
        "tools": set(_candidate_map(available.get("tools"), "id")),
        "webhooks": set(_candidate_map(available.get("webhooks"), "id")),
        "api_routes": set(_candidate_map(available.get("api_routes"), "id")),
        "prompts": set(_candidate_map(available.get("prompts"), "id")),
        "frontend": set(_candidate_map(available.get("frontend"), "id")),
        "flows": set(_candidate_map(available.get("flows"), "id")),
        "nodes": set(_candidate_map(available.get("capability_nodes"), "id")),
    }
    for category, ids in available_ids.items():
        for item_id in _string_list(selected.get(category)):
            if item_id not in ids:
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        f"profile_selected_{category}_missing",
                        f"Selected {category[:-1] if category.endswith('s') else category} '{item_id}' is not currently available.",
                    )
                )
    return diagnostics


def _tool_candidate(tool: Dict[str, Any]) -> Dict[str, Any]:
    execution = tool.get("execution") if isinstance(tool.get("execution"), dict) else {}
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip()
    return {
        "id": tool_id,
        "label": str(tool.get("display_name") or tool.get("name") or tool_id),
        "name": str(tool.get("name") or tool_id),
        "kind": "tool",
        "summary": str(tool.get("summary") or tool.get("description") or ""),
        "path": str(metadata.get("manifest_path") or ""),
        "handler": str(execution.get("handler") or ""),
        "execution_type": str(execution.get("type") or "local"),
        "source_pack_id": str(tool.get("source_pack_id") or metadata.get("source_pack_id") or ""),
        "risk": str(metadata.get("risk") or tool.get("risk") or "low"),
    }


def _webhook_candidate(endpoint: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(endpoint.get("id") or "").strip(),
        "label": str(endpoint.get("id") or endpoint.get("kind") or "webhook"),
        "kind": "webhook",
        "webhook_kind": str(endpoint.get("kind") or "generic"),
        "input_profile_id": str(endpoint.get("input_profile_id") or "").strip(),
        "default_delivery": dict(endpoint.get("default_delivery") if isinstance(endpoint.get("default_delivery"), dict) else {}),
        "enabled": bool(endpoint.get("enabled")),
        "public_url": dict(endpoint.get("public_url") if isinstance(endpoint.get("public_url"), dict) else {}),
        "metadata": dict(endpoint.get("metadata") if isinstance(endpoint.get("metadata"), dict) else {}),
    }


def _input_profile_candidate(profile: Any) -> Dict[str, Any]:
    return {
        "id": str(getattr(profile, "id", "")),
        "label": str(getattr(profile, "display_name", "") or getattr(profile, "id", "")),
        "kind": "input_profile",
        "provider": str(getattr(profile, "provider", "")),
    }


def _api_route_candidate(spec: HttpRouteSpec) -> Dict[str, Any]:
    route_id = f"{spec.method} {spec.pattern}"
    return {
        "id": route_id,
        "label": route_id,
        "kind": "api",
        "method": spec.method,
        "path": spec.pattern,
        "block_module": spec.block_module,
        "function_name": spec.function_name,
        "flow_id": spec.flow_id,
        "fallback_block_module": spec.fallback_block_module,
        "handler_name": spec.handler_name,
    }


def _prompt_candidates(
    profile: Dict[str, Any],
    capability_catalog: CapabilityCatalog,
    workspace_manager: ProfileWorkspaceManager,
    defaultspack_root: Path,
) -> List[Dict[str, Any]]:
    prompt_map: Dict[str, Dict[str, Any]] = {}
    for prompt in capability_catalog.prompts():
        prompt_id = str(prompt.get("id") or "").strip()
        if not prompt_id:
            continue
        content_ref = str(prompt.get("content_ref") or "").strip()
        prompt_map[prompt_id] = {
            "id": prompt_id,
            "label": str(prompt.get("name") or prompt_id),
            "kind": "prompt",
            "source_pack_id": str(prompt.get("source_pack_id") or ""),
            "path": str(defaultspack_root / content_ref) if content_ref else "",
            "preview": str(prompt.get("preview") or ""),
        }

    profile_id = str(profile.get("profile_id") or "").strip()
    if profile_id:
        paths = workspace_manager.paths_for_profile(profile_id)
        for prompt in _prompt_files(paths.prompts_dir):
            prompt_map[prompt["id"]] = prompt
        snapshots_root = paths.snapshots_dir / str(profile.get("base_pack") or "defaultspack") / "prompts"
        for prompt in _prompt_files(snapshots_root):
            prompt_map.setdefault(prompt["id"], prompt)
    return sorted(prompt_map.values(), key=lambda item: item["id"])


def _prompt_files(root: Path) -> List[Dict[str, Any]]:
    if not root.is_dir():
        return []
    prompts: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        prompt_id = _prompt_id_from_path(path)
        if not prompt_id:
            continue
        preview = ""
        try:
            preview = path.read_text(encoding="utf-8").strip().splitlines()[0]
        except OSError:
            preview = ""
        prompts[prompt_id] = {
            "id": prompt_id,
            "label": prompt_id,
            "kind": "prompt",
            "path": str(path),
            "preview": preview,
            "source_pack_id": "profile_workspace" if "profiles" in path.parts else "profile_snapshot",
        }
    return list(prompts.values())


def _prompt_id_from_path(path: Path) -> str:
    name = path.name
    if name == "prompt.md":
        return path.parent.name
    for suffix in (".system.md", ".prompt.md", ".md", ".txt"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return ""


def _frontend_candidates(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}

    def _add(entry: Dict[str, Any], source_kind: str) -> None:
        item_id = str(entry.get("id") or "").strip()
        if not item_id:
            return
        items[item_id] = {
            "id": item_id,
            "label": str(entry.get("label") or entry.get("title") or item_id),
            "kind": "frontend",
            "source_kind": source_kind,
            "profile_visibility": dict(entry.get("profile_visibility") if isinstance(entry.get("profile_visibility"), dict) else {}),
            "metadata": {
                key: value
                for key, value in entry.items()
                if key not in {"id", "label", "title"}
            },
        }

    shell = catalog.get("shell") if isinstance(catalog.get("shell"), dict) else {}
    layout = shell.get("layout") if isinstance(shell.get("layout"), dict) else {}
    for region in layout.get("regions") if isinstance(layout.get("regions"), list) else []:
        if isinstance(region, dict):
            _add(region, "shell_region")
    for renderer in shell.get("renderers") if isinstance(shell.get("renderers"), list) else []:
        if isinstance(renderer, dict):
            _add(renderer, "shell_renderer")
    for part in catalog.get("parts") if isinstance(catalog.get("parts"), list) else []:
        if isinstance(part, dict):
            _add(part, "part")
    sidebar = catalog.get("sidebar") if isinstance(catalog.get("sidebar"), dict) else {}
    for item in sidebar.get("items") if isinstance(sidebar.get("items"), list) else []:
        if isinstance(item, dict):
            _add(item, "sidebar_item")
    settings = catalog.get("settings") if isinstance(catalog.get("settings"), dict) else {}
    for section in settings.get("sections") if isinstance(settings.get("sections"), list) else []:
        if isinstance(section, dict):
            _add(section, "settings_section")
    chat_rendering = catalog.get("chat_rendering") if isinstance(catalog.get("chat_rendering"), dict) else {}
    for renderer in chat_rendering.get("renderers") if isinstance(chat_rendering.get("renderers"), list) else []:
        if isinstance(renderer, dict):
            _add(renderer, "chat_renderer")
    return sorted(items.values(), key=lambda item: (item["source_kind"], item["id"]))


def _flow_candidates(ecosystem_dir: str | None) -> List[Dict[str, Any]]:
    roots = _ecosystem_roots(ecosystem_dir)
    flows: Dict[str, Dict[str, Any]] = {}
    for pack_root in roots:
        flows_dir = pack_root / "flows"
        if not flows_dir.is_dir():
            continue
        for path in sorted(flows_dir.glob("*.flow.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
            flow_id = str(data.get("flow_id") or path.name[: -len(".flow.yaml")]).strip()
            if not flow_id:
                continue
            flows[flow_id] = {
                "id": flow_id,
                "label": str(data.get("name") or flow_id),
                "kind": "flow",
                "path": str(path),
                "source_pack_id": pack_root.name,
            }
    return sorted(flows.values(), key=lambda item: item["id"])


def _startup_catalog_nodes(startup_catalog: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    nodes: Dict[str, Dict[str, Any]] = {}
    catalog = startup_catalog if isinstance(startup_catalog, dict) else {}
    for pack in catalog.get("packs") if isinstance(catalog.get("packs"), list) else []:
        if not isinstance(pack, dict):
            continue
        pack_id = str(pack.get("pack_id") or "").strip()
        for node in pack.get("nodes") if isinstance(pack.get("nodes"), list) else []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("node_id") or node.get("ref") or "").strip()
            if not node_id:
                continue
            display_name = node.get("display_name") if isinstance(node.get("display_name"), dict) else {}
            label = str(display_name.get("en") or display_name.get("ja") or node.get("component_id") or node_id)
            node_metadata = dict(node.get("metadata") if isinstance(node.get("metadata"), dict) else {})
            launch = node_metadata.get("launch") if isinstance(node_metadata.get("launch"), dict) else {}
            nodes[node_id] = {
                "id": node_id,
                "label": label,
                "kind": "capability_node",
                "component_type": str(node.get("component_type") or node_metadata.get("component_type") or ""),
                "component_id": str(node.get("component_id") or node_metadata.get("component_id") or ""),
                "source_pack_id": pack_id,
                "ports": list(node.get("ports") if isinstance(node.get("ports"), list) else []),
                "launch": dict(launch),
                "metadata": node_metadata,
            }
    return sorted(nodes.values(), key=lambda item: item["id"])


def _prompt_resolution_preview(profile: Dict[str, Any], workspace_manager: ProfileWorkspaceManager) -> Dict[str, Any]:
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return {}
    workspace = workspace_manager.payload_for_profile(profile_id)
    try:
        return resolve_effective_prompt(
            {
                "profile_id": profile_id,
                "base_pack": str(profile.get("base_pack") or "defaultspack"),
                "system_prompt_id": profile.get("system_prompt_id"),
                "default_prompt_id": profile.get("default_prompt_id"),
                "workspace": workspace,
            }
        )
    except Exception as exc:
        return {
            "profile_id": profile_id,
            "prompt_id": str(profile.get("system_prompt_id") or profile.get("default_prompt_id") or ""),
            "source_type": "error",
            "source": "",
            "content": "",
            "diagnostic": str(exc),
        }


def _tool_selection_preview(selected_tools: Any, available_tools: Any) -> List[Dict[str, Any]]:
    selected = set(_string_list(selected_tools))
    entries: List[Dict[str, Any]] = []
    for candidate in available_tools if isinstance(available_tools, list) else []:
        if not isinstance(candidate, dict):
            continue
        tool_name = str(candidate.get("id") or "").strip()
        if not tool_name:
            continue
        if tool_name in selected:
            entries.append(
                {
                    "tool_name": tool_name,
                    "status": "allowed",
                    "reason_code": "profile_selected",
                }
            )
        elif selected:
            entries.append(
                {
                    "tool_name": tool_name,
                    "status": "blocked",
                    "reason_code": "not_selected_by_profile",
                }
            )
    return entries


def _selected_items_with_catalog(selected_ids: Any, catalog_items: Any, key: str) -> List[Dict[str, Any]]:
    catalog = _candidate_map(catalog_items, key)
    items: List[Dict[str, Any]] = []
    for item_id in _string_list(selected_ids):
        candidate = catalog.get(item_id)
        if candidate:
            items.append(copy.deepcopy(candidate))
        else:
            items.append({"id": item_id, "label": item_id, "missing": True})
    return items


def _webhook_runtime_preview(selected_ids: Any, catalog_items: Any) -> Dict[str, Any]:
    selected = _string_list(selected_ids)
    catalog = _candidate_map(catalog_items, "id")
    effective: List[Dict[str, Any]] = []
    for webhook_id in selected:
        candidate = catalog.get(webhook_id)
        if candidate:
            delivery = candidate.get("default_delivery") if isinstance(candidate.get("default_delivery"), dict) else {}
            effective.append(
                {
                    **copy.deepcopy(candidate),
                    "delivery_action": str(delivery.get("action_id") or ""),
                    "profile_selection_applied": True,
                }
            )
        else:
            effective.append(
                {
                    "id": webhook_id,
                    "label": webhook_id,
                    "missing": True,
                    "profile_selection_applied": True,
                }
            )
    return {
        "selected": selected,
        "effective": effective,
        "warning": "Endpoint is shared; profile selection does not disable unselected endpoints.",
    }


def _candidate_map(items: Any, key: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get(key) or "").strip()
        if item_id:
            result[item_id] = item
    return result


def _defaultspack_root(ecosystem_dir: str | None) -> Path:
    if ecosystem_dir:
        candidate = Path(ecosystem_dir) / "defaultspack"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"


def _ecosystem_roots(ecosystem_dir: str | None) -> List[Path]:
    root = Path(ecosystem_dir) if ecosystem_dir else Path(__file__).resolve().parents[1] / "ecosystem"
    if not root.is_dir():
        return [_defaultspack_root(ecosystem_dir)]
    return [path for path in sorted(root.iterdir()) if path.is_dir() and (path / "ecosystem.json").is_file()]


def _edge_id(from_id: str, to_id: str, kind: str) -> str:
    return f"{from_id}->{to_id}:{kind}"


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        return []
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _diagnostic(level: str, code: str, message: str) -> Dict[str, Any]:
    return {"level": level, "code": code, "message": message}
