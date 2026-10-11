from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


COMPUTER_TOOL_IDS = {"computer_use", "browser_computer", "browser_use", "browser_companion"}
TOOL_SELECTION_MODES = {"auto", "review", "manual", "none"}
TOOL_SELECTION_SCOPES = {"turn", "conversation"}
CAPABILITY_TARGET_KINDS = {"activity", "service", "tool", "skill"}
TOOL_SELECTION_STRATEGIES = {
    "hybrid",
    "semantic",
    "catalog_ai",
    "all_with_hints",
    "all_schemas",
    "lexical",
}


@dataclass(frozen=True)
class CapabilityTarget:
    """A structured Activity, Service, Tool, or Skill target."""

    kind: str
    id: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


ToolTarget = CapabilityTarget


@dataclass
class ToolSelectionRequest:
    mode: str = "auto"
    strategy: str | None = None
    scope: str = "turn"
    include: list[ToolTarget] = field(default_factory=list)
    exclude: list[ToolTarget] = field(default_factory=list)
    must_use: bool = False
    preview_id: str | None = None
    source: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "strategy": self.strategy,
            "scope": self.scope,
            "include": [target.to_dict() for target in self.include],
            "exclude": [target.to_dict() for target in self.exclude],
            "must_use": self.must_use,
            "preview_id": self.preview_id,
            "source": self.source,
        }


@dataclass
class ToolRecommendation:
    tool_id: str
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolSelectionResult:
    recommended_tools: list[ToolRecommendation] = field(default_factory=list)
    not_selected: list[dict[str, Any]] = field(default_factory=list)
    requires_tool_calling_model: bool = False
    candidate_count: int = 0
    stage: str = "keyword"
    selector_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_tools": [item.to_dict() for item in self.recommended_tools],
            "not_selected": list(self.not_selected),
            "requires_tool_calling_model": self.requires_tool_calling_model,
            "candidate_count": self.candidate_count,
            "stage": self.stage,
            "selector_model": self.selector_model,
        }


@dataclass
class ToolSelectionDecision:
    selection_id: str
    mode: str
    strategy: str
    stage: str
    selected_tools: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[ToolRecommendation] = field(default_factory=list)
    selected_services: list[dict[str, Any]] = field(default_factory=list)
    permission_summary: dict[str, int] = field(default_factory=dict)
    eligible_count: int = 0
    candidate_count: int = 0
    selected_count: int = 0
    provider_schema_count: int = 0
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    unknown_targets: list[str] = field(default_factory=list)
    duration_ms: int = 0
    cache_hit: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        selected_tool_ids = [
            str(tool.get("tool_id") or tool.get("name") or "").strip()
            for tool in self.selected_tools
            if str(tool.get("tool_id") or tool.get("name") or "").strip()
        ]
        return {
            "selection_id": self.selection_id,
            "mode": self.mode,
            "strategy": self.strategy,
            "stage": self.stage,
            "eligible_count": self.eligible_count,
            "candidate_count": self.candidate_count,
            "selected_tool_ids": selected_tool_ids,
            "selected_services": list(self.selected_services),
            "recommendations": [item.to_dict() for item in self.recommendations],
            "permission_summary": dict(self.permission_summary),
            "fallbacks": list(self.fallbacks),
            "duration_ms": self.duration_ms,
            "cache_hit": self.cache_hit,
            "metrics": dict(self.metrics),
        }


def normalize_tool_target(value: Any) -> CapabilityTarget | None:
    if isinstance(value, CapabilityTarget):
        return value
    if isinstance(value, str):
        target_id = value.strip()
        return CapabilityTarget("tool", target_id) if target_id else None
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or value.get("type") or "").strip().lower()
    if kind not in CAPABILITY_TARGET_KINDS:
        if value.get("tool_id") and not value.get("service_id"):
            kind = "tool"
        elif value.get("service_id") and not value.get("tool_id"):
            kind = "service"
    target_id = str(
        value.get("id")
        or value.get("tool_id")
        or value.get("service_id")
        or value.get("activity_id")
        or value.get("skill_id")
        or ""
    ).strip()
    if not target_id:
        return None
    if kind not in CAPABILITY_TARGET_KINDS:
        return None
    return CapabilityTarget(kind, target_id)


def normalize_tool_targets(value: Any) -> list[CapabilityTarget]:
    if not isinstance(value, list):
        return []
    targets: list[CapabilityTarget] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        target = normalize_tool_target(item)
        if target is None:
            continue
        key = (target.kind, target.id)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)
    return targets
