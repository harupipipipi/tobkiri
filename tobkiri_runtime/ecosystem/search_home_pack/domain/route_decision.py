from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .safe_url import (
    build_google_fallback_url,
    classify_direct_url,
    unsafe_scheme_reason,
    validate_candidate_url,
)


# Legacy route name kept for compatibility. The behavior is now:
# resolve the web intent and redirect to the best destination URL.
GOOGLE_REDIRECT = "GOOGLE_REDIRECT"
ASK_AI_WITH_SEARCH = "ASK_AI_WITH_SEARCH"
ASK_AI = "ASK_AI"
BLOCKED = "BLOCKED"
URL_NAVIGATE = "URL_NAVIGATE"

_SEARCH_INTENT_RE = re.compile(
    r"(株価|評価額|時価総額|ニュース|news|検索|価格|為替|天気|weather|\bPR\s*#?\d+\b|\bmerge\b)",
    re.IGNORECASE,
)
_QUESTION_INTENT_RE = re.compile(r"(\?|？|とは|必要|なぜ|どうして|教えて|できますか|すべき)")


@dataclass(slots=True)
class RouteDecision:
    route_type: str = GOOGLE_REDIRECT
    query: str = ""
    target_url: str = ""
    target_candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_index: int = -1
    fallback_url: str = ""
    resolution_reason: str = ""
    used_ai_judge: bool = False
    used_visual_judge: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    route: str = ""
    confidence: float = 1.0
    normalized_query: str = ""
    reason: str = ""
    source: str = "heuristic"

    def __post_init__(self) -> None:
        if self.route:
            self.route_type = self.route
        else:
            self.route = self.route_type
        if not self.normalized_query:
            self.normalized_query = self.query
        if not self.reason:
            self.reason = self.resolution_reason
        elif not self.resolution_reason:
            self.resolution_reason = self.reason

    def selected_candidate(self) -> dict[str, Any] | None:
        if self.selected_index < 0 or self.selected_index >= len(self.target_candidates):
            return None
        return self.target_candidates[self.selected_index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_type": self.route_type,
            "query": self.query,
            "target_url": self.target_url,
            "target_candidates": [dict(item) for item in self.target_candidates],
            "selected_index": self.selected_index,
            "fallback_url": self.fallback_url,
            "resolution_reason": self.resolution_reason,
            "used_ai_judge": self.used_ai_judge,
            "used_visual_judge": self.used_visual_judge,
            "metadata": dict(self.metadata),
        }


def decide_route(raw: str, *, bridge: Any = None) -> RouteDecision:
    """Classify legacy Search Home input without invoking network-heavy resolution.

    The current Search Home flow uses SearchTargetResolver for full candidate
    probing. This helper is kept as a lightweight compatibility layer for tests
    and older callers that only need coarse route selection.
    """
    text = str(raw or "").strip()
    fallback_url = build_google_fallback_url(text)
    if not text:
        return _legacy_decision(
            GOOGLE_REDIRECT,
            text,
            fallback_url=fallback_url,
            reason="empty input defaults to search",
        )

    direct = classify_direct_url(text)
    if (direct and direct.get("blocked")) or unsafe_scheme_reason(text):
        return _legacy_decision(BLOCKED, text, reason="unsafe URL scheme is blocked")
    if direct and direct.get("url"):
        validation = validate_candidate_url(
            str(direct.get("url") or ""), allow_localhost=False
        )
        if not validation.ok:
            return _legacy_decision(
                BLOCKED,
                text,
                fallback_url=fallback_url,
                reason=f"destination policy blocked: {validation.reason}",
            )
        return _legacy_decision(
            URL_NAVIGATE,
            text,
            target_url=validation.normalized_url,
            fallback_url=fallback_url,
            reason=str(direct.get("reason") or "recognized direct URL"),
        )

    lowered = text.casefold()
    if lowered.startswith("!g "):
        query = text[3:].strip()
        return _legacy_decision(
            GOOGLE_REDIRECT,
            query,
            fallback_url=build_google_fallback_url(query),
            reason="google shortcut",
        )
    if lowered.startswith("google:"):
        query = text.split(":", 1)[1].strip()
        return _legacy_decision(
            GOOGLE_REDIRECT,
            query,
            fallback_url=build_google_fallback_url(query),
            reason="google prefix",
        )
    if lowered.startswith("!ai "):
        query = text[4:].strip()
        return _legacy_decision(ASK_AI, query, reason="ai shortcut")

    if _SEARCH_INTENT_RE.search(text):
        return _legacy_decision(
            ASK_AI_WITH_SEARCH,
            text,
            fallback_url=fallback_url,
            reason="search-backed answer intent",
        )
    if _QUESTION_INTENT_RE.search(text):
        return _legacy_decision(ASK_AI, text, reason="question intent")

    if bridge is not None and callable(getattr(bridge, "classify_with_ai", None)):
        try:
            decision = bridge.classify_with_ai(text)
        except Exception:
            decision = None
        if isinstance(decision, RouteDecision):
            return decision
        if isinstance(decision, dict):
            return RouteDecision(
                route=str(decision.get("route") or decision.get("route_type") or ASK_AI_WITH_SEARCH),
                query=str(decision.get("query") or text),
                confidence=float(decision.get("confidence") or 1.0),
                normalized_query=str(decision.get("normalized_query") or text),
                reason=str(decision.get("reason") or decision.get("resolution_reason") or ""),
                source=str(decision.get("source") or "ai"),
            )

    return _legacy_decision(
        ASK_AI_WITH_SEARCH,
        text,
        fallback_url=fallback_url,
        reason="ambiguous input defaults to AI with search",
    )


def _legacy_decision(
    route: str,
    query: str,
    *,
    target_url: str = "",
    fallback_url: str = "",
    reason: str = "",
    source: str = "heuristic",
) -> RouteDecision:
    return RouteDecision(
        route=route,
        route_type=route,
        query=query,
        target_url=target_url,
        fallback_url=fallback_url,
        selected_index=0 if target_url else -1,
        normalized_query=query,
        reason=reason,
        resolution_reason=reason,
        source=source,
    )
