from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecosystem.search_home_pack.domain.route_decision import RouteDecision, decide_route  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com", "URL_NAVIGATE"),
        ("github.com/harupipipipi/rumiai", "URL_NAVIGATE"),
        ("localhost:3000", "BLOCKED"),
        ("https://user:password@example.com/", "BLOCKED"),
        ("!g rumiai profile", "GOOGLE_REDIRECT"),
        ("google: rumiai", "GOOGLE_REDIRECT"),
        ("!ai rumiai profile設計", "ASK_AI"),
        ("Go fmtって必要？", "ASK_AI"),
        ("日東紡 株価", "ASK_AI_WITH_SEARCH"),
        ("openai 評価額", "ASK_AI_WITH_SEARCH"),
        ("rumiai PR156 mergeどうする", "ASK_AI_WITH_SEARCH"),
        ("javascript:alert(1)", "BLOCKED"),
        ("file:///etc/passwd", "BLOCKED"),
    ],
)
def test_search_home_route(raw: str, expected: str):
    decision = decide_route(raw)

    assert decision.route == expected


def test_search_home_ambiguous_input_uses_ai_classifier_when_bridge_is_available():
    class StubBridge:
        def classify_with_ai(self, query: str) -> RouteDecision:
            assert query == "opaque query"
            return RouteDecision(
                route="ASK_AI",
                confidence=0.73,
                normalized_query=query,
                reason="AI classifier picked ASK_AI",
                source="ai",
            )

    decision = decide_route("opaque query", bridge=StubBridge())

    assert decision.route == "ASK_AI"
    assert decision.source == "ai"


def test_search_home_ambiguous_input_falls_back_to_ai_with_search_when_bridge_fails():
    class FailingBridge:
        def classify_with_ai(self, _query: str) -> RouteDecision:
            raise RuntimeError("runtime unavailable")

    decision = decide_route("opaque query", bridge=FailingBridge())

    assert decision.route == "ASK_AI_WITH_SEARCH"
    assert decision.reason == "ambiguous input defaults to AI with search"
