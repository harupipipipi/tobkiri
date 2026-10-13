from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


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

    decision = ToolSelectionService(settings={}).select(
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

    decision = ToolSelectionService(settings={}).select(
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


@pytest.mark.parametrize(
    "blocked_tool_id", [None, "coding_terminal_exec", "python_sandbox_execute"]
)
def test_explicit_coding_activity_selects_native_sandbox_and_keeps_permissions(
    blocked_tool_id: str | None,
) -> None:
    """Coding advertises the selected native sandbox and honors tool blocks."""
    from domain.capability.activity_registry import ActivityRegistry
    from domain.chat.tool_selection_service import ToolSelectionService
    from domain.tool.registry import ToolRegistry

    legacy_sandbox_tool_ids = {
        "sandbox_file_read",
        "sandbox_file_write",
        "sandbox_file_patch",
        "sandbox_diff_preview",
        "sandbox_terminal_exec",
        "sandbox_artifact_export",
    }
    host_tool_ids = {
        "coding_file_list",
        "coding_file_read",
        "coding_file_search",
        "coding_file_create",
        "coding_file_write",
        "coding_file_patch",
        "coding_file_delete",
        "coding_file_restore",
        "coding_terminal_exec",
        "coding_git_status",
        "coding_git_diff",
        "coding_git_commit",
        "coding_git_push",
    }
    selectable_ids = {
        "coding_file_create", "coding_terminal_exec", "python_sandbox_execute"
    }
    tools_root = ROOT / "ecosystem" / "rumi_default_tools_pack" / "tools"
    tools = []
    for tool_id in sorted(selectable_ids | legacy_sandbox_tool_ids):
        manifest = json.loads(
            (tools_root / tool_id / "manifest.json").read_text(encoding="utf-8")
        )
        tool = ToolRegistry._tool_from_manifest(
            manifest, source_pack_id="rumi_default_tools_pack"
        )
        assert tool is not None, tool_id
        tools.append(tool)
    expansion = ActivityRegistry().expand(["coding"], tools)
    overrides = {blocked_tool_id: "block"} if blocked_tool_id else {}
    service = ToolSelectionService(
        settings={
            "tools": {
                "selection_strategy": "all_schemas",
                "tool_permission_overrides": overrides,
            }
        }
    )

    decision = service.select(
        "@coding create a small workspace file and run its Python script",
        tools,
        selection=_selection(mode="auto", strategy="all_schemas"),
        context={"principal_capabilities": ["developer"]},
    )
    selected_ids = {tool["tool_id"] for tool in decision.selected_tools}

    assert host_tool_ids.issubset(expansion["tool_ids"])
    assert "python_sandbox_execute" in expansion["tool_ids"]
    assert legacy_sandbox_tool_ids.isdisjoint(expansion["tool_ids"])
    assert selected_ids == selectable_ids - {blocked_tool_id}
    assert decision.permission_summary["confirm"] >= 2
    native_tool = next(
        tool for tool in tools if tool["tool_id"] == "python_sandbox_execute"
    )
    assert native_tool["requires_approval"] is True
    assert native_tool["execution"]["type"] == "global_contract"


def test_coding_native_sandbox_schema_and_selected_profile_edges() -> None:
    """The advertised tool resolves to the native factory and jailed guest route."""
    from jsonschema import Draft202012Validator, ValidationError

    from ecosystem.rumi_default_tools_pack.runtime import python_sandbox_execute
    from tobkiri_host.artifact_compiler import compile_pack_root

    manifest = json.loads(
        (
            ROOT / "ecosystem" / "rumi_default_tools_pack" / "tools"
            / "python_sandbox_execute" / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    execution = manifest["config"]["execution"]
    assert python_sandbox_execute.HOST_PROVIDER_FACTORY.function_id == execution[
        "provider_instance_id"
    ]
    parameters = manifest["config"]["schema"]["parameters"]
    validator = Draft202012Validator(parameters)
    arguments = {
        "mode": "script",
        "script_path": "cli.py",
        "arguments": [],
        "stdin": "",
        "cwd": ".",
        "timeout_seconds": 10,
    }
    validator.validate(arguments)
    assert python_sandbox_execute.validate_arguments(arguments) == arguments
    # The saved conversation owner supplies the workspace binding. Tool args
    # cannot choose another workspace or supply execution authority.
    for extra in ({"workspace_id": "other"}, {"approved": True}):
        with pytest.raises(ValidationError):
            validator.validate({**arguments, **extra})
        with pytest.raises(ValueError):
            python_sandbox_execute.validate_arguments({**arguments, **extra})
    with pytest.raises(ValidationError):
        validator.validate({**arguments, "script_path": "../cli.py"})

    profile = json.loads(
        (DEFAULTSPACK_ROOT / "v4" / "defaults.profile.v5.json").read_text(
            encoding="utf-8"
        )
    )
    edges = profile["requested_edges"]
    host_edge = next(
        edge for edge in edges
        if edge["target_provider_id"] == execution["provider_instance_id"]
        and edge["contract_id"] == execution["contract_id"]
        and edge["operation_id"] == execution["operation"]
    )
    assert host_edge["authority_mode"] == "profile_grant"
    guest_edge = next(
        edge for edge in edges
        if edge["caller_function_id"] == execution["provider_instance_id"]
        and edge["contract_id"] == python_sandbox_execute.GUEST_CONTRACT
        and edge["operation_id"] == python_sandbox_execute.GUEST_OPERATION
        and edge["target_provider_id"] == python_sandbox_execute.GUEST_FUNCTION
    )
    assert guest_edge["authority_mode"] == "profile_grant"
    guest_pack = compile_pack_root(DEFAULTSPACK_ROOT)
    assert (
        python_sandbox_execute.GUEST_CONTRACT,
        python_sandbox_execute.GUEST_OPERATION,
    ) in guest_pack.routes


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

    decision = ToolSelectionService(settings={}).select(
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
