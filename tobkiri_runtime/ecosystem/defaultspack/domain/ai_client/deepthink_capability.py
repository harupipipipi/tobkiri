"""Bounded, public capability appraisal for a DeepThink task."""

from __future__ import annotations

from typing import Any


def normalize_capability_assessment(value: Any, *, model: str) -> dict[str, Any]:
    """Keep untrusted model appraisals small and mark their evidence as claims."""

    raw = value if isinstance(value, dict) else {}

    def short(value: Any, limit: int = 500) -> str:
        return str(value or "").strip()[:limit] if isinstance(value, str) else ""

    evidence = []
    for item in (raw.get("evidence") if isinstance(raw.get("evidence"), list) else [])[:8]:
        if not isinstance(item, dict):
            continue
        kind = short(item.get("kind"), 32)
        if kind not in {"benchmark", "past_artifact", "external_report", "tool_result"}:
            continue
        reference = short(item.get("reference"))
        finding = short(item.get("finding"))
        if not reference or not finding:
            continue
        evidence.append(
            {
                "kind": kind,
                "reference": reference,
                "finding": finding,
                # A model's citation alone does not establish that a source was checked.
                "verification": "model_reported",
            }
        )

    limitations = [
        short(item)
        for item in (raw.get("limitations") if isinstance(raw.get("limitations"), list) else [])[:8]
        if short(item)
    ]
    method = raw.get("method") if isinstance(raw.get("method"), dict) else {}
    approach = short(method.get("approach"), 32)
    if approach not in {"model", "host_tool", "software", "delegate", "clarify"}:
        approach = "clarify"
    return {
        "model": model,
        "evidence": evidence,
        "limitations": limitations or ["Task-specific capability is not established."],
        "uncertainties": [
            short(item)
            for item in (
                raw.get("uncertainties")
                if isinstance(raw.get("uncertainties"), list)
                else []
            )[:8]
            if short(item)
        ],
        "method": {
            "approach": approach,
            "reason": short(method.get("reason")),
            "fallback": short(method.get("fallback")),
        },
        "evidence_sufficient": bool(evidence) and raw.get("evidence_sufficient") is True,
    }
