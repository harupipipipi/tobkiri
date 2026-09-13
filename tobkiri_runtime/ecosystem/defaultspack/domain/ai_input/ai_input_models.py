from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


AI_INPUT_VERSION = 1

AiInputPort = Literal[
    "system",
    "developer",
    "user",
    "context",
    "tools",
    "policy",
    "metadata",
]

AiInputNodeKind = Literal[
    "model_input",
    "prompt_segment",
    "tool_schema",
    "memory_source",
    "retrieval_source",
    "api_route",
    "frontend_instruction",
    "profile_policy",
    "condition_gate",
    "vector_gate",
    "budget_gate",
    "merge",
]


@dataclass(frozen=True)
class AiInputNode:
    id: str
    kind: str
    label: str
    ref: str = ""
    input_ports: list[str] = field(default_factory=list)
    output_ports: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "ref": self.ref,
            "input_ports": list(self.input_ports),
            "output_ports": list(self.output_ports),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AiInputEdge:
    id: str
    from_id: str
    from_port: str
    to_id: str
    to_port: str
    kind: str
    active: bool = True
    gate_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "from_id": self.from_id,
            "from_port": self.from_port,
            "to_id": self.to_id,
            "to_port": self.to_port,
            "kind": self.kind,
            "active": self.active,
            "gate_id": self.gate_id,
            "metadata": dict(self.metadata),
        }
        return payload


@dataclass(frozen=True)
class PromptSegment:
    id: str
    text: str
    source: str
    source_type: str
    tokens: int
    priority: int = 100
    enabled: bool = True
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "source_type": self.source_type,
            "tokens": int(self.tokens),
            "priority": int(self.priority),
            "enabled": bool(self.enabled),
            "reason": self.reason,
            "metadata": dict(self.metadata),
            "preview": _preview_text(self.text),
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass(frozen=True)
class ToolSchemaSegment:
    id: str
    tool_id: str
    name: str
    schema: dict[str, Any]
    tokens: int
    enabled: bool = True
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_schema: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "tool_id": self.tool_id,
            "name": self.name,
            "tokens": int(self.tokens),
            "enabled": bool(self.enabled),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }
        if include_schema:
            payload["schema"] = dict(self.schema)
        return payload


@dataclass(frozen=True)
class EffectiveAiInput:
    profile_id: str
    model_node_id: str
    system_segments: list[PromptSegment]
    developer_segments: list[PromptSegment] = field(default_factory=list)
    context_segments: list[PromptSegment] = field(default_factory=list)
    tool_schemas: list[ToolSchemaSegment] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    token_estimate: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=dict)
    disabled_segments: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    gate_decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model_node_id": self.model_node_id,
            "system_segments": [
                segment.to_dict(include_text=include_text) for segment in self.system_segments
            ],
            "developer_segments": [
                segment.to_dict(include_text=include_text) for segment in self.developer_segments
            ],
            "context_segments": [
                segment.to_dict(include_text=include_text) for segment in self.context_segments
            ],
            "tool_schemas": [
                segment.to_dict(include_schema=include_text) for segment in self.tool_schemas
            ],
            "policy": dict(self.policy),
            "disabled_segments": [dict(item) for item in self.disabled_segments],
        }


@dataclass(frozen=True)
class AiInputSegmentRegistry:
    prompt_segments: dict[str, PromptSegment] = field(default_factory=dict)
    tool_schemas: dict[str, ToolSchemaSegment] = field(default_factory=dict)
    policy_segments: dict[str, PromptSegment] = field(default_factory=dict)

    def segment_for_node(self, node_id: str) -> PromptSegment | ToolSchemaSegment | None:
        return (
            self.prompt_segments.get(node_id)
            or self.tool_schemas.get(node_id)
            or self.policy_segments.get(node_id)
        )


def normalize_ai_input_config(value: Any, *, strict: bool = False) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    disabled_edges = _string_list(raw.get("disabled_edges"), strict=strict, field_name="disabled_edges")
    inserted_edges = _normalize_inserted_edges(raw.get("inserted_edges"), strict=strict)
    gates = _normalize_gates(raw.get("gates"), strict=strict)
    budgets = _normalize_budgets(raw.get("budgets") or raw.get("budget"), strict=strict)
    return {
        "version": _positive_int(raw.get("version"), AI_INPUT_VERSION),
        "disabled_edges": disabled_edges,
        "gates": gates,
        "inserted_edges": inserted_edges,
        "budgets": budgets,
    }


def edge_from_dict(raw_edge: dict[str, Any], *, strict: bool = False) -> AiInputEdge | None:
    raw_metadata = raw_edge.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    edge_id = str(raw_edge.get("id") or "").strip()
    from_id = str(raw_edge.get("from_id") or raw_edge.get("from") or "").strip()
    to_id = str(raw_edge.get("to_id") or raw_edge.get("to") or "").strip()
    kind = str(raw_edge.get("kind") or "contributes_to").strip()
    from_port = str(raw_edge.get("from_port") or metadata.get("from_port") or "output").strip()
    to_port = str(raw_edge.get("to_port") or metadata.get("to_port") or "").strip()
    if not edge_id or not from_id or not to_id or not kind:
        if strict:
            raise ValueError("inserted edge requires id, from_id, to_id, and kind")
        return None
    return AiInputEdge(
        id=edge_id,
        from_id=from_id,
        from_port=from_port or "output",
        to_id=to_id,
        to_port=to_port,
        kind=kind,
        active=bool(raw_edge.get("active", True)),
        gate_id=_optional_string(raw_edge.get("gate_id") or metadata.get("gate_id")),
        metadata=dict(metadata),
    )


def _preview_text(text: str, limit: int = 280) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_string(value: Any) -> str | None:
    candidate = str(value or "").strip()
    return candidate or None


def _string_list(value: Any, *, strict: bool, field_name: str) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if value is None:
        return []
    if not isinstance(value, list):
        if strict:
            raise ValueError(f"ai_input.{field_name} must be a list")
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        candidate = str(item or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def _normalize_inserted_edges(value: Any, *, strict: bool) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        if strict:
            raise ValueError("ai_input.inserted_edges must be a list")
        return []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_edge in enumerate(value):
        if not isinstance(raw_edge, dict):
            if strict:
                raise ValueError(f"ai_input.inserted_edges[{index}] must be an object")
            continue
        edge = edge_from_dict(raw_edge, strict=strict)
        if edge is None:
            continue
        if edge.id in seen:
            if strict:
                raise ValueError(f"ai_input.inserted_edges contains duplicate id '{edge.id}'")
            continue
        seen.add(edge.id)
        edges.append(edge.to_dict())
    return edges


def _normalize_gates(value: Any, *, strict: bool) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("ai_input.gates must be an object")
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for gate_id, raw_gate in value.items():
        normalized_id = str(gate_id or "").strip()
        if not normalized_id:
            continue
        if not isinstance(raw_gate, dict):
            if strict:
                raise ValueError(f"ai_input.gates.{normalized_id} must be an object")
            continue
        kind = str(raw_gate.get("kind") or "condition_gate").strip()
        if kind not in {"condition_gate", "vector_gate", "budget_gate"}:
            if strict:
                raise ValueError(f"Unsupported AI input gate kind '{kind}'")
            kind = "condition_gate"
        gate = dict(raw_gate)
        gate["id"] = str(gate.get("id") or normalized_id)
        gate["kind"] = kind
        if kind == "vector_gate":
            gate["runtime_enabled"] = bool(gate.get("runtime_enabled", False))
        normalized[normalized_id] = gate
    return normalized


def _normalize_budgets(value: Any, *, strict: bool) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("ai_input.budgets must be an object")
        return {}
    if "max_system_tokens" in value or "max_tool_schema_tokens" in value:
        value = {
            "system": {
                "max_tokens": value.get("max_system_tokens"),
                "strategy": value.get("strategy") or "priority",
            },
            "tools": {
                "max_tokens": value.get("max_tool_schema_tokens"),
                "strategy": value.get("strategy") or "priority",
            },
        }
    budgets: dict[str, dict[str, Any]] = {}
    for port, raw_budget in value.items():
        port_name = str(port or "").strip()
        if not port_name or not isinstance(raw_budget, dict):
            continue
        max_tokens = raw_budget.get("max_tokens")
        if max_tokens is None:
            continue
        try:
            max_tokens_int = int(max_tokens)
        except (TypeError, ValueError):
            continue
        if max_tokens_int <= 0:
            continue
        strategy = str(raw_budget.get("strategy") or "priority").strip()
        if strategy not in {"priority", "recent", "score"}:
            strategy = "priority"
        budgets[port_name] = {"max_tokens": max_tokens_int, "strategy": strategy}
    return budgets
