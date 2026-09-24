from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _settings_owner(tmp_path: Path, *, enabled: bool | None):
    from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore

    owner = FrontendSettingsStore(tmp_path / "settings" / "frontend_settings.json")
    if enabled is not None:
        owner.update(
            lambda current: {
                **current,
                "automation": {
                    **(
                        current.get("automation")
                        if isinstance(current.get("automation"), dict)
                        else {}
                    ),
                    "subagent_teams_enabled": enabled,
                },
            }
        )
    return owner


def test_disabled_preference_blocks_delegate_even_if_request_claims_enabled(tmp_path):
    from domain.input.actions.agent_delegate import handle
    from domain.input.envelope import RumiInputEnvelope

    result = handle(
        RumiInputEnvelope(
            role="user",
            input="delegate this task",
            chat={},
            source={},
            delivery={"action_id": "agent.delegate"},
            params={
                "task": "delegate this task",
                "automation": {"subagent_teams_enabled": True},
                "settings": {"automation": {"subagent_teams_enabled": True}},
            },
        ),
        {},
        settings_owner=_settings_owner(tmp_path, enabled=False),
    )

    assert result["status"] == "error"
    assert result["code"] == "SUBAGENTS_DISABLED"
    assert "無効" in result["assistant_text"]


def test_context_settings_owner_blocks_delegate_when_an_explicit_owner_is_omitted(
    tmp_path,
):
    from domain.input.actions.agent_delegate import handle
    from domain.input.envelope import RumiInputEnvelope

    result = handle(
        RumiInputEnvelope(
            role="user",
            input="delegate this task",
            chat={},
            source={},
            delivery={"action_id": "agent.delegate"},
            params={"task": "delegate this task"},
        ),
        {"_settings_owner_port": _settings_owner(tmp_path, enabled=False)},
    )

    assert result["status"] == "error"
    assert result["code"] == "SUBAGENTS_DISABLED"


def test_missing_and_enabled_preferences_preserve_delegate_behavior(
    tmp_path,
    monkeypatch,
):
    from domain.input.actions.agent_delegate import handle
    from domain.input.envelope import RumiInputEnvelope
    from domain.subagent_team.availability import subagent_delegation_enabled

    missing = _settings_owner(tmp_path / "missing", enabled=None)
    enabled = _settings_owner(tmp_path / "enabled", enabled=True)
    assert subagent_delegation_enabled(settings_owner=missing) is True
    assert subagent_delegation_enabled(settings_owner=enabled) is True

    monkeypatch.setattr(
        "blocks.agent.execute.run",
        lambda *_args, **_kwargs: {"status": "ok", "data": {"status": "running"}},
    )
    result = handle(
        RumiInputEnvelope(
            role="user",
            input="delegate this task",
            chat={},
            source={},
            delivery={"action_id": "agent.delegate"},
            params={"task": "delegate this task"},
        ),
        {},
        settings_owner=enabled,
    )

    assert result["status"] == "ok"


def test_disabled_preference_blocks_compatibility_and_workspace_entrypoints(tmp_path):
    from blocks.agent.multi_execute import run as run_multi
    from blocks.agent.run_subagent import run as run_subagent
    from blocks.subagent_team.bootstrap import run as bootstrap_team

    owner = _settings_owner(tmp_path, enabled=False)
    compatibility = run_subagent(
        {"role_id": "tool_selector", "payload": {"candidate_tools": []}},
        {},
        settings_owner=owner,
    )
    multi = run_multi({"task": "delegate this task"}, {}, settings_owner=owner)
    bootstrap = bootstrap_team(
        {"conversation_id": "chat-disabled"},
        {},
        settings_owner=owner,
    )

    for result in (compatibility, multi, bootstrap):
        assert result["status"] == "error"
        assert result["error"]["code"] == "SUBAGENTS_DISABLED"


def test_disabled_preference_blocks_team_send_but_keeps_creator_status_available(
    tmp_path,
    monkeypatch,
):
    from domain.subagent_team.service import SubagentTeamService

    owner = _settings_owner(tmp_path, enabled=False)
    service = SubagentTeamService(
        company_store=SimpleNamespace(get_company=lambda _company_id: {"id": "team"}),
        runtime_store=SimpleNamespace(),
        settings_owner=owner,
    )
    existing = service.ensure_team({"company_id": "team", "bootstrap": True})
    send = service.send_message("team", {"content": "@worker new task"})

    monkeypatch.setattr(
        "domain.subagent_team.service.CreatorService.request",
        lambda *_args, **_kwargs: {"status": "available"},
    )
    status = service.creator_request("team", {"action": "creator_status"})

    assert existing == {
        "company_id": "team",
        "company": {"id": "team"},
        "bootstrapped": True,
    }
    assert send == {
        "denied": True,
        "allowed": False,
        "code": "SUBAGENTS_DISABLED",
        "message": "サブエージェントは設定で無効になっています。",
    }
    assert status == {"status": "available"}


def test_disabled_preference_blocks_tool_and_utility_subagent_entrypoints(tmp_path):
    from domain.agent.subagent_orchestrator import run_subagent
    from domain.tool.executor import ToolExecutor

    owner = _settings_owner(tmp_path, enabled=False)

    class UnexpectedSubagent:
        def run(self, _arguments, _context):
            raise AssertionError("disabled preference must not start a subagent")

    tool = ToolExecutor(subagent_factory=UnexpectedSubagent)
    public_result = tool.execute(
        "subagent",
        {"task": "do not delegate", "automation": {"subagent_teams_enabled": True}},
        {"_settings_owner_port": owner},
    )
    function_result = tool._execute_local(
        "subagent",
        {"task": "do not delegate"},
        {"_settings_owner_port": owner},
    )
    utility_result = run_subagent(
        "tool_selector",
        {"candidate_tools": []},
        context={"_settings_owner_port": owner},
    )

    for result in (public_result, function_result):
        assert result["is_error"] is True
        assert result["code"] == "SUBAGENTS_DISABLED"
        assert result["widget"]["code"] == "SUBAGENTS_DISABLED"
    assert utility_result["status"] == "error"
    assert utility_result["code"] == "SUBAGENTS_DISABLED"


def test_disabled_preference_blocks_implicit_team_bootstrap_defaults(tmp_path):
    from domain.subagent_team.service import SubagentTeamService

    owner = _settings_owner(tmp_path, enabled=False)
    store = SimpleNamespace(
        get_company=lambda _company_id: None,
        find_company_by_conversation_id=lambda _conversation_id: None,
    )
    service = SubagentTeamService(
        company_store=store,
        runtime_store=SimpleNamespace(),
        settings_owner=owner,
    )

    conversation = service.ensure_team({"conversation_id": "chat-disabled"})
    default = service.ensure_team({})

    for result in (conversation, default):
        assert result["denied"] is True
        assert result["code"] == "SUBAGENTS_DISABLED"
        assert result["bootstrapped"] is False


def test_disabled_preference_preserves_explicit_non_bootstrap_status(tmp_path, monkeypatch):
    from domain.subagent_team.service import CompanyService, SubagentTeamService

    owner = _settings_owner(tmp_path, enabled=False)
    store = SimpleNamespace(
        get_company=lambda _company_id: None,
        find_company_by_conversation_id=lambda _conversation_id: None,
    )
    monkeypatch.setattr(
        CompanyService,
        "status_for_conversation",
        lambda _self, conversation_id, *, bootstrap: {
            "conversation_id": conversation_id,
            "bootstrapped": bootstrap,
        },
    )
    service = SubagentTeamService(
        company_store=store,
        runtime_store=SimpleNamespace(),
        settings_owner=owner,
    )

    result = service.ensure_team(
        {"conversation_id": "chat-existing-status", "bootstrap": False},
    )

    assert result == {
        "conversation_id": "chat-existing-status",
        "bootstrapped": False,
    }


def test_disabled_preference_blocks_dispatch_without_mutating_existing_task(tmp_path):
    from domain.company.run_dispatcher import CompanyRunDispatcher

    owner = _settings_owner(tmp_path, enabled=False)
    task = {
        "task_id": "task-existing",
        "status": "queued",
        "target_agent_ids": ["worker"],
    }
    dispatcher = CompanyRunDispatcher(
        company_store=SimpleNamespace(get_company=lambda _company_id: {"id": "team"}),
        runtime_store=SimpleNamespace(get_task=lambda _task_id, *, company_id: task),
    )

    result = dispatcher.dispatch_task(
        "team",
        "task-existing",
        context={"_settings_owner_port": owner},
    )

    assert result == {
        "task": task,
        "dispatch": {
            "id": "",
            "status": "disabled",
            "code": "SUBAGENTS_DISABLED",
            "message": "サブエージェントは設定で無効になっています。",
        },
        "results": [],
        "run_links": [],
    }
    assert task["status"] == "queued"


def test_disabled_preference_blocks_legacy_message_and_remote_task_entrypoints(tmp_path):
    from blocks.agent.multi_message import run as run_multi_message
    from blocks.remote.task_create import run as run_remote_task_create

    owner = _settings_owner(tmp_path, enabled=False)
    context = {"_settings_owner_port": owner}
    multi_message = run_multi_message({"session_id": "session", "message": "delegate"}, context)
    remote_task = run_remote_task_create({"input": "delegate"}, context)

    for result in (multi_message, remote_task):
        assert result["status"] == "error"
        assert result["error"]["code"] == "SUBAGENTS_DISABLED"
