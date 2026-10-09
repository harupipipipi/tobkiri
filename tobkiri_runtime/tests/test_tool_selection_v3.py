from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _selection(**overrides):
    from domain.chat.tool_selection_schema import ToolSelectionRequest

    request = ToolSelectionRequest()
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


def _target(kind: str, target_id: str):
    from domain.chat.tool_selection_schema import ToolTarget

    return ToolTarget(kind, target_id)


def _tools():
    return [
        {
            "tool_id": "github.search_code",
            "name": "GitHub Search Code",
            "summary": "Search repository code and pull request files.",
            "tags": ["github", "search", "code"],
        },
        {
            "tool_id": "github.create_issue",
            "name": "GitHub Create Issue",
            "summary": "Create a GitHub issue.",
            "tags": ["github", "issue", "create"],
        },
        {
            "tool_id": "web_search",
            "name": "Web Search",
            "summary": "Search web pages for current information.",
            "tags": ["web", "search"],
        },
        {
            "tool_id": "computer_use",
            "name": "Computer Use",
            "summary": "Control the local computer screen and browser.",
            "tags": ["computer", "browser"],
        },
    ]


def test_manual_service_include_and_tool_exclude():
    from domain.chat.tool_selection_service import ToolSelectionService

    decision = ToolSelectionService().select(
        "GitHubの実装を確認して",
        _tools(),
        selection=_selection(
            mode="manual",
            include=[_target("service", "github")],
            exclude=[_target("tool", "github.create_issue")],
        ),
        context={"principal_capabilities": ["developer"]},
    )

    assert decision.mode == "manual"
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["github.search_code"]
    assert decision.unknown_targets == []


def test_none_mode_selects_no_tools():
    from domain.chat.tool_selection_service import ToolSelectionService

    decision = ToolSelectionService().select(
        "webで検索して",
        _tools(),
        selection=_selection(mode="none"),
        context={"principal_capabilities": ["developer"]},
    )

    assert decision.selected_tools == []
    assert decision.provider_schema_count == 0
    assert decision.stage == "none"


def test_block_permission_removes_candidate_but_confirm_remains_selectable():
    from domain.chat.tool_selection_service import ToolSelectionService

    service = ToolSelectionService(
        settings={
            "tools": {
                "selection_strategy": "all_schemas",
                "tool_permission_overrides": {"github.search_code": "block"},
            }
        }
    )

    decision = service.select(
        "GitHub issueを作成して",
        _tools(),
        selection=_selection(mode="auto", strategy="all_schemas"),
        context={"principal_capabilities": ["developer"]},
    )
    selected_ids = [tool["tool_id"] for tool in decision.selected_tools]

    assert "github.search_code" not in selected_ids
    assert "github.create_issue" in selected_ids
    assert decision.permission_summary["confirm"] >= 1


def test_computer_tool_requires_explicit_intent():
    from domain.chat.tool_selection_service import ToolSelectionService

    service = ToolSelectionService(settings={"tools": {"selection_strategy": "all_schemas"}})
    without_intent = service.select(
        "検索して要約して",
        _tools(),
        selection=_selection(mode="auto", strategy="all_schemas"),
        context={"principal_capabilities": ["developer"]},
    )
    with_intent = service.select(
        "Chromeを開いて画面操作して",
        _tools(),
        selection=_selection(mode="auto", strategy="all_schemas"),
        context={
            "user_requested_computer_use": True,
            "principal_capabilities": ["developer"],
        },
    )

    assert "computer_use" not in [tool["tool_id"] for tool in without_intent.selected_tools]
    assert "computer_use" in [tool["tool_id"] for tool in with_intent.selected_tools]


def test_unknown_connection_status_fails_closed():
    from domain.chat.tool_selection_service import ToolSelectionService
    from domain.tool.service_catalog import infer_connection_status

    tools = [
        {
            "tool_id": "unknown_connector",
            "name": "Unknown Connector",
            "summary": "Should not be selected when status is ambiguous.",
            "availability": {"status": "maybe_later"},
            "tags": ["search"],
        },
        {
            "tool_id": "web_search",
            "name": "Web Search",
            "summary": "Search web pages.",
            "tags": ["web", "search"],
        },
    ]

    assert infer_connection_status(tools[0]) == "unavailable"
    decision = ToolSelectionService(settings={"tools": {"selection_strategy": "all_schemas"}}).select(
        "search",
        tools,
        selection=_selection(mode="auto", strategy="all_schemas"),
        context={"principal_capabilities": ["developer"]},
    )

    assert [tool["tool_id"] for tool in decision.selected_tools] == ["web_search"]


def test_conversation_preferences_apply_when_turn_has_no_override():
    from domain.chat.tool_selection_service import ToolSelectionService

    decision = ToolSelectionService().select(
        "GitHubを確認して",
        _tools(),
        selection=_selection(),
        context={
            "principal_capabilities": ["developer"],
            "conversation_tool_preferences": {
                "mode": "manual",
                "include": [{"kind": "service", "id": "github"}],
                "exclude": [{"kind": "tool", "id": "github.create_issue"}],
            }
        },
    )

    assert decision.mode == "manual"
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["github.search_code"]


def test_semantic_index_falls_back_to_lexical_and_reuses_cache(tmp_path):
    from domain.chat.tool_embedding_index import ToolEmbeddingIndex

    index = ToolEmbeddingIndex(pack_root=tmp_path)
    first = index.search("GitHub code search", _tools(), limit=3, backend="embedding")
    second = index.search("GitHub code search", _tools(), limit=3, backend="embedding")

    assert first["stage"] == "lexical_fallback"
    assert first["fallback_reason"] == "embedding_model_not_configured"
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert "github.search_code" in second["tool_ids"]
