from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


_BROWSER_INTENT_RE = re.compile(
    r"browser|chrome|vivaldi|web\s*page|open\s+.+site|クリック|ブラウザ|クローム|開いて|開く",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateDecision:
    gate_id: str
    decision: bool
    reason: str
    kind: str = "condition_gate"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "decision": bool(self.decision),
            "reason": self.reason,
            "kind": self.kind,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ConditionGateConfig:
    id: str
    expression: dict[str, Any]
    default: bool = False


@dataclass(frozen=True)
class VectorGateConfig:
    id: str
    query_source: str
    candidate_source: str
    threshold: float = 0.72
    max_segments: int = 5
    runtime_enabled: bool = False


@dataclass(frozen=True)
class BudgetGateConfig:
    id: str
    max_tokens: int
    strategy: Literal["priority", "recent", "score"] = "priority"


def evaluate_gate_config(gate_id: str, config: dict[str, Any], context: dict[str, Any]) -> GateDecision:
    kind = str(config.get("kind") or "condition_gate").strip()
    if kind == "condition_gate":
        raw_expression = config.get("expression")
        expression: dict[str, Any] = (
            raw_expression if isinstance(raw_expression, dict) else {}
        )
        default = bool(config.get("default", False))
        return evaluate_condition_gate(
            ConditionGateConfig(id=gate_id, expression=expression, default=default),
            context,
        )
    if kind == "vector_gate":
        runtime_enabled = bool(config.get("runtime_enabled", False))
        return GateDecision(
            gate_id=gate_id,
            decision=runtime_enabled,
            reason="vector_gate_runtime_enabled" if runtime_enabled else "vector_gate_runtime_disabled",
            kind=kind,
            metadata={
                "threshold": config.get("threshold", 0.72),
                "max_segments": config.get("max_segments", 5),
            },
        )
    return GateDecision(gate_id=gate_id, decision=True, reason="gate_kind_preview_only", kind=kind)


def evaluate_condition_gate(config: ConditionGateConfig, context: dict[str, Any]) -> GateDecision:
    if not config.expression:
        return GateDecision(
            gate_id=config.id,
            decision=bool(config.default),
            reason="empty_expression_default",
            kind="condition_gate",
        )
    try:
        decision = _eval_expression(config.expression, _context_with_inferred_intent(context))
    except Exception as exc:
        return GateDecision(
            gate_id=config.id,
            decision=bool(config.default),
            reason=f"condition_error:{exc.__class__.__name__}",
            kind="condition_gate",
        )
    return GateDecision(
        gate_id=config.id,
        decision=decision,
        reason="condition_matched" if decision else "condition_not_matched",
        kind="condition_gate",
    )


def infer_user_intent(message: str) -> str:
    text = str(message or "")
    if _BROWSER_INTENT_RE.search(text):
        return "browser_automation"
    return "general"


def _context_with_inferred_intent(context: dict[str, Any]) -> dict[str, Any]:
    updated = dict(context or {})
    if not str(updated.get("user_intent") or "").strip():
        message = updated.get("message") or updated.get("user_message") or updated.get("user_text") or ""
        updated["user_intent"] = infer_user_intent(str(message or ""))
    return updated


def _eval_expression(expression: dict[str, Any], context: dict[str, Any]) -> bool:
    if "all" in expression:
        entries = expression.get("all")
        return all(_eval_expression(item, context) for item in entries if isinstance(item, dict)) if isinstance(entries, list) else False
    if "any" in expression:
        entries = expression.get("any")
        return any(_eval_expression(item, context) for item in entries if isinstance(item, dict)) if isinstance(entries, list) else False
    if "not" in expression and isinstance(expression.get("not"), dict):
        return not _eval_expression(expression["not"], context)

    field = str(expression.get("field") or "").strip()
    op = str(expression.get("op") or "eq").strip()
    expected = expression.get("value")
    actual = _resolve_field(context, field)
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "includes":
        return _includes(actual, expected)
    if op == "not_includes":
        return not _includes(actual, expected)
    if op == "contains":
        return str(expected or "") in str(actual or "")
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    return False


def _resolve_field(context: dict[str, Any], field: str) -> Any:
    if not field:
        return None
    current: Any = context
    for part in field.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _includes(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (list, tuple, set)):
        return expected in actual or str(expected) in {str(item) for item in actual}
    if isinstance(actual, str):
        return str(expected or "") in actual
    return False
