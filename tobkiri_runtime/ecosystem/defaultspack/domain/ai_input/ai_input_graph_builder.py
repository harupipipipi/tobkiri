from __future__ import annotations

import copy
from typing import Any

from .ai_input_compiler import MODEL_INPUT_NODE_ID, compile_effective_ai_input
from .ai_input_models import (
    AiInputEdge,
    AiInputNode,
    AiInputSegmentRegistry,
    PromptSegment,
    ToolSchemaSegment,
    edge_from_dict,
    normalize_ai_input_config,
)
from .ai_input_segments import (
    collect_api_route_segments,
    collect_context_segments,
    collect_policy_segment,
    collect_prompt_segments,
    collect_tool_schema_segments,
)
from core_runtime.profile_workspace import ProfileWorkspaceManager


def build_ai_input_graph_response(
    profile: dict[str, Any],
    *,
    startup_catalog: dict[str, Any] | None = None,
    profile_workspace_manager: ProfileWorkspaceManager | None = None,
    ecosystem_dir: str | None = None,
    include_text: bool = True,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del startup_catalog, ecosystem_dir
    # The caller must pass a projection of the verified v4 activation.  Do not
    # reinterpret mutable Profile graph/YAML data into runtime permissions.
    normalized_profile = copy.deepcopy(profile)
    profile_id = str(normalized_profile.get("profile_id") or "").strip()
    raw_metadata = normalized_profile.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    ai_input_config = normalize_ai_input_config(metadata.get("ai_input"))
    manager = profile_workspace_manager or ProfileWorkspaceManager()

    prompt_segments = collect_prompt_segments(
        normalized_profile,
        workspace_manager=manager,
        include_text=include_text,
    )
    context_segments = collect_context_segments(
        normalized_profile,
        workspace_manager=manager,
        request_context=request_context,
    )
    api_route_segments = collect_api_route_segments(normalized_profile)
    tool_segments = collect_tool_schema_segments(normalized_profile)
    policy_segment = collect_policy_segment(normalized_profile)
    input_segments = [*prompt_segments, *context_segments, *api_route_segments]

    nodes, edges = _build_graph(
        profile_id=profile_id,
        prompt_segments=input_segments,
        tool_segments=tool_segments,
        policy_segment=policy_segment,
        ai_input_config=ai_input_config,
    )
    raw_policy = normalized_profile.get("policy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    registry = AiInputSegmentRegistry(
        prompt_segments={segment.id: segment for segment in input_segments},
        tool_schemas={segment.id: segment for segment in tool_segments},
        policy_segments={policy_segment.id: policy_segment},
    )
    effective = compile_effective_ai_input(
        profile_id=profile_id,
        nodes=nodes,
        edges=edges,
        segments=registry,
        policy=copy.deepcopy(policy),
        ai_input_config=ai_input_config,
        request_context=request_context,
    )
    return {
        "profile_id": profile_id,
        "profile": copy.deepcopy(normalized_profile),
        "ai_input": ai_input_config,
        "model_input": {
            "node_id": MODEL_INPUT_NODE_ID,
            "provider": _profile_provider(normalized_profile),
            "model": _profile_model(normalized_profile, request_context),
        },
        "graph": effective.graph,
        "effective_input": effective.to_dict(include_text=include_text),
        "token_estimate": effective.token_estimate,
        "gate_decisions": list(effective.gate_decisions),
        "diagnostics": list(effective.diagnostics),
    }


def build_runtime_ai_input_trace(
    profile: dict[str, Any],
    *,
    conversation_id: str = "",
    run_id: str = "",
    user_message: str = "",
    request_context: dict[str, Any] | None = None,
    include_text: bool = False,
) -> dict[str, Any]:
    context = {
        **dict(request_context or {}),
        "conversation_id": conversation_id,
        "run_id": run_id,
        "message": user_message,
        "user_text": user_message,
    }
    response = build_ai_input_graph_response(
        profile,
        include_text=include_text,
        request_context=context,
    )
    raw_effective = response.get("effective_input")
    effective: dict[str, Any] = raw_effective if isinstance(raw_effective, dict) else {}
    raw_tool_schemas = effective.get("tool_schemas")
    tool_schemas: list[Any] = raw_tool_schemas if isinstance(raw_tool_schemas, list) else []
    raw_context_segments = effective.get("context_segments")
    context_segments: list[Any] = (
        raw_context_segments if isinstance(raw_context_segments, list) else []
    )
    allowed_tool_ids = [
        str(item.get("tool_id") or item.get("name") or "").strip()
        for item in tool_schemas
        if isinstance(item, dict) and str(item.get("tool_id") or item.get("name") or "").strip()
    ]
    return {
        "trace_id": f"ait_{run_id or conversation_id or 'preview'}",
        "conversation_id": conversation_id,
        "run_id": run_id,
        "profile_id": response.get("profile_id"),
        "effective_input": effective,
        "token_estimate": response.get("token_estimate") if isinstance(response.get("token_estimate"), dict) else {},
        "allowed_tool_ids": allowed_tool_ids,
        "blocked": [],
        "graph": response.get("graph") if isinstance(response.get("graph"), dict) else {},
        "gate_decisions": response.get("gate_decisions") if isinstance(response.get("gate_decisions"), list) else [],
        "diagnostics": response.get("diagnostics") if isinstance(response.get("diagnostics"), list) else [],
        "provider_payload_summary": {
            "system_segment_count": len(
                value if isinstance((value := effective.get("system_segments")), list) else []
            ),
            "context_segment_count": len(context_segments),
            "tool_schema_count": len(tool_schemas),
        },
    }


def _build_graph(
    *,
    profile_id: str,
    prompt_segments: list[PromptSegment],
    tool_segments: list[ToolSchemaSegment],
    policy_segment: PromptSegment,
    ai_input_config: dict[str, Any],
) -> tuple[list[AiInputNode], list[AiInputEdge]]:
    nodes: dict[str, AiInputNode] = {
        MODEL_INPUT_NODE_ID: AiInputNode(
            id=MODEL_INPUT_NODE_ID,
            kind="model_input",
            label="Model Input",
            ref=profile_id,
            input_ports=["system", "developer", "user", "context", "tools", "policy", "metadata"],
            output_ports=["provider_payload"],
            metadata={"profile_id": profile_id},
        )
    }
    edges: dict[str, AiInputEdge] = {}

    for prompt_segment in prompt_segments:
        node_kind = _node_kind_for_prompt_segment(prompt_segment)
        output_port = _output_port_for_prompt_segment(prompt_segment)
        to_port = _target_port_for_prompt_segment(prompt_segment)
        nodes[prompt_segment.id] = AiInputNode(
            id=prompt_segment.id,
            kind=node_kind,
            label=_segment_label(prompt_segment),
            ref=str(
                prompt_segment.metadata.get("prompt_id")
                or prompt_segment.metadata.get("route_id")
                or ""
            ),
            output_ports=[output_port],
            metadata=prompt_segment.to_dict(include_text=False),
        )
        edge = _segment_edge(
            prompt_segment.id,
            to_port,
            _edge_kind_for_prompt_segment(prompt_segment),
            from_port=output_port,
            active=prompt_segment.enabled,
        )
        edges[edge.id] = edge

    for tool_segment in tool_segments:
        nodes[tool_segment.id] = AiInputNode(
            id=tool_segment.id,
            kind="tool_schema",
            label=tool_segment.name,
            ref=tool_segment.tool_id,
            output_ports=["schema"],
            metadata=tool_segment.to_dict(include_schema=False),
        )
        edge = _segment_edge(
            tool_segment.id,
            "tools",
            "provides_schema",
            from_port="schema",
            active=tool_segment.enabled,
        )
        edges[edge.id] = edge

    nodes[policy_segment.id] = AiInputNode(
        id=policy_segment.id,
        kind="profile_policy",
        label="Profile Policy",
        ref="profile.policy",
        output_ports=["rules"],
        metadata=policy_segment.to_dict(include_text=False),
    )
    policy_edge = _segment_edge(policy_segment.id, "policy", "provides_policy", from_port="rules")
    edges[policy_edge.id] = policy_edge

    raw_gates = ai_input_config.get("gates")
    gates: dict[str, Any] = raw_gates if isinstance(raw_gates, dict) else {}
    for gate_id, gate in gates.items():
        if not isinstance(gate, dict):
            continue
        gate_node_id = str(gate.get("id") or gate_id)
        nodes[gate_node_id] = AiInputNode(
            id=gate_node_id,
            kind=str(gate.get("kind") or "condition_gate"),
            label=str(gate.get("label") or gate_node_id),
            ref=gate_node_id,
            input_ports=["input"],
            output_ports=["pass", "block"],
            metadata=dict(gate),
        )

    for raw_edge in ai_input_config.get("inserted_edges") or []:
        if not isinstance(raw_edge, dict):
            continue
        inserted_edge = edge_from_dict(raw_edge)
        if inserted_edge is not None:
            edges[inserted_edge.id] = inserted_edge

    return (
        sorted(nodes.values(), key=lambda item: (item.kind, item.label, item.id)),
        sorted(edges.values(), key=lambda item: (item.kind, item.from_id, item.to_id, item.id)),
    )


def _segment_edge(
    from_id: str,
    to_port: str,
    kind: str,
    *,
    from_port: str = "output",
    active: bool = True,
) -> AiInputEdge:
    edge_id = f"edge:{from_id}->{MODEL_INPUT_NODE_ID}.{to_port}"
    return AiInputEdge(
        id=edge_id,
        from_id=from_id,
        from_port=from_port,
        to_id=MODEL_INPUT_NODE_ID,
        to_port=to_port,
        kind=kind,
        active=active,
        metadata={"generated": True},
    )


def _node_kind_for_prompt_segment(segment: PromptSegment) -> str:
    if segment.source_type in {"memory_source", "retrieval_source"}:
        return segment.source_type
    if segment.source_type == "api_route":
        return "api_route"
    return "prompt_segment"


def _output_port_for_prompt_segment(segment: PromptSegment) -> str:
    if segment.source_type in {"memory_source", "retrieval_source"}:
        return "context"
    if segment.source_type == "api_route":
        return "route"
    return "output"


def _target_port_for_prompt_segment(segment: PromptSegment) -> str:
    if segment.source_type in {"memory_source", "retrieval_source"}:
        return "context"
    if segment.source_type == "api_route":
        return "policy"
    return "system"


def _edge_kind_for_prompt_segment(segment: PromptSegment) -> str:
    if segment.source_type in {"memory_source", "retrieval_source"}:
        return "provides_context"
    if segment.source_type == "api_route":
        return "provides_policy"
    return "contributes_to"


def _segment_label(segment: PromptSegment) -> str:
    if segment.source_type == "api_route":
        return str(segment.metadata.get("route_id") or segment.id.removeprefix("api_route:"))
    if segment.source_type in {"memory_source", "retrieval_source"}:
        return str(segment.metadata.get("source_kind") or segment.id.removeprefix("memory:"))
    return str(segment.metadata.get("prompt_id") or segment.id.removeprefix("prompt:"))


def _profile_provider(profile: dict[str, Any]) -> str | None:
    model = _profile_model(profile, None)
    if model and "/" in model:
        return model.split("/", 1)[0]
    return None


def _profile_model(profile: dict[str, Any], request_context: dict[str, Any] | None) -> str | None:
    context = request_context if isinstance(request_context, dict) else {}
    raw_params = context.get("chat_params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    model = params.get("model") or context.get("model") or profile.get("model")
    return str(model).strip() if isinstance(model, str) and model.strip() else None
