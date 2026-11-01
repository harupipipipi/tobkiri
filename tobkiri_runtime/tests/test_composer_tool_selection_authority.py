"""Visible Composer selections override old pins without weakening tool gates."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.chat.tool_selection_schema import ToolSelectionRequest, ToolTarget
from domain.chat.tool_selection_service import ToolSelectionService

TOOLS = [
    {"tool_id": "web_search", "name": "Web Search"},
    {"tool_id": "github.search_code", "name": "GitHub Search Code"},
]


@pytest.mark.parametrize("source", ["tool_selection", "tool_selection_preview"])
def test_explicit_turn_uses_visible_targets_instead_of_saved_preferences(source: str) -> None:
    """Removed old pins/exclusions do not return through backend preference merging."""
    decision = ToolSelectionService(settings={}).select(
        "Search", TOOLS,
        selection=ToolSelectionRequest(
            source=source, mode="manual", scope="turn",
            include=[ToolTarget("service", "web")],
        ),
        context={"conversation_tool_preferences": {
            "mode": "none", "include": [{"kind": "service", "id": "github"}],
            "exclude": [{"kind": "service", "id": "web"}],
        }},
    )
    assert decision.mode == "manual"
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["web_search"]


@pytest.mark.parametrize("source,scope", [("default", "turn"), ("tool_selection", "conversation")])
def test_legacy_default_and_conversation_scope_keep_saved_pins(source: str, scope: str) -> None:
    """Compatibility callers continue to use their saved conversation defaults."""
    decision = ToolSelectionService(settings={}).select(
        "Search", TOOLS,
        selection=ToolSelectionRequest(source=source, mode="manual", scope=scope),
        context={"conversation_tool_preferences": {
            "mode": "manual", "include": [{"kind": "service", "id": "github"}],
        }},
    )
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["github.search_code"]


@pytest.mark.parametrize("source", ["tool_selection", "tool_selection_preview"])
def test_authoritative_scope_keeps_raw_tool_exclusions_developer_only(source: str) -> None:
    """A new source of selection intent is not a grant of execution authority."""
    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            "Search", TOOLS,
            selection=ToolSelectionRequest(
                source=source, mode="manual", include=[ToolTarget("service", "web")],
                exclude=[ToolTarget("tool", "github.search_code")],
            ),
            context={},
        )


def test_authoritative_explicit_none_does_not_restore_a_pinned_service() -> None:
    """The visible no-tools mode remains effective even with a stored manual pin."""
    decision = ToolSelectionService(settings={}).select(
        "Search", TOOLS,
        selection=ToolSelectionRequest(source="tool_selection", mode="none"),
        context={"conversation_tool_preferences": {
            "mode": "manual", "include": [{"kind": "service", "id": "github"}],
        }},
    )
    assert decision.mode == "none"
    assert decision.selected_tools == []


def _frontend_service_request() -> tuple[str, ToolSelectionRequest]:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "composer_service_mentions.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    request = dict(fixture["request"])
    request["include"] = [ToolTarget(**target) for target in request["include"]]
    request["exclude"] = [ToolTarget(**target) for target in request["exclude"]]
    return fixture["text"], ToolSelectionRequest(source="tool_selection", **request)


SERVICE_TOOLS = [
    {"tool_id": "github.search_code", "display_name": "Search Code"},
    {"tool_id": "github.read_issue", "display_name": "Read Issue"},
    {"tool_id": "github.delete_issue", "display_name": "Delete Issue"},
    {"tool_id": "web_search", "display_name": "Web Search"},
]


@pytest.mark.parametrize("source", ["tool_selection", "tool_selection_preview"])
def test_frontend_expanded_service_request_selects_only_visible_members(
    source: str,
) -> None:
    """The shared frontend-generated request works without granting other members."""
    text, request = _frontend_service_request()
    request.source = source
    decision = ToolSelectionService(settings={}).select(
        text, SERVICE_TOOLS, selection=request, context={},
    )
    assert [tool["tool_id"] for tool in decision.selected_tools] == [
        "github.search_code", "github.read_issue",
    ]


@pytest.mark.parametrize("text", [
    "Plain text", "@-github", "\\@github", "a@github",
    "https://example/@github", "@github_extra", "@github\u0301",
])
def test_unverified_service_spelling_cannot_admit_raw_targets(text: str) -> None:
    """Ordinary text and exclusion syntax do not grant positive selection intent."""
    _, request = _frontend_service_request()
    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            text, SERVICE_TOOLS, selection=request, context={},
        )


def test_service_mention_does_not_verify_unrelated_or_unadmitted_raw_tool() -> None:
    """Membership comes from the current admitted catalog, never requested IDs."""
    text, request = _frontend_service_request()
    request.include.append(ToolTarget("tool", "web_search"))
    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            text, SERVICE_TOOLS, selection=request, context={},
        )
    _, request = _frontend_service_request()
    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            text, SERVICE_TOOLS[1:], selection=request, context={},
        )


def test_service_include_does_not_authorize_a_raw_exclusion() -> None:
    """A verified service member still cannot be excluded as a raw low-level tool."""
    text, request = _frontend_service_request()
    request.exclude = [ToolTarget("tool", "github.delete_issue")]
    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            text, SERVICE_TOOLS, selection=request, context={},
        )


@pytest.mark.parametrize("label_field", ["display_name", "name"])
def test_service_label_collision_does_not_verify_additional_tool_members(
    label_field: str,
) -> None:
    """An exact Tool label keeps its existing meaning when it resembles a service."""
    _, request = _frontend_service_request()
    tools = [*SERVICE_TOOLS, {"tool_id": "other_action", label_field: "GitHub"}]
    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            "@GitHub", tools, selection=request, context={},
        )


def test_service_members_with_missing_connections_remain_unavailable() -> None:
    """Positive intent cannot restore a tool disabled by connection readiness."""
    text, request = _frontend_service_request()
    tools = [dict(tool) for tool in SERVICE_TOOLS]
    tools[1]["enabled"] = False
    decision = ToolSelectionService(settings={}).select(
        text, tools, selection=request, context={},
    )
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["github.search_code"]
