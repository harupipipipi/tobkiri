from __future__ import annotations

from collections import defaultdict
from typing import Any

from .ai_input_gates import evaluate_gate_config
from .ai_input_models import (
    AiInputEdge,
    AiInputNode,
    AiInputSegmentRegistry,
    EffectiveAiInput,
    PromptSegment,
    ToolSchemaSegment,
)


MODEL_INPUT_NODE_ID = "model_input:default"


def compile_effective_ai_input(
    *,
    profile_id: str,
    nodes: list[AiInputNode],
    edges: list[AiInputEdge],
    segments: AiInputSegmentRegistry,
    policy: dict[str, Any],
    ai_input_config: dict[str, Any],
    request_context: dict[str, Any] | None = None,
) -> EffectiveAiInput:
    context = dict(request_context or {})
    disabled_edge_ids = set(ai_input_config.get("disabled_edges") or [])
    raw_gates = ai_input_config.get("gates")
    gates: dict[str, dict[str, Any]] = raw_gates if isinstance(raw_gates, dict) else {}
    raw_budgets = ai_input_config.get("budgets")
    budgets: dict[str, Any] = raw_budgets if isinstance(raw_budgets, dict) else {}
    node_by_id = {node.id: node for node in nodes}
    graph_edges = _apply_disabled_edges(edges, disabled_edge_ids, segments)
    outgoing: dict[str, list[AiInputEdge]] = defaultdict(list)
    for edge in graph_edges:
        outgoing[edge.from_id].append(edge)

    diagnostics: list[dict[str, Any]] = []
    gate_decisions: dict[str, dict[str, Any]] = {}
    disabled_segments: list[dict[str, Any]] = []
    system_segments: list[PromptSegment] = []
    context_segments: list[PromptSegment] = []
    tool_schemas: list[ToolSchemaSegment] = []
    policy_segments: list[PromptSegment] = []

    for node_id, segment in _all_segments(segments).items():
        target_port = _target_port_for_segment(segment)
        path = _active_path_to_model_input(
            node_id,
            target_port,
            outgoing,
            node_by_id,
            gates,
            context,
            gate_decisions,
        )
        if not segment.enabled:
            disabled_segments.append(_disabled_segment_payload(segment, "policy_disabled"))
            continue
        if path is None:
            disabled_segments.append(_disabled_segment_payload(segment, "edge_disabled_or_gate_blocked"))
            continue
        if isinstance(segment, ToolSchemaSegment):
            tool_schemas.append(segment)
        elif target_port == "policy":
            policy_segments.append(segment)
        elif target_port == "context":
            context_segments.append(segment)
        else:
            system_segments.append(segment)

    system_segments = _apply_prompt_budget(system_segments, budgets.get("system"), disabled_segments, diagnostics)
    tool_schemas = _apply_tool_budget(tool_schemas, budgets.get("tools"), disabled_segments, diagnostics)

    token_estimate = _summarize_tokens(
        system_segments=system_segments,
        context_segments=context_segments,
        tool_schemas=tool_schemas,
        policy_segments=policy_segments,
    )
    graph = {
        "nodes": [node.to_dict() for node in nodes],
        "edges": [edge.to_dict() for edge in graph_edges],
    }
    return EffectiveAiInput(
        profile_id=profile_id,
        model_node_id=MODEL_INPUT_NODE_ID,
        system_segments=sorted(system_segments, key=lambda item: (item.priority, item.id)),
        context_segments=sorted(context_segments, key=lambda item: (item.priority, item.id)),
        tool_schemas=sorted(tool_schemas, key=lambda item: (item.name, item.id)),
        policy={**policy, "segments": [segment.to_dict(include_text=False) for segment in policy_segments]},
        token_estimate=token_estimate,
        graph=graph,
        disabled_segments=disabled_segments,
        diagnostics=diagnostics,
        gate_decisions=list(gate_decisions.values()),
    )


def _apply_disabled_edges(
    edges: list[AiInputEdge],
    disabled_edge_ids: set[str],
    segments: AiInputSegmentRegistry,
) -> list[AiInputEdge]:
    output: list[AiInputEdge] = []
    for edge in edges:
        if edge.id not in disabled_edge_ids:
            output.append(edge)
            continue
        segment = segments.segment_for_node(edge.from_id)
        allow_disable = True
        if segment is not None:
            allow_disable = bool(segment.metadata.get("allow_disable", True))
        output.append(
            AiInputEdge(
                id=edge.id,
                from_id=edge.from_id,
                from_port=edge.from_port,
                to_id=edge.to_id,
                to_port=edge.to_port,
                kind=edge.kind,
                active=edge.active and not allow_disable,
                gate_id=edge.gate_id,
                metadata={
                    **dict(edge.metadata),
                    "disabled_by_ai_input": allow_disable,
                    "disable_rejected": not allow_disable,
                },
            )
        )
    return output


def _active_path_to_model_input(
    node_id: str,
    target_port: str,
    outgoing: dict[str, list[AiInputEdge]],
    node_by_id: dict[str, AiInputNode],
    gates: dict[str, dict[str, Any]],
    context: dict[str, Any],
    gate_decisions: dict[str, dict[str, Any]],
) -> list[str] | None:
    stack: list[tuple[str, list[str]]] = [(node_id, [])]
    visited: set[str] = set()
    while stack:
        current, path = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for edge in outgoing.get(current, []):
            if not edge.active:
                continue
            if edge.gate_id and not _gate_allows(edge.gate_id, gates, context, gate_decisions):
                continue
            next_node = node_by_id.get(edge.to_id)
            if next_node and next_node.kind.endswith("_gate"):
                gate_id = next_node.id
                if not _gate_allows(gate_id, gates, context, gate_decisions):
                    continue
            next_path = [*path, edge.id]
            if edge.to_id == MODEL_INPUT_NODE_ID and edge.to_port == target_port:
                return next_path
            stack.append((edge.to_id, next_path))
    return None


def _gate_allows(
    gate_id: str,
    gates: dict[str, dict[str, Any]],
    context: dict[str, Any],
    gate_decisions: dict[str, dict[str, Any]],
) -> bool:
    config = gates.get(gate_id) or gates.get(gate_id.removeprefix("gate:"))
    if not isinstance(config, dict):
        gate_decisions.setdefault(
            gate_id,
            {"gate_id": gate_id, "decision": False, "reason": "missing_gate_config", "kind": "condition_gate"},
        )
        return False
    decision = evaluate_gate_config(gate_id, config, context).to_dict()
    gate_decisions[gate_id] = decision
    return bool(decision.get("decision"))


def _all_segments(
    segments: AiInputSegmentRegistry,
) -> dict[str, PromptSegment | ToolSchemaSegment]:
    return {
        **segments.prompt_segments,
        **segments.tool_schemas,
        **segments.policy_segments,
    }


def _target_port_for_segment(segment: PromptSegment | ToolSchemaSegment) -> str:
    if isinstance(segment, ToolSchemaSegment):
        return "tools"
    if segment.source_type in {"memory_source", "retrieval_source"}:
        return "context"
    if segment.source_type in {"profile_policy", "api_route"} or segment.id.startswith("policy:"):
        return "policy"
    return "system"


def _disabled_segment_payload(segment: PromptSegment | ToolSchemaSegment, reason: str) -> dict[str, Any]:
    payload = {
        "id": segment.id,
        "tokens": int(segment.tokens),
        "reason": reason if not segment.reason else segment.reason,
        "source_type": getattr(segment, "source_type", "tool_schema"),
        "metadata": dict(segment.metadata),
    }
    if isinstance(segment, ToolSchemaSegment):
        payload["tool_id"] = segment.tool_id
    return payload


def _apply_prompt_budget(
    segments: list[PromptSegment],
    budget: Any,
    disabled_segments: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> list[PromptSegment]:
    if not isinstance(budget, dict):
        return segments
    max_tokens = budget.get("max_tokens")
    if max_tokens is None:
        return segments
    try:
        max_tokens_int = int(max_tokens)
    except (TypeError, ValueError):
        return segments
    if max_tokens_int <= 0:
        return segments
    kept: list[PromptSegment] = []
    total = 0
    for segment in sorted(segments, key=lambda item: (item.priority, item.id)):
        if total + segment.tokens > max_tokens_int and kept:
            disabled_segments.append(_disabled_segment_payload(segment, "budget_exceeded"))
            continue
        kept.append(segment)
        total += segment.tokens
    if len(kept) != len(segments):
        diagnostics.append(
            {
                "severity": "info",
                "code": "system_budget_applied",
                "message": f"System prompt budget kept {len(kept)} of {len(segments)} segments.",
            }
        )
    return kept


def _apply_tool_budget(
    segments: list[ToolSchemaSegment],
    budget: Any,
    disabled_segments: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> list[ToolSchemaSegment]:
    if not isinstance(budget, dict):
        return segments
    max_tokens = budget.get("max_tokens")
    if max_tokens is None:
        return segments
    try:
        max_tokens_int = int(max_tokens)
    except (TypeError, ValueError):
        return segments
    if max_tokens_int <= 0:
        return segments
    kept: list[ToolSchemaSegment] = []
    total = 0
    for segment in sorted(segments, key=lambda item: (item.tokens, item.name)):
        if total + segment.tokens > max_tokens_int and kept:
            disabled_segments.append(_disabled_segment_payload(segment, "budget_exceeded"))
            continue
        kept.append(segment)
        total += segment.tokens
    if len(kept) != len(segments):
        diagnostics.append(
            {
                "severity": "info",
                "code": "tool_schema_budget_applied",
                "message": f"Tool schema budget kept {len(kept)} of {len(segments)} schemas.",
            }
        )
    return kept


def _summarize_tokens(
    *,
    system_segments: list[PromptSegment],
    context_segments: list[PromptSegment],
    tool_schemas: list[ToolSchemaSegment],
    policy_segments: list[PromptSegment],
) -> dict[str, Any]:
    by_port = {
        "system": sum(segment.tokens for segment in system_segments),
        "context": sum(segment.tokens for segment in context_segments),
        "tools": sum(segment.tokens for segment in tool_schemas),
        "policy": sum(segment.tokens for segment in policy_segments),
    }
    by_node: dict[str, int] = {}
    for segment in [*system_segments, *context_segments, *policy_segments]:
        by_node[segment.id] = int(segment.tokens)
    for tool_schema in tool_schemas:
        by_node[tool_schema.id] = int(tool_schema.tokens)
    return {
        "total": sum(by_port.values()),
        "by_port": by_port,
        "by_node": dict(sorted(by_node.items(), key=lambda item: item[1], reverse=True)),
    }
