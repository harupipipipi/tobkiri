from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


UUIDISH_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _reset_team_workspace_singletons() -> None:
    from domain.agent_runtime.run_store import AgentRunStore
    from domain.company.runtime_store import CompanyRuntimeStore
    from domain.company.store import CompanyStore
    from domain.input import action_registry
    from domain.tool.registry import ToolRegistry
    from domain.tool.runtime_creator import RuntimeToolCreator

    AgentRunStore._instance = None
    CompanyRuntimeStore._instance = None
    CompanyStore._instance = None
    ToolRegistry._instance = None
    RuntimeToolCreator._instance = None
    action_registry._DEFAULT_REGISTRY = None


def _configure_temp_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(tmp_path / "agent_runtime"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "coding_workspaces.json"))
    _reset_team_workspace_singletons()


def _create_workspace(*, settings: dict[str, Any] | None = None) -> tuple[Any, Any, dict[str, Any]]:
    from domain.company.contract_facade import CompanyContractFacade
    from domain.company.runtime_store import CompanyRuntimeStore
    from domain.company.store import CompanyStore

    store = CompanyStore()
    runtime_store = CompanyRuntimeStore()
    store.create_company(
        company_id="tw_short",
        name="Subagent Team",
        settings=settings,
        metadata={"surface": "subagent_team_workspace"},
    )
    canonical_company = CompanyContractFacade(
        {
            "company_id": "tw_short",
            "name": "Subagent Team",
            "settings": settings or {},
            "metadata": {"surface": "subagent_team_workspace"},
        },
        {},
    ).run("create")
    return store, runtime_store, canonical_company


def _trusted_coding_workspace(workspace: Path, workspace_id: str = "trusted-team-workspace") -> str:
    from domain.coding.workspace_store import WorkspaceStore

    WorkspaceStore().create(workspace, workspace_id=workspace_id, trusted=True)
    return workspace_id


def test_team_workspace_short_ids_are_stable_and_non_uuid():
    from domain.subagent_team.ids import ensure_short_id, generate_short_id, slug_id, stable_short_id
    from domain.subagent_team.normalizers import normalize_team_agent, normalize_team_channel

    first = stable_short_id("ag", "Coder Kai")
    repeated = stable_short_id("ag", "Coder Kai")
    generated = generate_short_id("ag", existing=[first], length=7)
    ensured, metadata = ensure_short_id({}, prefix="ag", seed="Coder Kai", existing=[first])

    assert first == repeated
    assert generated != first
    assert ensured != first
    assert metadata["short_id"] == ensured
    for public_id in (first, generated, ensured, slug_id("Ship Room!!!", max_length=24)):
        assert len(public_id) <= 24
        assert UUIDISH_ID_RE.search(public_id) is None
        assert "/" not in public_id and " " not in public_id

    agent = normalize_team_agent({"agent_id": "coder_kai", "display_name": "Coder Kai"})
    channel = normalize_team_channel({"id": "ship-room", "name": "Ship Room"})
    assert agent["metadata"]["short_id"].startswith("ag_")
    assert channel["metadata"]["short_id"].startswith("ch_")


def test_rich_policy_caps_active_subagents_when_rich_is_off(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    _create_workspace()

    from domain.subagent_team.rich_policy import RichPolicy, evaluate_rich_payload

    payload = evaluate_rich_payload(
        {
            "content": "/rich " + ("x" * 30),
            "rich_payload": {"blocks": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
            "attachments": [{"name": "a"}, {"name": "b"}],
        },
        policy=RichPolicy(max_text_chars=12, max_blocks=2, max_attachments=1),
    )

    assert payload["requested"] is True
    assert payload["clipped"] is True
    assert payload["result"] == {"content_chars": 12, "blocks": 2, "attachments": 1}
    assert payload["original"] == {"content_chars": 36, "blocks": 3, "attachments": 2}
    assert payload["content"].endswith("...")


def test_pm_gate_blocks_large_or_risky_goals_until_pm_approval():
    from domain.subagent_team.pm_gate import gated_content, pm_gate_decision

    gate = pm_gate_decision(
        sender_id="user",
        content="@coding_engineer implement and push",
        target_agent_ids=["coding_engineer", "reviewer"],
        rich_requested=True,
    )

    assert gate["requires_pm"] is True
    assert gate["requested_target_agent_ids"] == ["coding_engineer", "reviewer"]
    assert gate["target_agent_ids"] == ["project_manager"]
    assert gate["route"] == "pm_gate"
    gated = gated_content(
        content="@coding_engineer implement and push",
        sender_id="user",
        gate=gate,
    )
    assert gated.startswith("@project_manager PM gate request")
    assert "@coding_engineer" not in gated
    assert "at coding_engineer" in gated

    pm_gate = pm_gate_decision(
        sender_id="project_manager",
        content="@coding_engineer implement",
        target_agent_ids=["coding_engineer"],
    )
    assert pm_gate["requires_pm"] is False
    assert pm_gate["target_agent_ids"] == ["coding_engineer"]


def test_creator_preview_returns_plan_without_workspace_side_effects(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    before_agents = store.list_agents(company["id"]) or []
    before_channels = store.list_channels(company["id"]) or []
    before_tasks, before_task_total = runtime_store.list_tasks(company["id"], limit=200)
    before_messages, before_message_total = runtime_store.list_messages(company["id"], limit=200)

    preview = service.creator_preview(
        company["id"],
        {
            "action": "message",
            "content": "@coding_engineer implement the upload fix with /rich context",
            "sender_id": "user",
            "target_agent_ids": ["coding_engineer"],
            "rich": True,
            "approved": True,
        },
    )

    after_agents = store.list_agents(company["id"]) or []
    after_channels = store.list_channels(company["id"]) or []
    after_tasks, after_task_total = runtime_store.list_tasks(company["id"], limit=200)
    after_messages, after_message_total = runtime_store.list_messages(company["id"], limit=200)

    assert preview is not None
    assert preview["will_execute_tools"] is False
    assert preview["routing"]["direct_tool_execution"] is False
    assert preview["routing"]["target_agent_ids"] == ["project_manager"]
    assert preview["pm_gate"]["requires_pm"] is True
    assert preview["rich"]["requested"] is True
    assert preview["lifecycle"]["managed_by"] == "creator"
    assert preview["lifecycle"]["approval_bypass"] is False
    assert len(after_agents) == len(before_agents)
    assert len(after_channels) == len(before_channels)
    assert before_task_total == after_task_total
    assert before_message_total == after_message_total
    assert before_tasks == after_tasks
    assert before_messages == after_messages


def test_file_tree_payload_hides_absolute_workspace_root(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "team-workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello\n", encoding="utf-8")
    workspace_id = _trusted_coding_workspace(workspace)

    from domain.subagent_team.file_tree import build_file_tree

    payload = build_file_tree(
        {"workspace_id": workspace_id, "directory": ".", "include_git": False},
        {},
    )
    encoded = json.dumps(payload, sort_keys=True)

    assert str(workspace.resolve()) not in encoded
    assert "/Users/" not in encoded
    assert payload["root"] == "."
    assert str(payload["workspace_root"]).startswith("workspace:")
    assert str(payload["workspace_id"]) == workspace_id
    assert payload["files"][0]["path"] == "notes.txt"


def test_file_tree_open_returns_sanitized_file_preview_and_channel_history(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()
    workspace = tmp_path / "team-workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(f"root={workspace.resolve()}\nhello\n", encoding="utf-8")
    workspace_id = _trusted_coding_workspace(workspace)

    from blocks.subagent_team import file_tree as file_tree_block

    raw_open = file_tree_block.run(
        {
            "action": "open",
            "workspace_root": str(workspace),
            "path": "notes.txt",
            "include_git": False,
        },
        {},
    )
    assert raw_open["status"] == "error"
    assert raw_open["error"]["code"] == "WORKSPACE_UNTRUSTED"

    opened_file = file_tree_block.run(
        {
            "action": "open",
            "workspace_id": workspace_id,
            "path": "notes.txt",
            "include_git": False,
        },
        {},
    )
    assert opened_file["status"] == "ok"
    assert opened_file["data"]["kind"] == "file"
    assert opened_file["data"]["path"] == "notes.txt"
    assert str(workspace.resolve()) not in json.dumps(opened_file["data"], sort_keys=True)
    assert opened_file["data"]["workspace_root"].startswith("workspace:")
    assert "hello" in opened_file["data"]["preview"]

    runtime_store.add_message(
        company["id"],
        channel_id="ops-company",
        sender_id="project_manager",
        content="history entry",
    )
    opened_history = file_tree_block.run(
        {
            "action": "open",
            "company_id": company["id"],
            "node_type": "channel",
            "node_id": "channel:ops-company",
        },
        {"actor_id": "project_manager"},
    )
    assert opened_history["status"] == "ok"
    assert opened_history["data"]["kind"] == "channel"
    assert opened_history["data"]["messages"][0]["content"] == "history entry"
    assert "/Users/" not in json.dumps(opened_history["data"], sort_keys=True)


def test_file_tree_includes_team_workspace_virtual_history(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()
    workspace = tmp_path / "team-workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello\n", encoding="utf-8")
    workspace_id = _trusted_coding_workspace(workspace)

    from domain.subagent_team.service import SubagentTeamService
    from domain.subagent_team.file_tree import build_file_tree

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    created = service.creator_request(
        company["id"],
        {"action": "create_team", "team_size": 2, "channel_name": "History Room"},
    )
    service.ensure_dm(
        company["id"],
        {
            "sender_id": created["agents"][0]["agent_id"],
            "agent_id": created["agents"][1]["agent_id"],
        },
    )

    tree = build_file_tree(
        {"workspace_id": workspace_id, "company_id": company["id"], "include_git": False},
        {},
    )

    encoded = json.dumps(tree["team_workspace"], sort_keys=True)
    assert "channels/{channel}/conversation.md" in tree["team_workspace"]["paths"]
    assert "messages.jsonl" in encoded
    assert "approvals" in encoded
    assert "artifacts" in encoded
    assert "team-workspace/agents/" in encoded
    assert "runs" in encoded
    assert "dms" in encoded


def test_channel_check_context_includes_membership_pm_gate_and_rich_policy(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    service.upsert_agent(company["id"], {"agent_id": "coding_engineer", "display_name": "Coding Engineer"})
    channel = service.upsert_channel(
        company["id"],
        {"id": "ship-room", "name": "Ship Room", "members": ["project_manager", "coding_engineer"]},
    )
    runtime_store.add_message(
        company["id"],
        channel_id="ship-room",
        sender_id="user",
        content="@coding_engineer please continue",
    )
    runtime_store.create_task(
        company["id"],
        channel_id="ship-room",
        title="Upload fix",
        description="Continue implementation",
        target_agent_ids=["coding_engineer"],
        status="queued",
    )

    context = service.channel_check(company["id"], {"channel_id": channel["id"], "limit": 10})

    assert context is not None
    assert context["kind"] == "channel.check"
    assert context["company_id"] == company["id"]
    assert context["channel"]["id"] == "ship-room"
    assert context["message_total"] == 1
    assert context["task_total"] == 1
    assert context["open_tasks"][0]["target_agent_ids"] == ["coding_engineer"]
    assert "Use PM gates for direct specialist work from non-PM senders." in context["instructions"]
    assert context["allowed"] is True
    assert context["agent_is_member"] is False
    assert context["membership"]["is_member"] is False
    assert context["target_membership"]["all_targets_are_members"] is True
    assert context["pm_required"] is False
    assert context["rich_allowed"] is True
    assert context["task_completion_condition"]["pm_receipt_grants_user_approval"] is False


def test_channel_and_dm_history_reads_require_trusted_member_or_pm_actor(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService
    from blocks.subagent_team import dms as dms_block

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    service.upsert_channel(
        company["id"],
        {"id": "private-build", "name": "Private Build", "members": ["project_manager", "coding_engineer"]},
    )
    runtime_store.add_message(
        company["id"],
        channel_id="private-build",
        sender_id="project_manager",
        content="private history",
    )

    missing_actor = service.list_messages(company["id"], {"channel_id": "private-build"})
    outsider = service.list_messages(company["id"], {"channel_id": "private-build"}, context={"actor_id": "research_specialist"})
    member = service.list_messages(company["id"], {"channel_id": "private-build"}, context={"actor_id": "coding_engineer"})
    pm = service.list_messages(company["id"], {"channel_id": "private-build"}, context={"actor_id": "project_manager"})

    assert missing_actor["denied"] is True
    assert missing_actor["code"] == "ACTOR_REQUIRED"
    assert outsider["denied"] is True
    assert outsider["code"] == "CHANNEL_MEMBERSHIP_REQUIRED"
    assert member[0][0]["content"] == "private history"
    assert pm[0][0]["content"] == "private history"

    dm = service.ensure_dm(
        company["id"],
        {"sender_id": "project_manager", "agent_id": "coding_engineer"},
    )
    runtime_store.add_message(
        company["id"],
        channel_id=dm["id"],
        sender_id="project_manager",
        content="dm history",
    )
    dm_outsider = service.list_messages(company["id"], {"channel_id": dm["id"]}, context={"actor_id": "reviewer"})
    dm_member = service.list_messages(company["id"], {"channel_id": dm["id"]}, context={"actor_id": "coding_engineer"})

    assert dm_outsider["denied"] is True
    assert dm_outsider["code"] == "DM_PARTICIPANT_REQUIRED"
    assert dm_member[0][0]["content"] == "dm history"

    block_member = dms_block.run(
        {"company_id": company["id"], "action": "messages", "dm_id": dm["id"]},
        {"actor_id": "coding_engineer"},
    )
    block_outsider = dms_block.run(
        {"company_id": company["id"], "action": "messages", "dm_id": dm["id"]},
        {"actor_id": "reviewer"},
    )
    assert block_member["status"] == "ok"
    assert block_member["data"]["messages"][0]["content"] == "dm history"
    assert block_outsider["status"] == "error"
    assert block_outsider["error"]["code"] == "DM_PARTICIPANT_REQUIRED"


def test_archived_dm_cannot_be_read_or_reactivated_by_send(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from blocks.subagent_team import channels as channels_block
    from blocks.subagent_team import dms as dms_block
    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    dm = service.ensure_dm(
        company["id"],
        {"sender_id": "project_manager", "agent_id": "coding_engineer"},
    )
    runtime_store.add_message(
        company["id"],
        channel_id=dm["id"],
        sender_id="project_manager",
        content="archived dm history",
    )

    archived = channels_block.run(
        {"company_id": company["id"], "action": "archive", "channel_id": dm["id"]},
        {"actor_id": "project_manager"},
    )
    assert archived["status"] == "ok"
    assert archived["data"]["channel"]["metadata"]["lifecycle"]["state"] == "archived"

    read_archived = dms_block.run(
        {"company_id": company["id"], "action": "messages", "dm_id": dm["id"]},
        {"actor_id": "coding_engineer"},
    )
    send_archived = dms_block.run(
        {
            "company_id": company["id"],
            "action": "send",
            "dm_id": dm["id"],
            "sender_id": "project_manager",
            "agent_id": "coding_engineer",
            "content": "should not revive",
        },
        {"actor_id": "project_manager"},
    )
    stored = store.get_channel(company["id"], dm["id"])

    assert read_archived["status"] == "error"
    assert read_archived["error"]["code"] == "DM_ARCHIVED"
    assert send_archived["status"] == "error"
    assert send_archived["error"]["code"] == "DM_ARCHIVED"
    assert stored["visibility"] == "archived"
    assert stored["metadata"]["lifecycle"]["state"] == "archived"
    assert "should not revive" not in json.dumps(
        runtime_store.list_messages(company["id"], channel_id=dm["id"])[0],
        sort_keys=True,
    )


def test_pm_decision_requires_stored_manager_actor_and_ignores_client_approval_flags(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from blocks.subagent_team import goals as goals_block

    task = runtime_store.create_task(
        company["id"],
        channel_id="ops-company",
        title="Ship guarded change",
        description="Needs approval",
        target_agent_ids=["coding_engineer"],
        source="goal",
        status="waiting_approval",
    )

    worker_approve = goals_block.run(
        {
            "company_id": company["id"],
            "action": "approve",
            "goal_id": task["task_id"],
            "actor_id": "coding_engineer",
            "approved": True,
            "_tool_server_approved": True,
            "approval_token": "client-token",
        },
        {},
    )
    assert worker_approve["status"] == "error"
    assert worker_approve["error"]["code"] == "ACTOR_REQUIRED"

    spoofed_pm = goals_block.run(
        {
            "company_id": company["id"],
            "action": "approve",
            "goal_id": task["task_id"],
            "actor_id": "project_manager",
            "approved": True,
            "approval_token": "client-token",
        },
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_pm["status"] == "error"
    assert spoofed_pm["error"]["code"] == "FORBIDDEN"

    worker_complete = goals_block.run(
        {
            "company_id": company["id"],
            "action": "task_complete",
            "task_id": task["task_id"],
            "actor_id": "reviewer",
            "approved": True,
        },
        {"actor_id": "reviewer"},
    )
    assert worker_complete["status"] == "error"
    assert worker_complete["error"]["code"] == "FORBIDDEN"

    pm_approve = goals_block.run(
        {
            "company_id": company["id"],
            "action": "approve",
            "goal_id": task["task_id"],
            "actor_id": "project_manager",
        },
        {"actor_id": "project_manager"},
    )
    assert pm_approve["status"] == "ok"
    approved_task = pm_approve["data"]
    metadata = approved_task["metadata"]
    assert approved_task["status"] == "queued"
    assert metadata["approval"] == "approved"
    assert metadata["approval_receipt_id"].startswith("pmr_")
    assert metadata["approval_receipt"]["actor_id"] == "project_manager"
    assert metadata["approval_receipt"]["grants_user_approval"] is False


def test_goal_generic_update_cannot_bypass_pm_decision_route(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    _, runtime_store, company = _create_workspace()

    from blocks.subagent_team import goals as goals_block

    task = runtime_store.create_task(
        company["id"],
        channel_id="ops-company",
        title="Guarded goal",
        description="Needs PM decision",
        target_agent_ids=["coding_engineer"],
        source="goal",
        status="waiting_approval",
    )

    blocked_status = goals_block.run(
        {
            "company_id": company["id"],
            "action": "update",
            "goal_id": task["task_id"],
            "updates": {"status": "queued", "approved": True},
        },
        {"actor_id": "coding_engineer"},
    )
    blocked_metadata = goals_block.run(
        {
            "company_id": company["id"],
            "action": "update",
            "goal_id": task["task_id"],
            "updates": {"metadata": {"approval": "approved", "approval_receipt_id": "client"}},
        },
        {"actor_id": "coding_engineer"},
    )
    safe_title = goals_block.run(
        {
            "company_id": company["id"],
            "action": "update",
            "goal_id": task["task_id"],
            "updates": {"title": "Retitled guarded goal"},
        },
        {"actor_id": "coding_engineer"},
    )
    stored = runtime_store.get_task(task["task_id"], company_id=company["id"])

    assert blocked_status["status"] == "error"
    assert blocked_status["error"]["code"] == "GOAL_DECISION_REQUIRED"
    assert blocked_metadata["status"] == "error"
    assert blocked_metadata["error"]["code"] == "GOAL_DECISION_REQUIRED"
    assert safe_title["status"] == "ok"
    assert safe_title["data"]["title"] == "Retitled guarded goal"
    assert stored["status"] == "waiting_approval"
    assert "approval" not in (stored.get("metadata") or {})


def test_rich_state_persists_and_creator_cannot_self_enable_or_exceed_cap(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from blocks.subagent_team import rich as rich_block
    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    status = service.rich_status(company["id"])
    assert status["rich_enabled"] is False

    blocked = service.creator_request(
        company["id"],
        {"action": "create_team", "team_size": 6, "channel_name": "Six Pack"},
    )
    assert blocked["denied"] is True
    assert blocked["code"] == "RICH_MODE_REQUIRED"

    creator_enable = rich_block.run(
        {"company_id": company["id"], "action": "set", "enabled": True, "actor_id": "creator"},
        {"actor_id": "creator"},
    )
    assert creator_enable["status"] == "error"
    assert creator_enable["error"]["code"] == "FORBIDDEN"

    spoofed_rich_enable = rich_block.run(
        {"company_id": company["id"], "action": "set", "enabled": True, "actor_id": "project_manager"},
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_rich_enable["status"] == "error"
    assert spoofed_rich_enable["error"]["code"] == "FORBIDDEN"

    pm_enable = rich_block.run(
        {"company_id": company["id"], "action": "set", "enabled": True, "actor_id": "project_manager"},
        {"actor_id": "project_manager"},
    )
    assert pm_enable["status"] == "ok"
    assert pm_enable["data"]["rich_enabled"] is True

    allowed = service.creator_request(
        company["id"],
        {"action": "create_team", "team_size": 6, "channel_name": "Six Pack"},
    )
    assert allowed["allowed"] is True
    assert allowed["rich_policy"]["enabled"] is True
    assert allowed["team_size"] == 6
    assert allowed["channel"]["metadata"]["subagent_team"]["pm_agent_id"]
    for agent in allowed["agents"]:
        assert UUIDISH_ID_RE.fullmatch(agent["agent_id"])
        metadata = agent["metadata"]
        nested = metadata["subagent_team"]
        assert nested["uuid"] == agent["agent_id"]
        assert nested["short_id"].startswith("sa-")
        assert nested["legacy_short_id"].startswith("ag_")
        assert nested["short_id"] in agent["aliases"]
        assert nested["legacy_short_id"] in agent["aliases"]
        assert UUIDISH_ID_RE.search(nested["short_id"]) is None
        assert "channel_check" in agent["allowed_tools"]
        assert all("." not in tool for tool in agent["allowed_tools"] if tool.startswith(("subagent", "channel")))
        assert nested["tool_aliases"]["channel_check"] == "channel.check"
        assert "Internal uuid:" in agent["system_prompt"]
        assert "Human-facing short id:" in agent["system_prompt"]
        assert "PM gate:" in agent["system_prompt"]

    persisted = rich_block.run({"company_id": company["id"], "action": "get"}, {})
    assert persisted["status"] == "ok"
    assert persisted["data"]["rich_enabled"] is True


def test_agents_post_uses_creator_and_keeps_legacy_slug_as_alias(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    _, _, company = _create_workspace()

    from blocks.subagent_team import agents as agents_block

    created = agents_block.run(
        {
            "company_id": company["id"],
            "action": "create",
            "agent": {
                "agent_id": "legacy_coder_slug",
                "display_name": "Legacy Coder",
                "role": "coder",
            },
        },
        {"actor_id": "subagent_creator"},
    )

    assert created["status"] == "ok"
    payload = created["data"]
    assert payload["status"] == "created"
    agent = payload["agents"][0]
    assert UUIDISH_ID_RE.fullmatch(agent["agent_id"])
    assert "legacy_coder_slug" in agent["aliases"]
    assert agent["metadata"]["subagent_team"]["legacy_alias"] == "legacy_coder_slug"


def test_channel_check_enforced_before_message_goal_and_dm_routing(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    channel = service.upsert_channel(
        company["id"],
        {"id": "pm-only", "name": "PM Only", "members": ["project_manager"]},
    )
    assert channel["id"] == "pm-only"

    message = service.send_message(
        company["id"],
        {
            "channel_id": "pm-only",
            "sender_id": "user",
            "content": "@coding_engineer please implement",
            "target_agent_ids": ["coding_engineer"],
        },
    )
    assert message["denied"] is True
    assert message["code"] == "TARGET_NOT_CHANNEL_MEMBER"

    goal = service.create_goal(
        company["id"],
        {
            "channel_id": "pm-only",
            "sender_id": "user",
            "title": "Implement safely",
            "description": "Please implement",
            "target_agent_ids": ["coding_engineer"],
        },
    )
    assert goal["denied"] is True
    assert goal["code"] == "TARGET_NOT_CHANNEL_MEMBER"

    creator_goal = service.creator_request(
        company["id"],
        {
            "action": "goal",
            "channel_id": "pm-only",
            "sender_id": "user",
            "title": "Implement through creator",
            "description": "Please implement",
            "target_agent_ids": ["coding_engineer"],
        },
    )
    assert creator_goal["denied"] is True
    assert creator_goal["code"] == "TARGET_NOT_CHANNEL_MEMBER"

    dm = service.send_dm(
        company["id"],
        {
            "sender_id": "user",
            "agent_id": "not_a_real_agent",
            "content": "hello",
        },
    )
    assert dm["denied"] is True
    assert dm["code"] == "TARGET_NOT_FOUND"


def test_sender_id_project_manager_spoof_cannot_bypass_pm_gate_for_message_dm_or_goal(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    before_dms = service.list_dms(company["id"]) or []

    message = service.send_message(
        company["id"],
        {
            "channel_id": "ops-company",
            "sender_id": "project_manager",
            "content": "@coding_engineer please implement",
            "target_agent_ids": ["coding_engineer"],
        },
    )
    assert message["message"]["sender_id"] == "user"
    assert message["message"]["metadata"]["subagent_team"]["pm_gate"]["requires_pm"] is True
    assert message["tasks"][0]["target_agent_ids"] in (["project_manager"], ["operations_manager"])

    dm = service.send_dm(
        company["id"],
        {
            "sender_id": "project_manager",
            "agent_id": "coding_engineer",
            "content": "please implement directly",
        },
    )
    assert dm["denied"] is True
    assert dm["code"] == "PM_REQUIRED"
    assert (service.list_dms(company["id"]) or []) == before_dms

    goal = service.create_goal(
        company["id"],
        {
            "channel_id": "ops-company",
            "sender_id": "project_manager",
            "title": "Implement safely",
            "description": "Please implement",
            "target_agent_ids": ["coding_engineer"],
        },
    )
    assert goal["status"] == "waiting_approval"
    assert goal["target_agent_ids"] in (["project_manager"], ["operations_manager"])
    assert goal["metadata"]["pm_gate"]["requires_pm"] is True


def test_trusted_context_actor_not_client_sender_controls_message_and_goal_authority(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    message = service.send_message(
        company["id"],
        {
            "channel_id": "ops-company",
            "sender_id": "project_manager",
            "content": "@coding_engineer please implement",
            "target_agent_ids": ["coding_engineer"],
        },
        context={"actor_id": "coding_engineer"},
    )
    assert message["message"]["sender_id"] == "coding_engineer"
    assert message["message"]["metadata"]["subagent_team"]["pm_gate"]["requires_pm"] is True
    assert message["tasks"][0]["target_agent_ids"] in (["project_manager"], ["operations_manager"])

    goal = service.create_goal(
        company["id"],
        {
            "channel_id": "ops-company",
            "sender_id": "project_manager",
            "title": "Implement safely",
            "description": "Please implement",
            "target_agent_ids": ["coding_engineer"],
        },
        context={"actor_id": "coding_engineer"},
    )
    assert goal["status"] == "waiting_approval"
    assert goal["target_agent_ids"] in (["project_manager"], ["operations_manager"])


def test_dm_send_resolves_human_short_id_to_internal_uuid(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace(settings={"subagent_team": {"rich_enabled": True}})

    def fake_dispatch_task(self, company_id, task_id, **kwargs):
        task = self.runtime_store.get_task(task_id, company_id=company_id)
        return {
            "task": task,
            "dispatch": {"status": "queued", "requested_by": kwargs.get("requested_by")},
            "results": [{"status": "queued"}],
            "run_links": [],
        }

    monkeypatch.setattr(
        "domain.company.run_dispatcher.CompanyRunDispatcher.dispatch_task",
        fake_dispatch_task,
    )

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    created = service.creator_request(
        company["id"],
        {"action": "create_team", "team_size": 2, "channel_name": "DM Short IDs"},
    )
    pm = next(agent for agent in created["agents"] if agent["metadata"]["subagent_team"]["role"] == "pm")
    worker = next(agent for agent in created["agents"] if agent["metadata"]["subagent_team"]["role"] != "pm")
    worker_uuid = worker["agent_id"]
    worker_short = worker["metadata"]["subagent_team"]["short_id"]

    routed = service.send_dm(
        company["id"],
        {
            "sender_id": pm["agent_id"],
            "agent_id": "@" + worker_short,
            "content": "Please inspect the latest change.",
        },
        context={"actor_id": pm["agent_id"]},
    )

    assert routed["channel_check"]["kind"] == "channel.check"
    assert routed["resolution"]["resolved_agent_ids"] == [worker_uuid]
    assert routed["tasks"][0]["target_agent_ids"] == [worker_uuid]
    assert routed["tasks"][0]["metadata"]["channel_check"]["target_agent_ids"] == [worker_uuid]
    assert UUIDISH_ID_RE.search(worker_short) is None
    assert worker_short.startswith("sa-")


def test_creator_safe_actions_record_channel_check_and_guard_main_lifecycle(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from blocks.subagent_team import agents as agents_block
    from blocks.subagent_team import creator as creator_block
    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    created = service.creator_request(
        company["id"],
        {"action": "create_team", "team_size": 2, "channel_name": "Safe Actions"},
    )
    channel_id = created["channel"]["id"]
    pm = next(agent for agent in created["agents"] if agent["metadata"]["subagent_team"]["role"] == "pm")
    worker = next(agent for agent in created["agents"] if agent["metadata"]["subagent_team"]["role"] != "pm")
    worker_short = worker["metadata"]["subagent_team"]["short_id"]

    status = creator_block.run(
        {"company_id": company["id"], "tool_id": "subagent_status", "channel_id": channel_id},
        {},
    )
    assert status["status"] == "ok"
    assert status["data"]["preview"]["provider_safe_action"] == "status"
    assert status["data"]["preview"]["channel_check"]["kind"] == "channel.check"

    routed = service.send_message(
        company["id"],
        {
            "channel_id": channel_id,
            "sender_id": pm["agent_id"],
            "content": "@" + worker_short + " please inspect",
        },
        context={"actor_id": pm["agent_id"]},
    )
    assert routed["channel_check"]["kind"] == "channel.check"
    assert routed["message"]["metadata"]["subagent_team"]["channel_check"]["kind"] == "channel.check"
    assert routed["tasks"][0]["metadata"]["channel_check"]["kind"] == "channel.check"

    blocked = agents_block.run(
        {"company_id": company["id"], "action": "create", "agent": {"display_name": "Direct Main", "role": "coder"}},
        {"actor_id": "main_agent"},
    )
    assert blocked["status"] == "error"
    assert blocked["error"]["code"] == "CREATOR_REQUIRED"


def test_direct_agent_channel_and_settings_mutations_require_trusted_pm_or_creator_context(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    _, _, company = _create_workspace()

    from blocks.subagent_team import agents as agents_block
    from blocks.subagent_team import channels as channels_block
    from blocks.subagent_team import dms as dms_block
    from blocks.subagent_team import rich as rich_block

    no_context_agent = agents_block.run(
        {"company_id": company["id"], "action": "create", "agent": {"display_name": "No Context"}},
        {},
    )
    assert no_context_agent["status"] == "error"
    assert no_context_agent["error"]["code"] == "ACTOR_REQUIRED"

    spoofed_agent = agents_block.run(
        {
            "company_id": company["id"],
            "action": "patch",
            "agent_id": "coding_engineer",
            "actor_id": "project_manager",
            "updates": {"display_name": "Spoofed"},
        },
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_agent["status"] == "error"
    assert spoofed_agent["error"]["code"] == "FORBIDDEN"

    no_context_channel = channels_block.run(
        {
            "company_id": company["id"],
            "action": "create",
            "channel": {"id": "no-context", "members": ["coding_engineer"]},
        },
        {},
    )
    assert no_context_channel["status"] == "error"
    assert no_context_channel["error"]["code"] == "ACTOR_REQUIRED"

    spoofed_channel = channels_block.run(
        {
            "company_id": company["id"],
            "action": "patch",
            "channel_id": "ops-company",
            "actor_id": "project_manager",
            "updates": {"description": "spoofed"},
        },
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_channel["status"] == "error"
    assert spoofed_channel["error"]["code"] == "FORBIDDEN"

    no_context_dm_create = dms_block.run(
        {
            "company_id": company["id"],
            "action": "create",
            "sender_id": "project_manager",
            "agent_id": "coding_engineer",
        },
        {},
    )
    assert no_context_dm_create["status"] == "error"
    assert no_context_dm_create["error"]["code"] == "ACTOR_REQUIRED"

    no_context_dm_ensure = dms_block.run(
        {
            "company_id": company["id"],
            "action": "ensure",
            "sender_id": "project_manager",
            "agent_id": "coding_engineer",
        },
        {},
    )
    assert no_context_dm_ensure["status"] == "error"
    assert no_context_dm_ensure["error"]["code"] == "ACTOR_REQUIRED"

    spoofed_dm_create = dms_block.run(
        {
            "company_id": company["id"],
            "action": "create",
            "sender_id": "project_manager",
            "actor_id": "project_manager",
            "agent_id": "coding_engineer",
        },
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_dm_create["status"] == "error"
    assert spoofed_dm_create["error"]["code"] == "FORBIDDEN"

    spoofed_dm_ensure = dms_block.run(
        {
            "company_id": company["id"],
            "action": "ensure",
            "sender_id": "project_manager",
            "actor_id": "project_manager",
            "agent_id": "coding_engineer",
        },
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_dm_ensure["status"] == "error"
    assert spoofed_dm_ensure["error"]["code"] == "FORBIDDEN"
    assert dms_block.run({"company_id": company["id"], "action": "list"}, {})["data"]["total"] == 0

    no_context_settings = rich_block.run(
        {"company_id": company["id"], "action": "set", "enabled": True, "actor_id": "project_manager"},
        {},
    )
    assert no_context_settings["status"] == "error"
    assert no_context_settings["error"]["code"] == "ACTOR_REQUIRED"

    spoofed_settings = rich_block.run(
        {"company_id": company["id"], "action": "set", "enabled": True, "actor_id": "project_manager"},
        {"actor_id": "coding_engineer"},
    )
    assert spoofed_settings["status"] == "error"
    assert spoofed_settings["error"]["code"] == "FORBIDDEN"


def test_channels_with_five_members_require_pm_unless_creator_supplies_one(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    store, runtime_store, company = _create_workspace()

    from domain.subagent_team.service import SubagentTeamService

    service = SubagentTeamService(company_store=store, runtime_store=runtime_store)
    no_pm = service.upsert_channel(
        company["id"],
        {
            "id": "large-no-pm",
            "name": "Large No PM",
            "members": ["coding_engineer", "reviewer", "research_specialist", "scribe", "scheduler"],
        },
    )
    assert no_pm["denied"] is True
    assert no_pm["code"] == "PM_REQUIRED"

    with_pm = service.upsert_channel(
        company["id"],
        {
            "id": "large-with-pm",
            "name": "Large With PM",
            "members": ["project_manager", "coding_engineer", "reviewer", "research_specialist", "scribe"],
        },
    )
    assert with_pm["id"] == "large-with-pm"
    assert with_pm["metadata"]["subagent_team"]["pm_required"] is True

    store.update_settings(company["id"], {"subagent_team": {"rich_enabled": True}})
    creator = service.creator_request(
        company["id"],
        {"action": "create_team", "team_size": 5, "channel_name": "Creator Large"},
    )
    assert creator["allowed"] is True
    assert creator["channel"]["metadata"]["subagent_team"]["pm_agent_id"]


def test_subagent_team_company_write_bypass_is_blocked_on_company_routes(tmp_path, monkeypatch):
    _configure_temp_runtime(tmp_path, monkeypatch)
    _, _, company = _create_workspace()

    from blocks.company import create as company_create
    from blocks.company import channels as company_channels
    from blocks.company import dispatch as company_dispatch
    from blocks.company import messages as company_messages
    from blocks.company import settings as company_settings
    from blocks.company import tasks as company_tasks
    from blocks.company import bootstrap as company_bootstrap
    from blocks.company import status as company_status

    channel_write = company_channels.run(
        {"company_id": company["id"], "action": "create", "channel": {"id": "bypass", "members": []}},
        {},
    )
    assert channel_write["status"] == "error"
    assert channel_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"

    message_write = company_messages.run(
        {"company_id": company["id"], "action": "create", "channel_id": "ops-company", "content": "@coding_engineer bypass"},
        {},
    )
    assert message_write["status"] == "error"
    assert message_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"

    task_write = company_tasks.run(
        {"company_id": company["id"], "action": "create", "title": "bypass", "target_agent_ids": ["coding_engineer"]},
        {},
    )
    assert task_write["status"] == "error"
    assert task_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"
    dispatch_write = company_dispatch.run({"company_id": company["id"], "task_id": "task_bypass"}, {})
    assert dispatch_write["status"] == "error"
    assert dispatch_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"

    settings_write = company_settings.run(
        {"company_id": company["id"], "action": "update", "settings": {"subagent_team": {"rich_enabled": True}}},
        {},
    )
    assert settings_write["status"] == "error"
    assert settings_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"

    ui_bootstrap = company_status.run({"conversation_id": "chat-main-guard", "bootstrap": True}, {})
    assert ui_bootstrap["status"] == "ok"
    ui_company_id = ui_bootstrap["data"]["company_id"]
    assert ui_bootstrap["data"]["company"]["metadata"]["source"] == "chat"
    ui_message_write = company_messages.run(
        {"company_id": ui_company_id, "action": "create", "channel_id": "ops-company", "content": "@coding_engineer bypass"},
        {},
    )
    assert ui_message_write["status"] == "ok"
    ui_task_write = company_tasks.run(
        {"company_id": ui_company_id, "action": "create", "title": "normal task", "target_agent_ids": ["coding_engineer"]},
        {},
    )
    assert ui_task_write["status"] == "ok"

    from domain.company.contract_facade import _conversation_company_id

    marker_company_id = _conversation_company_id("subagent-chat-guard")
    marker_created = company_create.run(
        {
            "id": marker_company_id,
            "name": "Subagent Marker",
            "metadata": {
                "conversation_id": "subagent-chat-guard",
                "surface": "subagent_team_workspace",
                "subagent_team": True,
            },
        },
        {},
    )
    assert marker_created["status"] == "ok", marker_created

    marker_bootstrap = company_bootstrap.run(
        {
            "conversation_id": "subagent-chat-guard",
            "scope": "conversation",
            "metadata": {
                "conversation_id": "subagent-chat-guard",
                "surface": "subagent_team_workspace",
                "subagent_team": True,
            },
        },
        {},
    )
    assert marker_bootstrap["status"] == "ok", marker_bootstrap
    marker_company_id = marker_bootstrap["data"]["company"]["id"]
    marker_message_write = company_messages.run(
        {"company_id": marker_company_id, "action": "create", "channel_id": "ops-company", "content": "@coding_engineer bypass"},
        {},
    )
    assert marker_message_write["status"] == "error"
    assert marker_message_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"
    marker_task_write = company_tasks.run(
        {"company_id": marker_company_id, "action": "create", "title": "blocked task", "target_agent_ids": ["coding_engineer"]},
        {},
    )
    assert marker_task_write["status"] == "error"
    assert marker_task_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"
    marker_dispatch_write = company_dispatch.run({"company_id": marker_company_id, "task_id": "task_bypass"}, {})
    assert marker_dispatch_write["status"] == "error"
    assert marker_dispatch_write["error"]["code"] == "SUBAGENT_TEAM_POLICY_REQUIRED"

    normal = company_create.run({"id": "plain-company", "name": "Plain Company"}, {})
    assert normal["status"] == "ok"
    normal_message = company_messages.run(
        {"company_id": "plain-company", "action": "create", "channel_id": "ops-company", "content": "hello"},
        {},
    )
    assert normal_message["status"] == "ok"


def test_subagent_team_requires_captured_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/subagent-team/channels",
        "tobkiri.subagent-team.v1",
        "defaultspack.subagent-team.list-channels",
    )


def test_subagent_team_provider_safe_function_manifests_are_registered():
    from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID

    expected = {
        "subagent_request": ("subagent.request", "blocks.subagent_team.creator"),
        "subagent_status": ("subagent.status", "blocks.subagent_team.creator"),
        "subagent_create": ("subagent.create", "blocks.subagent_team.creator"),
        "subagent_dm_send": ("subagent.dm.send", "blocks.subagent_team.creator"),
        "subagent_channel_join": ("subagent.channel.join", "blocks.subagent_team.creator"),
        "subagent_goal_propose": ("subagent.goal.propose", "blocks.subagent_team.creator"),
        "subagent_goal_approve": ("subagent.goal.approve", "blocks.subagent_team.creator"),
        "subagent_task_complete": ("subagent.task.complete", "blocks.subagent_team.creator"),
        "channel_check": ("channel.check", "blocks.subagent_team.channel_check"),
    }

    for function_id, (display_alias, block_module) in expected.items():
        assert function_id in FUNCTION_SPECS_BY_ID
        assert "." not in function_id
        manifest_path = DEFAULTSPACK_ROOT / "functions" / function_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["function_id"] == function_id
        assert display_alias in manifest["vocab_aliases"]
        assert manifest["extensions"]["defaultspack"]["block_module"] == block_module
        assert manifest["extensions"]["defaultspack"]["default_args"]["action"] == function_id


def test_subagent_team_mutation_requires_captured_operation(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/subagent-team/rich",
        "tobkiri.subagent-team.v1",
        "defaultspack.subagent-team.update-rich",
    )
