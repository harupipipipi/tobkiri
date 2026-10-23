from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.tool.executor import ToolExecutor  # noqa: E402
from domain.tool.external_connector_tools import (  # noqa: E402
    _dry_or_commands,
    github_issue_list,
    github_issue_update,
    jira_issue_sync,
    linear_issue_sync,
)
from domain.tool.registry import ToolRegistry  # noqa: E402


def _data(result: dict) -> dict:
    assert result["is_error"] is False
    return result["widget"]["data"]


def test_github_issue_update_builds_multi_step_dry_run_plan():
    data = _data(
        github_issue_update(
            {
                "issue_number": "42",
                "repo": "owner/repo",
                "title": "Ship agent task board",
                "status": "Ready For Review",
                "assignee": "alice",
                "comment": "Synced from task board card card-1",
            }
        )
    )

    assert data["dry_run"] is True
    assert data["tool"] == "github_issue_update"
    assert data["commands"][0] == [
        "gh",
        "issue",
        "edit",
        "42",
        "--repo",
        "owner/repo",
        "--title",
        "Ship agent task board",
        "--add-assignee",
        "alice",
        "--add-label",
        "status:ready-for-review",
    ]
    assert data["commands"][1] == [
        "gh",
        "issue",
        "comment",
        "42",
        "--body",
        "Synced from task board card card-1",
        "--repo",
        "owner/repo",
    ]
    assert data["payload"]["issue"] == "42"


def test_github_issue_update_maps_closed_status_to_state_command_after_edits():
    data = _data(github_issue_update({"issue": "77", "title": "Done", "status": "closed"}))

    assert data["commands"][0][:5] == ["gh", "issue", "edit", "77", "--title"]
    assert data["commands"][1] == ["gh", "issue", "close", "77"]


def test_github_issue_list_builds_read_only_filtered_plan():
    data = _data(
        github_issue_list(
            {
                "repo": "owner/repo",
                "status": "Needs Review",
                "assignee": "@me",
                "labels": ["agent", "task-board"],
                "search": "sync",
                "limit": 12,
            }
        )
    )

    assert data["dry_run"] is True
    assert data["command"] == [
        "gh",
        "issue",
        "list",
        "--repo",
        "owner/repo",
        "--limit",
        "12",
        "--assignee",
        "@me",
        "--label",
        "agent",
        "--label",
        "task-board",
        "--label",
        "status:needs-review",
        "--search",
        "sync",
    ]


def test_linear_and_jira_sync_return_redacted_dry_run_payloads():
    linear = _data(
        linear_issue_sync(
            {
                "key": "LIN-123",
                "title": "Review task plan",
                "status": "In Progress",
                "assignee": "alice",
                "comment": "Started from card-1",
                "metadata": {"api_token": "secret-token", "task_board_card_id": "card-1"},
            }
        )
    )
    jira = _data(jira_issue_sync({"issue_id": "OPS-9", "description": "Blocked", "metadata": {"secret": "s"}}))

    assert linear["payload"]["connector_required"] == "linear"
    assert linear["payload"]["metadata"]["api_token"] == "[redacted]"
    assert linear["commands"][0][:4] == ["linear", "issue", "update", "LIN-123"]
    assert linear["commands"][1] == ["linear", "issue", "comment", "LIN-123", "--body", "Started from card-1"]
    assert jira["payload"]["connector_required"] == "jira"
    assert jira["payload"]["metadata"]["secret"] == "[redacted]"
    assert jira["commands"][0][:4] == ["jira", "issue", "update", "OPS-9"]


@pytest.fixture
def defaultspack_tools_selected(monkeypatch):
    """Opt these registry tests into the explicitly selected defaultspack owner."""

    from core_runtime import resolved_profile_scope
    from domain.components import registry as component_registry
    from domain.tool import registry as tool_registry

    selected = frozenset({"defaultspack", "rumi_default_tools_pack"})
    monkeypatch.setattr(resolved_profile_scope, "effective_pack_ids", lambda: selected)
    monkeypatch.setattr(tool_registry, "effective_pack_ids", lambda: selected)
    monkeypatch.setattr(component_registry, "effective_pack_ids", lambda: selected)
    component_registry.get_domain_component_registry(force_reload=True)
    tool_registry.ToolRegistry._instance = None
    yield
    tool_registry.ToolRegistry._instance = None


def test_issue_sync_manifests_load_with_connector_approval_policy(
    defaultspack_tools_selected,
):
    ToolRegistry._instance = None
    registry = ToolRegistry()

    update_tool = registry.get("github_issue_update")
    list_tool = registry.get("github_issue_list")
    linear_tool = registry.get("linear_issue_sync")
    jira_tool = registry.get("jira_issue_sync")

    assert update_tool["requires_approval"] is True
    assert update_tool["risk"] == "high"
    assert list_tool["requires_approval"] is True
    assert list_tool["write_action"] is False
    assert linear_tool["risk"] == "high"
    assert jira_tool["execution"]["handler"].endswith(":jira_issue_sync")


def test_issue_sync_executor_requires_approval_then_returns_dry_run_when_approved(
    defaultspack_tools_selected,
    defaultspack_capability_plan_context,
):
    ToolRegistry._instance = None

    approval = ToolExecutor().execute(
        "github_issue_list",
        {"repo": "owner/repo"},
        defaultspack_capability_plan_context("github_issue_list"),
    )
    assert approval["is_error"] is False
    assert approval["widget"]["approval_required"] is True

    from domain.tool_policy.internal_context import mark_trusted_profile_policy_context

    approved_context = mark_trusted_profile_policy_context(
        {
            **defaultspack_capability_plan_context("github_issue_list"),
            "profile_policy": {"yolo_mode": True},
            "pack_id": "defaultspack",
        }
    )
    executed = ToolExecutor().execute(
        "github_issue_list",
        {"repo": "owner/repo"},
        approved_context,
    )
    assert executed["is_error"] is False
    assert executed["widget"]["data"]["dry_run"] is True


def test_connector_command_execution_surfaces_nonzero_exit_as_error():
    result = _dry_or_commands(
        {"execute": True, "timeout": 15},
        [[sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]],
        "github_issue_list",
    )

    assert result["is_error"] is True
    assert result["widget"]["error"]["code"] == "COMMAND_FAILED"
    assert result["widget"]["error"]["data"]["exit_code"] == 3
