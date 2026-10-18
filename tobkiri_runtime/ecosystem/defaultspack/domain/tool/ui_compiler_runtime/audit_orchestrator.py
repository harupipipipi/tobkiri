from __future__ import annotations

from typing import Any

from domain.ui_compiler import RenderMatrix, UIPlan
from domain.tool.schema_adapter import list_or_empty, mapping_or_empty


class UIQualityAuditOrchestrator:
    def audit(
        self,
        *,
        plan: UIPlan,
        foundation: dict[str, Any],
        page_matrix: RenderMatrix,
        page_compression: dict[str, Any],
        accepted_count: int,
    ) -> dict[str, Any]:
        metrics = _aggregate(page_matrix)
        audits = {
            "intent": _intent_report(plan),
            "foundation": _foundation_report(foundation),
            "topology": _topology_report(plan, metrics),
            "split": _split_report(plan),
            "compression": _normalize_compression(page_compression),
            "textPressure": _text_pressure_report(metrics),
            "typography": _typography_report(foundation, metrics),
            "colorRoles": _color_report(foundation, metrics),
            "surfaceAudit": _surface_report(foundation, metrics),
            "interactionBudget": _interaction_report(plan, metrics),
            "responsive": _responsive_report(metrics),
            "accessibility": _accessibility_report(metrics),
        }
        failed = [
            key
            for key, report in audits.items()
            if isinstance(report, dict) and report.get("status") == "fail"
        ]
        return {
            "status": "fail" if failed else "pass",
            "failedAudits": failed,
            "acceptedLeafCount": accepted_count,
            "metrics": metrics,
            **audits,
        }


def _aggregate(render_matrix: RenderMatrix) -> dict[str, Any]:
    snapshots = list(render_matrix.snapshots or [])
    if not snapshots:
        return {"snapshotCount": 0}

    def maximum(key: str, default: float = 0) -> float:
        return max(float(item.metrics.get(key, default) or default) for item in snapshots)

    def total(key: str) -> float:
        return sum(float(item.metrics.get(key) or 0) for item in snapshots)

    mobile = [item for item in snapshots if int(item.metrics.get("viewport") or item.viewport) <= 390]
    return {
        "snapshotCount": len(snapshots),
        "viewports": sorted({int(item.metrics.get("viewport") or item.viewport) for item in snapshots}),
        "maxVisibleTextBlocks": int(maximum("visibleTextBlocks")),
        "maxVisibleCharacters": int(maximum("visibleCharacters")),
        "maxAverageLineLength": int(maximum("averageLineLength")),
        "lineClampFrequency": int(total("lineClampCount")),
        "ellipsisFrequency": int(total("ellipsisCount")),
        "repeatedMetadataLines": int(total("repeatedMetadataLines")),
        "worstJapaneseBreakQuality": min(float(item.metrics.get("japaneseBreakQuality") or 1) for item in snapshots),
        "maxLabelDensity": maximum("labelDensity"),
        "maxVisibleActions": int(maximum("visibleActions")),
        "minAllowedActions": int(min(float(item.metrics.get("allowedActions") or 99) for item in snapshots)),
        "horizontalOverflow": int(total("horizontalOverflow")),
        "tinyFontUsage": int(total("tinyFontUsage")),
        "touchTargetFailures": int(total("touchTargetFailures")),
        "toolbarOverflow": int(total("toolbarOverflow")),
        "primaryActionUnreachable": int(total("primaryActionUnreachable")),
        "maxSurfaceDepth": int(maximum("surfaceDepth", 1)),
        "maxCardNestingDepth": int(maximum("cardNestingDepth", 1)),
        "maxCardCount": int(maximum("cardCount")),
        "maxBorderCount": int(maximum("borderCount")),
        "maxDividerCount": int(maximum("dividerCount")),
        "maxShadowCount": int(maximum("shadowCount")),
        "maxGradientCount": int(maximum("gradientCount")),
        "nonSemanticColorCount": int(total("nonSemanticColorCount")),
        "maxMutedTextRatio": maximum("mutedTextRatio"),
        "maxRadiusUniformity": maximum("radiusUniformity"),
        "mobileDisclosureUsed": any(bool(item.metrics.get("mobileDisclosureUsed")) for item in mobile),
        "mobileHorizontalOverflow": any(bool(item.metrics.get("horizontalOverflow")) for item in mobile),
        "minContrast": min(float(item.metrics.get("contrastMin") or 0) for item in snapshots),
        "focusVisible": all(bool(item.metrics.get("focusVisible")) for item in snapshots),
        "keyboardNav": all(bool(item.metrics.get("keyboardNav")) for item in snapshots),
        "minAriaRoles": int(min(float(item.metrics.get("ariaRoles") or 0) for item in snapshots)),
    }


def _intent_report(plan: UIPlan) -> dict[str, Any]:
    root = plan.root.node
    missing = [
        name
        for name, value in {
            "purpose": root.purpose,
            "density": root.density,
            "importance": root.importance,
        }.items()
        if not str(value or "").strip()
    ]
    return _report("fail" if missing else "pass", missing, {"productMode": root.metadata.get("productMode")})


def _foundation_report(foundation: dict[str, Any]) -> dict[str, Any]:
    missing = []
    for key in ("direction", "typography", "spacing", "color", "surface", "primitives"):
        if not foundation.get(key):
            missing.append(key)
    direction = mapping_or_empty(foundation.get("direction"))
    surface = mapping_or_empty(foundation.get("surface"))
    evidence = {
        "productMode": direction.get("productMode"),
        "surfacePolicy": surface,
        "primitiveCount": len(list_or_empty(foundation.get("primitives"))),
    }
    return _report("fail" if missing else "pass", missing, evidence)


def _topology_report(plan: UIPlan, metrics: dict[str, Any]) -> dict[str, Any]:
    mobile_behaviors = [
        node.node.layout_envelope.mobile_behavior
        for node in plan.root.planned_nodes()
        if node.node.layout_envelope.mobile_behavior
    ]
    fail = bool(metrics.get("mobileHorizontalOverflow")) or not any(
        item in {"route", "sheet", "drawer", "sticky-bottom", "stack"} for item in mobile_behaviors
    )
    return _report(
        "fail" if fail else "pass",
        ["mobile topology is desktop-shrink or overflowing"] if fail else [],
        {"mobileBehaviors": mobile_behaviors, "mobileDisclosureUsed": metrics.get("mobileDisclosureUsed")},
    )


def _split_report(plan: UIPlan) -> dict[str, Any]:
    contracts = plan.contracts()
    semantic_children = len(plan.root.children)
    fail = semantic_children == 0 or not contracts
    return _report(
        "fail" if fail else "pass",
        ["semantic regions or component contracts are missing"] if fail else [],
        {"semanticRegions": semantic_children, "contracts": len(contracts)},
    )


def _normalize_compression(page_compression: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "pass" if page_compression.get("status") == "pass" else "fail",
        "score": page_compression.get("compressionScore"),
        "metrics": page_compression.get("metrics") or {},
        "issues": page_compression.get("issues") or [],
    }


def _text_pressure_report(metrics: dict[str, Any]) -> dict[str, Any]:
    issues = []
    if int(metrics.get("maxVisibleCharacters") or 0) > 1200:
        issues.append("visible character count exceeds the screen budget")
    if int(metrics.get("maxVisibleTextBlocks") or 0) > 10:
        issues.append("too many visible text blocks in one screen")
    if int(metrics.get("maxAverageLineLength") or 0) > 92:
        issues.append("average line length is too high")
    if int(metrics.get("lineClampFrequency") or 0) > 0 or int(metrics.get("ellipsisFrequency") or 0) > 2:
        issues.append("important text is clipped or ellipsized")
    if float(metrics.get("maxLabelDensity") or 0) > 2.5:
        issues.append("label density per region is too high")
    if float(metrics.get("worstJapaneseBreakQuality") or 1) < 0.75:
        issues.append("long Japanese text break quality is weak")
    return _report("fail" if issues else "pass", issues, {key: metrics.get(key) for key in (
        "maxVisibleTextBlocks",
        "maxVisibleCharacters",
        "maxAverageLineLength",
        "lineClampFrequency",
        "ellipsisFrequency",
        "repeatedMetadataLines",
        "worstJapaneseBreakQuality",
        "maxLabelDensity",
    )})


def _typography_report(foundation: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    typography = mapping_or_empty(foundation.get("typography"))
    roles = mapping_or_empty(typography.get("roles"))
    required = {"pageTitle", "sectionTitle", "body", "label", "caption", "numeric", "code"}
    missing = sorted(required - set(roles))
    issues = [f"missing typography roles: {', '.join(missing)}"] if missing else []
    if int(metrics.get("tinyFontUsage") or 0) > 0:
        issues.append("tiny font escape detected")
    return _report("fail" if issues else "pass", issues, {"roles": sorted(roles)})


def _color_report(foundation: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    color = mapping_or_empty(foundation.get("color"))
    roles = list_or_empty(color.get("roles"))
    required = {"canvas", "surface", "textPrimary", "textSecondary", "actionPrimary", "statusCritical"}
    missing = sorted(required - set(str(item) for item in roles))
    issues = [f"missing semantic color roles: {', '.join(missing)}"] if missing else []
    if int(metrics.get("maxGradientCount") or 0) > 0:
        issues.append("generic gradient usage detected")
    if int(metrics.get("nonSemanticColorCount") or 0) > 0:
        issues.append("non-semantic color usage detected")
    if float(metrics.get("maxMutedTextRatio") or 0) > 0.7:
        issues.append("muted text dominates the hierarchy")
    return _report("fail" if issues else "pass", issues, {"roles": roles})


def _surface_report(foundation: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    surface = mapping_or_empty(foundation.get("surface"))
    max_nested = int(surface.get("maxNestedDepth") or 1)
    issues = []
    if int(metrics.get("maxSurfaceDepth") or 0) > max_nested + 1:
        issues.append("surface nesting exceeds policy")
    if int(metrics.get("maxCardNestingDepth") or 0) > 2:
        issues.append("card nesting depth is too high")
    if int(metrics.get("maxBorderCount") or 0) > 10:
        issues.append("border repetition is too high")
    if int(metrics.get("maxShadowCount") or 0) > 1:
        issues.append("shadow abuse detected")
    if float(metrics.get("maxRadiusUniformity") or 0) > 0.92:
        issues.append("same-radius-everything syndrome detected")
    return _report("fail" if issues else "pass", issues, {"surfacePolicy": surface})


def _interaction_report(plan: UIPlan, metrics: dict[str, Any]) -> dict[str, Any]:
    budgets = {contract.id: contract.visible_action_budget for contract in plan.contracts()}
    fail = int(metrics.get("maxVisibleActions") or 0) > max([*budgets.values(), 1])
    issues = ["visible action budget exceeded"] if fail else []
    if int(metrics.get("primaryActionUnreachable") or 0) > 0:
        issues.append("primary action unreachable")
    return _report("fail" if issues else "pass", issues, {"regionBudgets": budgets, "maxVisibleActions": metrics.get("maxVisibleActions")})


def _responsive_report(metrics: dict[str, Any]) -> dict[str, Any]:
    issues = []
    if metrics.get("mobileHorizontalOverflow"):
        issues.append("390px mobile viewport overflows horizontally")
    if int(metrics.get("toolbarOverflow") or 0) > 0:
        issues.append("toolbar overflows responsive budget")
    if not metrics.get("mobileDisclosureUsed"):
        issues.append("mobile topology does not show route, drawer, sheet, disclosure, or step-down behavior")
    return _report("fail" if issues else "pass", issues, {"viewports": metrics.get("viewports"), "mobileDisclosureUsed": metrics.get("mobileDisclosureUsed")})


def _accessibility_report(metrics: dict[str, Any]) -> dict[str, Any]:
    issues = []
    if int(metrics.get("touchTargetFailures") or 0) > 0:
        issues.append("touch target too small")
    if float(metrics.get("minContrast") or 0) < 4.5:
        issues.append("contrast below 4.5")
    if not metrics.get("focusVisible"):
        issues.append("focus visibility missing")
    if not metrics.get("keyboardNav"):
        issues.append("keyboard navigation missing")
    if int(metrics.get("minAriaRoles") or 0) < 1:
        issues.append("aria role coverage missing")
    return _report("fail" if issues else "pass", issues, {"minContrast": metrics.get("minContrast"), "focusVisible": metrics.get("focusVisible"), "keyboardNav": metrics.get("keyboardNav")})


def _report(status: str, issues: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "issues": [{"severity": "blocker" if status == "fail" else "info", "message": issue} for issue in issues],
        "evidence": evidence,
    }
