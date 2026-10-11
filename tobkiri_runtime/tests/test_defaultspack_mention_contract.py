from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.company.mention import CompanyMentionService, extract_mentions  # noqa: E402
from domain.mention import extract_mention_values, iter_mention_tokens  # noqa: E402
from domain.subagent_team.mention_parser import (  # noqa: E402
    parse_mentions,
    sanitize_agent_mentions_for_gate,
)


pytestmark = pytest.mark.contract


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mention_boundaries.json").read_text(
        encoding="utf-8"
    )
)


def test_shared_mention_boundary_fixtures_cover_company_and_team() -> None:
    for fixture in FIXTURES:
        expected = fixture["tokens"]
        normalized_expected = [value.lower() for value in expected]
        text = fixture["text"]
        known_values = fixture.get("known_values")
        assert extract_mention_values(text, known_values) == expected, fixture["name"]
        assert extract_mentions(text, known_values) == normalized_expected, fixture["name"]
        assert (
            parse_mentions(text, known_values)["agent_mentions"] == normalized_expected
        ), fixture["name"]
        if "token_spans" in fixture:
            assert [
                [token.start, token.end]
                for token in iter_mention_tokens(text, known_values)
            ] == fixture["token_spans"], fixture["name"]


def test_mention_boundary_fixture_has_required_regression_classes() -> None:
    names = {fixture["name"] for fixture in FIXTURES}
    assert {
        "standalone trigger",
        "Japanese adjacency",
        "ASCII terminal punctuation",
        "full width terminal punctuation",
        "email address",
        "international email address",
        "URL path",
        "Unicode URL path handle",
        "mention after URL full width comma",
        "mention after URL full width period",
        "mention after URL closing parenthesis",
        "known dotted file after Japanese text",
        "known dotted tool after Japanese text",
        "double at",
        "escaped mention",
        "supplementary-plane letter",
        "long text",
    } <= names


def test_subagent_gate_sanitization_preserves_escaped_literals() -> None:
    assert sanitize_agent_mentions_for_gate("お願い@pm \\@reviewer") == (
        "お願いat pm \\@reviewer"
    )


def test_company_resolution_accepts_known_dotted_id_after_japanese_text() -> None:
    class _Store:
        def get_company(self, company_id: str):
            assert company_id == "company-1"
            return {
                "agents": {
                    "agent.one": {
                        "agent_id": "agent.one",
                        "id": "agent.one",
                        "role_key": "reviewer",
                        "aliases": [],
                    }
                }
            }

    resolved = CompanyMentionService(_Store()).resolve(
        "company-1",
        "お願い@agent.one",
    )

    assert resolved is not None
    assert resolved["mentions"] == ["agent.one"]
    assert resolved["resolved_agent_ids"] == ["agent.one"]


def test_chat_tool_inference_uses_the_same_safe_boundaries(monkeypatch) -> None:
    from domain.chat import run_request

    class _Registry:
        def get(self, tool_id: str):
            return (
                {"name": tool_id}
                if tool_id in {"web_search", "mcp.server"}
                else None
            )

        def list_tools(self):
            return [
                {"tool_id": "web_search", "display_name": "Web Search"},
                {
                    "tool_id": "mcp.server",
                    "display_name": "MCP Server",
                },
            ]

    monkeypatch.setattr(run_request, "ToolRegistry", _Registry)

    assert run_request._tool_mention_ids_from_text("調べて@web_search。") == [
        "web_search"
    ]
    assert run_request._tool_mention_ids_from_text(
        "mail@example.com https://example.com/@web_search \\@web_search @@web_search"
    ) == []
    assert run_request._tool_mention_ids_from_text(
        "https://example.com。@web_search"
    ) == ["web_search"]
    assert run_request._tool_mention_ids_from_text("お願い@mcp.server") == [
        "mcp.server"
    ]
    assert run_request._tool_mention_ids_from_text(
        "お願い@Web Search で調べて"
    ) == ["web_search"]
    assert run_request._tool_mention_ids_from_text("お願い@unknown.example") == []
