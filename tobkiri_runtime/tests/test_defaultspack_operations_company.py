from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _reset_defaultspack_singletons():
    from domain.agent.org_manager import OrgManager
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore
    from domain.company.store import CompanyStore
    from domain.tool.registry import ToolRegistry

    scheduler = Scheduler._instance
    if scheduler is not None:
        for schedule_id in list(getattr(scheduler, "_timers", {}).keys()):
            scheduler._cancel_timer(schedule_id)
    Scheduler._instance = None
    OrgManager._instance = None
    ChatStore._instance = None
    CompanyRuntimeStore._instance = None
    CompanyStore._instance = None
    ToolRegistry._instance = None


def test_operations_company_profile_coexists_with_default_profile():
    from domain.capability.catalog import CapabilityCatalog

    manifest = CapabilityCatalog(DEFAULTSPACK_ROOT).manifest()
    profile_ids = {profile["profile_id"] for profile in manifest["profiles"]}

    assert "defaultspack.local_agent" in profile_ids
    assert "defaultspack.operations_company" in profile_ids
    assert "defaultspack.mimo_coding_company" in profile_ids
    assert manifest["counts"]["profiles"] >= 2


def test_mimo_coding_company_uses_xiaomi_mimo_models(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        DEFAULT_FAST_MODEL,
        DEFAULT_MAIN_MODEL,
        DEFAULT_VISION_MODEL,
        MimoCodingCompanyRuntime,
        current_model_allowlist,
    )

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    allowlist = current_model_allowlist()
    runtime = MimoCodingCompanyRuntime()

    assert "xiaomi-token-plan-sgp/mimo-v2.5-pro" in allowlist
    assert "xiaomi-token-plan-sgp/mimo-v2-omni" in allowlist
    assert "xiaomi-token-plan-sgp/mimo-v2-flash" in allowlist
    assert "opencode-go/mimo-v2.5" not in allowlist
    assert "opencode-go/mimo-v2.5-pro" not in allowlist
    assert "opencode-go/minimax-m3" not in allowlist
    assert "groq/openai/gpt-oss-20b" not in allowlist
    assert "cerebras/zai-glm-4.7" not in allowlist
    assert DEFAULT_MAIN_MODEL == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert DEFAULT_FAST_MODEL == "xiaomi-token-plan-sgp/mimo-v2-flash"
    assert DEFAULT_VISION_MODEL == "xiaomi-token-plan-sgp/mimo-v2-omni"
    assert runtime._allowed_model("xiaomi-token-plan-sgp/mimo-v2.5-pro") == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert (
        runtime._allowed_model("xiaomi-token-plan-sgp/mimo-v2-omni")
        == "xiaomi-token-plan-sgp/mimo-v2-omni"
    )
    assert runtime._allowed_model("xiaomi-token-plan-sgp/mimo-v2-flash") == "xiaomi-token-plan-sgp/mimo-v2-flash"


def test_mimo_coding_company_defaults_use_launcher_user_data(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        MimoCodingCompanyRuntime,
    )

    monkeypatch.delenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", raising=False)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))

    runtime = MimoCodingCompanyRuntime()

    expected_root = tmp_path / "defaultspack" / "shared"
    assert runtime.state_path == expected_root / "mimo_coding_company" / "state.json"
    assert runtime.schedules_dir == expected_root / "schedules"
    assert runtime._docker_runtime_dir() == expected_root / "mimo_coding_company" / "docker_swarm"


def test_mimo_coding_company_status_supersedes_legacy_provider_conversations_idempotently(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        DEFAULT_MAIN_MODEL,
        LEGACY_PROVIDER_EXPIRED_SIGNAL,
        MimoCodingCompanyRuntime,
    )
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    state_path = tmp_path / "mimo" / "state.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 0,
            "desktops": [],
        }),
    )

    chat_store = ChatStore()
    active = chat_store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    current_xiaomi = chat_store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    legacy_opencode_go = chat_store.create_conversation(
        model="opencode-go/mimo-v2.5",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    wrong_profile = chat_store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.other"},
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "conversation_id": active["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "main_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "last_bootstrapped_at": "2026-06-30T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.status(sync_observability=True)
    second = runtime.status(sync_observability=True)

    assert len(first["harness"]["observability"]["legacy_provider_conversations"]["superseded"]) == 1
    assert second["harness"]["observability"]["legacy_provider_conversations"]["superseded"] == []
    for conversation_id, legacy_model in ((legacy_opencode_go["id"], "opencode-go/mimo-v2.5"),):
        conversation = ChatStore().get_conversation(conversation_id)
        metadata = conversation["metadata"]
        markers = [
            message
            for message in conversation["messages"]
            if message.get("role") == "assistant"
            and isinstance(message.get("metadata"), dict)
            and message["metadata"].get("signal") == LEGACY_PROVIDER_EXPIRED_SIGNAL
        ]
        assert conversation["model"] == DEFAULT_MAIN_MODEL
        assert metadata["superseded"] is True
        assert metadata["superseded_reason"] == LEGACY_PROVIDER_EXPIRED_SIGNAL
        assert metadata["legacy_provider_expired"] is True
        assert metadata["legacy_provider_model"] == legacy_model
        assert metadata["active_conversation_id"] == active["id"]
        assert len(markers) == 1
        assert markers[0]["metadata"]["source"] == "codex"
        assert markers[0]["metadata"]["attributed_to"] == "Codex"
        assert active["id"] in markers[0]["raw_text"]

    assert ChatStore().get_conversation(current_xiaomi["id"])["metadata"].get("superseded") is not True

    active_conversation = ChatStore().get_conversation(active["id"])
    skipped_wrong_profile = ChatStore().get_conversation(wrong_profile["id"])
    assert active_conversation["model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert active_conversation["messages"] == []
    assert skipped_wrong_profile["model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert skipped_wrong_profile["messages"] == []
    _reset_defaultspack_singletons()


def test_operations_company_bootstrap_creates_org_conversation_and_heartbeat(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.operations_company import OperationsCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_OPERATIONS_STATE_PATH", str(tmp_path / "ops" / "state.json"))

    status = OperationsCompanyRuntime().bootstrap(start_nonstop=True, heartbeat_minutes=30, model="stub/default")

    assert status["bootstrapped"] is True
    assert status["org"]["member_count"] == 9
    assert status["conversation_id"]
    conversation = ChatStore().get_conversation(status["conversation_id"])
    assert conversation["conversation_kind"] == "operations_company"
    assert conversation["agent_id"] == "client_manager"
    assert conversation["metadata"]["profile_id"] == "defaultspack.operations_company"

    heartbeat = status["schedules"][0]
    assert heartbeat["task"]["profile_id"] == "defaultspack.operations_company"
    assert heartbeat["task"]["agent_id"] == "operations_monitor"
    assert heartbeat["task"]["conversation_id"] == status["conversation_id"]
    assert "rumi_api" in heartbeat["task"]["tool_policy"]["tool_allowlist"]

    Scheduler().delete_schedule(heartbeat["id"])
    _reset_defaultspack_singletons()


def test_operations_conversation_resolves_pack_system_prompt():
    from blocks.chat.send import _conversation_system_prompt
    from domain.prompt.manager import get_manager

    prompt = _conversation_system_prompt({"system_prompt_id": "operations_company"}, get_manager())

    assert "Rumi Operations Company" in prompt
    assert "Client Manager" in prompt


def test_mimo_coding_company_bootstrap_creates_company_conversation_and_loops(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.task_store import CompanyTaskStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    status = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        docker_worker_count=4,
        docker_personas=["first_time_user", "power_user"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )

    assert status["bootstrapped"] is True
    assert status["company"]["id"] == "mimo-coding-company"
    assert status["conversation_id"]
    assert status["harness"]["qa_targets"] == ["http://127.0.0.1:3000"]
    assert status["harness"]["max_tool_calls"] is None
    assert status["harness"]["schedules_dir"] == str(tmp_path / "user_data" / "shared" / "schedules")
    assert status["company"]["metadata"]["max_tool_calls"] is None
    assert len(status["harness"]["seeded_task_ids"]) == 6
    assert status["harness"]["docker_swarm"]["worker_count"] == 4
    assert status["harness"]["docker_swarm"]["personas"] == ["first_time_user", "power_user"]
    assert len(status["harness"]["docker_swarm"]["workers"]) == 4
    assert status["harness"]["open_task_count"] == 6
    assert len(status["harness"]["stream_task_ids"]) == 6
    assert status["harness"]["autonomy_board"]["next_focus"][0]["id"] == "initial_harness_review"
    assert status["harness"]["qa_swarm_plan"]["workers"][0]["persona_id"] == "first_time_user"
    assert status["harness"]["qa_swarm_plan"]["workers"][0]["qa_target"] == "http://127.0.0.1:3000"
    assert Path(status["harness"]["docker_swarm"]["compose_path"]).is_file()
    assert Path(status["harness"]["docker_swarm"]["template_compose_path"]).is_file()
    assert Path(status["harness"]["docker_swarm"]["status_dir"]).is_dir()
    assert Path(status["harness"]["docker_swarm"]["supervisor_path"]).is_file()
    assert Path(status["harness"]["docker_swarm"]["workers"][0]["assignment_path"]).is_file()
    assert status["harness"]["docker_swarm"]["monitoring"]["reported_workers"] == 0
    assert "--project-name " + status["harness"]["docker_swarm"]["project_name"] in status["harness"]["docker_swarm"]["commands"]["up"]
    assert "docker ps --filter" in status["harness"]["docker_swarm"]["commands"]["docker_ps"]
    assert status["harness"]["docker_swarm"]["workers"][0]["container_name"].startswith(
        status["harness"]["docker_swarm"]["project_name"] + "-"
    )
    queued_tasks = CompanyTaskStore().list("mimo-coding-company", status="queued", limit=50, offset=0)
    assert queued_tasks is not None and queued_tasks[1] == 6
    assignment = json.loads(Path(status["harness"]["docker_swarm"]["workers"][0]["assignment_path"]).read_text(encoding="utf-8"))
    supervisor = json.loads(Path(status["harness"]["docker_swarm"]["supervisor_path"]).read_text(encoding="utf-8"))
    assert assignment["container_name"] == status["harness"]["docker_swarm"]["workers"][0]["container_name"]
    assert assignment["persona_id"] == "first_time_user"
    assert assignment["qa_target"] == "http://127.0.0.1:3000"
    assert supervisor["project_name"] == status["harness"]["docker_swarm"]["project_name"]
    assert supervisor["monitoring"]["reported_workers"] == 0
    assert supervisor["commands"]["docker_ps"] == status["harness"]["docker_swarm"]["commands"]["docker_ps"]

    conversation = ChatStore().get_conversation(status["conversation_id"])
    assert conversation["conversation_kind"] == "mimo_coding_company"
    assert conversation["agent_id"] == "client_manager"
    assert conversation["metadata"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert "mimo-coding-company" in conversation["tags"]
    loop_conversation_ids = status["loop_conversation_ids"]
    assert {"kickoff_review", "heartbeat", "improvement_loop", "qa_loop"} <= set(loop_conversation_ids)
    assert len(set(loop_conversation_ids.values())) == len(loop_conversation_ids)
    parent_after_lanes = ChatStore().get_conversation(status["conversation_id"])
    assert set(loop_conversation_ids.values()) <= set(parent_after_lanes["child_conversation_ids"])

    loop_keys = {schedule["task"]["metadata"]["loop_key"] for schedule in status["schedules"]}
    assert {"kickoff_review", "heartbeat", "improvement_loop", "qa_loop"} <= loop_keys
    assert any(schedule["task"]["agent_id"] == "browser_qa" for schedule in status["schedules"])
    schedules_by_loop = {schedule["task"]["metadata"]["loop_key"]: schedule for schedule in status["schedules"]}
    kickoff_schedule = schedules_by_loop["kickoff_review"]
    heartbeat_schedule = schedules_by_loop["heartbeat"]
    improvement_schedule = schedules_by_loop["improvement_loop"]
    qa_schedule = schedules_by_loop["qa_loop"]
    assert kickoff_schedule["task"]["timeout"] == 900
    assert heartbeat_schedule["task"]["timeout"] == 1800
    assert improvement_schedule["task"]["timeout"] == 30 * 24 * 60 * 60
    assert qa_schedule["task"]["timeout"] == 30 * 24 * 60 * 60
    assert improvement_schedule["task"]["timeout"] > heartbeat_schedule["task"]["timeout"]
    assert qa_schedule["task"]["timeout"] > heartbeat_schedule["task"]["timeout"]
    assert improvement_schedule["task"]["conversation_id"] == loop_conversation_ids["improvement_loop"]
    assert qa_schedule["task"]["conversation_id"] == loop_conversation_ids["qa_loop"]
    assert improvement_schedule["task"]["conversation_id"] != qa_schedule["task"]["conversation_id"]
    for loop_key, schedule in schedules_by_loop.items():
        assert schedule["task"]["conversation_id"] == loop_conversation_ids[loop_key]
        assert schedule["task"]["conversation_id"] != status["conversation_id"]
        assert schedule["task"]["metadata"]["parent_conversation_id"] == status["conversation_id"]
        assert schedule["task"]["metadata"]["schedule_failure_external_issue_policy"] == "blocked_only"
        assert "scheduled_timeout" in schedule["task"]["metadata"]["schedule_suppress_external_issue_on"]
        assert schedule["task"]["metadata"]["provider_health_external_issue_policy"] == "provider_health_only"
        assert schedule["task"]["metadata"]["provider_health_blocker_signal"] == "provider_health_blocker"
        loop_conversation = ChatStore().get_conversation(schedule["task"]["conversation_id"])
        assert loop_conversation["parent_conversation_id"] == status["conversation_id"]
        assert loop_conversation["conversation_kind"] == "mimo_coding_company_loop"
    assert heartbeat_schedule["task"]["tool_policy"]["max_tool_calls"] is None
    assert heartbeat_schedule["task"]["tool_policy"]["schedule_initial_tool_choice"] == "required"
    assert qa_schedule["task"]["tool_policy"]["schedule_initial_tool_choice"] == "required"
    assert qa_schedule["task"]["tool_policy"]["schedule_failure_external_issue_policy"] == "blocked_only"
    assert qa_schedule["task"]["tool_policy"]["provider_health_external_issue_policy"] == "provider_health_only"
    assert qa_schedule["task"]["tool_policy"]["provider_health_blocker_signal"] == "provider_health_blocker"
    assert qa_schedule["task"]["tool_policy"]["schedule_auto_approve_expires_in_seconds"] == 24 * 60 * 60
    assert "conversation_running" in qa_schedule["task"]["tool_policy"]["schedule_suppress_external_issue_on"]
    assert heartbeat_schedule["task"]["tools"] == ["rumi_api"]
    heartbeat_allowlist = set(heartbeat_schedule["task"]["tool_policy"]["tool_allowlist"])
    assert "rumi_api" in heartbeat_allowlist
    assert "todo" not in heartbeat_schedule["task"]["tools"]
    assert "subagent" not in heartbeat_schedule["task"]["tools"]
    assert "todo" not in heartbeat_allowlist
    assert "subagent" not in heartbeat_allowlist
    assert "knowledge_search" not in heartbeat_allowlist
    assert "knowledge_create" not in heartbeat_allowlist
    assert "web_search" not in heartbeat_allowlist
    heartbeat_auto_approve = set(heartbeat_schedule["task"]["tool_policy"]["schedule_auto_approve_tool_allowlist"])
    assert "rumi_api:list_routes" in heartbeat_auto_approve
    assert "GET /api/agent/mimo-company/manifest" in heartbeat_auto_approve
    assert "GET /api/agent/mimo-company/status" in heartbeat_auto_approve
    assert "GET /api/company/mimo-coding-company/channels" in heartbeat_auto_approve
    assert "GET /api/company/mimo-coding-company/messages" in heartbeat_auto_approve
    assert "GET /api/company/mimo-coding-company/status" in heartbeat_auto_approve
    assert "GET /api/company/status" in heartbeat_auto_approve
    assert "GET /api/desktops" in heartbeat_auto_approve
    assert "GET /api/health" in heartbeat_auto_approve
    assert "GET /api/tasks" in heartbeat_auto_approve
    assert "0/4 workers reported status" in heartbeat_schedule["task"]["message"]
    assert "short read-only heartbeat" in heartbeat_schedule["task"]["message"]
    assert "at most one or two rumi_api GET checks against allowlisted /api paths" in heartbeat_schedule["task"]["message"]
    assert "never request bare /status" in heartbeat_schedule["task"]["message"]
    assert "Do not call todo or subagent tools" in heartbeat_schedule["task"]["message"]
    assert "do not enumerate routes" in heartbeat_schedule["task"]["message"]
    assert "do not make fixes or create issues" in heartbeat_schedule["task"]["message"]
    assert "0/4 workers reported status" in qa_schedule["task"]["message"]
    assert "one small, useful, testable change" in improvement_schedule["task"]["message"]
    assert "Keep repo discovery narrow" in improvement_schedule["task"]["message"]
    assert "create a draft PR with coding_github_pr_create" in improvement_schedule["task"]["message"]
    assert "Do not create GitHub issues for scheduler bookkeeping noise" in qa_schedule["task"]["message"]
    assert "provider billing/credits/auth failures" in qa_schedule["task"]["message"]
    assert "CreditsError" in qa_schedule["task"]["message"]
    assert "HTTP 401" in qa_schedule["task"]["message"]
    assert {"desktop_list", "desktop_create", "desktop_frame", "desktop_input"} <= set(qa_schedule["task"]["tools"])
    auto_approve_allowlist = set(qa_schedule["task"]["tool_policy"]["schedule_auto_approve_tool_allowlist"])
    assert "rumi_api" not in auto_approve_allowlist
    assert {
        "rumi_api:list_routes",
        "GET /api/agent/mimo-company/manifest",
        "GET /api/agent/mimo-company/status",
        "GET /api/agent/self-improvement/status",
        "GET /api/agent/multi/status",
        "GET /api/company/mimo-coding-company/channels",
        "GET /api/company/mimo-coding-company/messages",
        "GET /api/company/mimo-coding-company/status",
        "GET /api/company/status",
        "GET /api/desktops",
        "GET /api/health",
        "GET /api/remote/host/status",
        "GET /api/repo/files",
        "GET /api/tasks",
        "todo",
        "subagent",
        "knowledge_search",
        "knowledge_create",
        "web_search",
        "desktop_list",
        "desktop_create",
        "desktop_frame",
        "desktop_input",
        "coding_file_read",
        "coding_file_search",
        "coding_file_list",
        "coding_file_write",
        "coding_file_create",
        "coding_file_patch",
        "coding_file_restore",
        "coding_git_status",
        "coding_git_diff",
        "coding_git_commit",
        "coding_git_push",
        "coding_github_pr_create",
        "coding_terminal_exec",
    } <= auto_approve_allowlist
    assert "managed desktop" in qa_schedule["task"]["message"]
    assert (tmp_path / "user_data" / "shared" / "schedules" / f"{qa_schedule['id']}.json").is_file()
    assert not (tmp_path / "ops_pack" / "user_data" / "shared" / "schedules" / f"{qa_schedule['id']}.json").exists()
    browser_qa_role = next(role for role in status["manifest"]["roles"] if role["agent_id"] == "browser_qa")
    assert {"desktop_list", "desktop_create", "desktop_frame", "desktop_input"} <= set(browser_qa_role["allowed_tools"])
    coding_engineer_role = next(role for role in status["manifest"]["roles"] if role["agent_id"] == "coding_engineer")
    assert {"coding_git_commit", "coding_git_push", "coding_github_pr_create"} <= set(coding_engineer_role["allowed_tools"])
    assert {
        "desktop_list",
        "desktop_create",
        "desktop_frame",
        "desktop_input",
        "coding_git_commit",
        "coding_git_push",
        "coding_github_pr_create",
    } <= set(status["manifest"]["tool_policy"]["allowlist"])
    assert status["harness"]["qa_swarm_plan"]["managed_desktop_fallback"]["tools"] == [
        "desktop_list",
        "desktop_create",
        "desktop_frame",
        "desktop_input",
    ]
    assert status["harness"]["qa_swarm_plan"]["managed_desktop_fallback"]["create_defaults"]["template_id"] == "desktop.browser"
    assert "template_id=desktop.browser" in qa_schedule["task"]["message"]
    assert "status is running" in qa_schedule["task"]["message"]
    assert "exactly matches the managed desktop target URL" in qa_schedule["task"]["message"]
    assert "Ignore destroyed, failed, stale, or wrong-target seats" in qa_schedule["task"]["message"]
    assert "If no current-target running browser desktop is available" in qa_schedule["task"]["message"]
    assert "ERR_CONNECTION_REFUSED" in qa_schedule["task"]["message"]
    assert "trusted local/server context" in qa_schedule["task"]["message"]
    assert "do not add payload owner_id" in qa_schedule["task"]["message"]
    assert "access_policy.owner_id as owner_id" not in qa_schedule["task"]["message"]
    assert "owner_id=mimo-coding-company" not in qa_schedule["task"]["message"]
    assert "action=type_text" in qa_schedule["task"]["message"]
    assert "action=key" in qa_schedule["task"]["message"]
    assert "never send a text-only payload" in qa_schedule["task"]["message"]

    for schedule in status["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_status_includes_runtime_workspace_counts(tmp_path, monkeypatch):
    from domain.company.runtime_store import CompanyRuntimeStore
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    runtime.bootstrap(
        start_nonstop=False,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    runtime_store = CompanyRuntimeStore()
    _before_messages, before_total = runtime_store.list_messages(
        "mimo-coding-company",
        channel_id="ops-company",
        limit=1,
        offset=0,
    )
    runtime_store.add_message(
        "mimo-coding-company",
        channel_id="ops-company",
        sender_id="scheduler",
        content="MiMo Team Workspace runtime sync check",
    )

    status = runtime.status()

    expected_total = before_total + 1
    assert status["company"]["message_count"] == expected_total
    assert status["company"]["runtime_counts"]["messages"] == expected_total
    assert status["company"]["channels"]["ops-company"]["message_count"] == expected_total
    assert status["company"]["channels"]["ops-company"]["last_message_at"]

    _reset_defaultspack_singletons()


def test_mimo_coding_company_status_uses_persisted_bootstrap_state_without_org(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / "mimo" / "state.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(state_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "org_id": "mimo-coding-company-org",
                "conversation_id": "conv_persisted",
                "last_bootstrapped_at": "2026-06-30T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    _reset_defaultspack_singletons()
    status = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack").status()

    assert status["bootstrapped"] is True
    assert status["org"] is None
    assert status["conversation_id"] == "conv_persisted"

    _reset_defaultspack_singletons()


def test_mimo_coding_company_status_block_accepts_explicit_recovery_flag(monkeypatch):
    from ecosystem.rumi_operations_company_pack.blocks.agent.mimo_company import status as status_block

    calls = []

    class FakeRuntime:
        def status(
            self,
            *,
            recover_scheduled_approvals=False,
            sync_observability=False,
            include_desktop_monitoring=False,
        ):
            calls.append(
                {
                    "recover_scheduled_approvals": recover_scheduled_approvals,
                    "sync_observability": sync_observability,
                    "include_desktop_monitoring": include_desktop_monitoring,
                }
            )
            return {"bootstrapped": True}

    monkeypatch.setattr(status_block, "MimoCodingCompanyRuntime", FakeRuntime)

    default_result = status_block.run({}, {})
    explicit_result = status_block.run({"recover_scheduled_approvals": "true"}, {})

    assert default_result["status"] == "ok"
    assert explicit_result["status"] == "ok"
    assert calls == [
        {
            "recover_scheduled_approvals": False,
            "sync_observability": True,
            "include_desktop_monitoring": False,
        },
        {
            "recover_scheduled_approvals": True,
            "sync_observability": True,
            "include_desktop_monitoring": False,
        },
    ]


def test_mimo_coding_company_bootstrap_can_run_without_docker_swarm(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    status = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        docker_worker_count=0,
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )

    docker_swarm = status["harness"]["docker_swarm"]
    assert docker_swarm["enabled"] is False
    assert docker_swarm["worker_count"] == 0
    assert docker_swarm["workers"] == []
    assert docker_swarm["disabled_reason"] == "non_docker_worker_mode"
    assert docker_swarm["monitoring"]["disabled"] is True
    assert docker_swarm["monitoring"]["total_workers"] == 0
    assert docker_swarm["monitoring"]["missing_status_workers"] == []
    assert status["company"]["metadata"]["docker_swarm"]["enabled"] is False
    assert status["harness"]["qa_swarm_plan"]["runtime_mode"] == "managed_desktop"
    assert status["harness"]["qa_swarm_plan"]["docker_disabled_reason"] == "non_docker_worker_mode"
    assert status["harness"]["qa_swarm_plan"]["workers"] == []
    assert status["company"]["metadata"]["qa_swarm_plan"]["runtime_mode"] == "managed_desktop"
    desktop_defaults = status["harness"]["qa_swarm_plan"]["managed_desktop_fallback"]["create_defaults"]
    assert desktop_defaults["starter"] == "browser_url"
    assert desktop_defaults["assigned_agent"] == "browser_qa"
    assert desktop_defaults["resolution"] == {"width": 1280, "height": 800}

    heartbeat_schedule = next(schedule for schedule in status["schedules"] if schedule["task"]["metadata"]["loop_key"] == "heartbeat")
    qa_schedule = next(schedule for schedule in status["schedules"] if schedule["task"]["metadata"]["loop_key"] == "qa_loop")
    assert "workers reported status" not in heartbeat_schedule["task"]["message"]
    assert "workers reported status" not in qa_schedule["task"]["message"]
    assert "First call desktop_list" in qa_schedule["task"]["message"]
    assert "status is running" in qa_schedule["task"]["message"]
    assert "exactly matches the managed desktop target URL" in qa_schedule["task"]["message"]
    assert "Ignore destroyed, failed, stale, or wrong-target seats" in qa_schedule["task"]["message"]
    assert "If no current-target running browser desktop is available" in qa_schedule["task"]["message"]
    assert "ERR_CONNECTION_REFUSED" in qa_schedule["task"]["message"]
    assert "trusted local/server context" in qa_schedule["task"]["message"]
    assert "do not add payload owner_id" in qa_schedule["task"]["message"]
    assert "access_policy.owner_id as owner_id" not in qa_schedule["task"]["message"]
    assert "owner_id=mimo-coding-company" not in qa_schedule["task"]["message"]
    assert "action=type_text" in qa_schedule["task"]["message"]
    assert "action=key" in qa_schedule["task"]["message"]
    assert "never send a text-only payload" in qa_schedule["task"]["message"]
    assert "Do not use rumi_api for desktop frames or inputs" in qa_schedule["task"]["message"]
    assert "/api/desktops/{seat_id}/frame is a GET route, never POST" in qa_schedule["task"]["message"]
    assert {"desktop_list", "desktop_create", "desktop_frame", "desktop_input"} <= set(qa_schedule["task"]["tools"])

    for schedule in status["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_qa_schedule_uses_managed_desktop_reachable_defaultspack_url(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    status = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:8766/chat"],
        docker_worker_count=0,
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )

    qa_schedule = next(schedule for schedule in status["schedules"] if schedule["task"]["metadata"]["loop_key"] == "qa_loop")
    message = qa_schedule["task"]["message"]
    qa_conversation_id = status["loop_conversation_ids"]["qa_loop"]
    managed_chat_target = f"http://127.0.0.1:18766/chat?chat={qa_conversation_id}"

    assert qa_schedule["task"]["conversation_id"] == qa_conversation_id
    assert f"Managed desktop target URLs: {managed_chat_target}" in message
    assert "browser_url=<managed desktop target URL>" in message
    assert "different address-bar URL" in message
    assert "stale/wrong-target" in message
    assert "http://127.0.0.1:8766/chat" not in message
    assert "http://127.0.0.1:18766/chat." not in message

    for schedule in status["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_managed_desktop_chat_url_falls_back_to_parent_conversation(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    message = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")._qa_message(
        {
            "conversation_id": "parent-conversation-123",
            "loop_conversation_ids": {},
            "qa_targets": ["http://127.0.0.1:8766/chat"],
            "docker_swarm": {"enabled": False, "worker_count": 0, "workers": []},
        }
    )

    assert "Managed desktop target URLs: http://127.0.0.1:18766/chat?chat=parent-conversation-123" in message
    assert "http://127.0.0.1:18766/chat." not in message
    _reset_defaultspack_singletons()


def test_mimo_coding_company_desktop_monitor_blocks_bare_chat_target(monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from blocks.sandbox import api as sandbox_api

    def fake_desktops_list(payload, context):
        assert payload == {"_handler": "desktops_list"}
        assert context["source"] == "mimo_observability"
        return {
            "status": "ok",
            "data": {
                "desktops": [
                    {
                        "seat_id": "seat_stale_chat",
                        "status": "running",
                        "template_id": "desktop.browser",
                        "startup": {
                            "starter": "browser_url",
                            "browser_url": "http://127.0.0.1:18766/chat",
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(sandbox_api, "run", fake_desktops_list)

    observation = MimoCodingCompanyRuntime._desktop_monitoring_observation()
    message = MimoCodingCompanyRuntime._desktop_monitoring_message(observation)

    assert observation["status"] == "blocked"
    assert observation["signal"] == "managed_desktop_chat_target_missing"
    assert observation["missing_chat_targets"][0]["seat_id"] == "seat_stale_chat"
    assert observation["missing_chat_targets"][0]["browser_url"] == "http://127.0.0.1:18766/chat"
    assert "without an explicit chat query" in observation["blocker"]
    assert "seat_stale_chat" in message
    assert "http://127.0.0.1:18766/chat" in message


def test_mimo_coding_company_desktop_monitor_ignores_historical_bare_chat_targets(monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from blocks.sandbox import api as sandbox_api

    def fake_desktops_list(payload, context):
        assert payload == {"_handler": "desktops_list"}
        assert context["source"] == "mimo_observability"
        return {
            "status": "ok",
            "data": {
                "desktops": [
                    {
                        "seat_id": "seat_running_qa",
                        "status": "running",
                        "template_id": "desktop.browser",
                        "startup": {
                            "starter": "browser_url",
                            "browser_url": "http://127.0.0.1:18766/chat?chat=qa-loop-123",
                        },
                        "frame": {
                            "frame_seq": 3,
                            "width": 1280,
                            "height": 800,
                            "captured_at": datetime.now(timezone.utc).timestamp(),
                        },
                    },
                    {
                        "seat_id": "seat_old_stopped",
                        "status": "stopped",
                        "template_id": "desktop.browser",
                        "startup": {
                            "starter": "browser_url",
                            "browser_url": "http://127.0.0.1:18766/chat",
                        },
                    },
                    {
                        "seat_id": "seat_old_destroyed",
                        "status": "destroyed",
                        "template_id": "desktop.browser",
                        "metadata": {
                            "startup": {
                                "browser_url": "http://127.0.0.1:18766/chat",
                            }
                        },
                    },
                    {
                        "seat_id": "seat_old_failed",
                        "status": "failed",
                        "template_id": "desktop.browser",
                        "desktop_spec": {
                            "browser_url": "http://127.0.0.1:18766/chat",
                        },
                    },
                ]
            },
        }

    monkeypatch.setattr(sandbox_api, "run", fake_desktops_list)

    observation = MimoCodingCompanyRuntime._desktop_monitoring_observation()

    assert observation["status"] == "ok"
    assert "signal" not in observation
    assert "missing_chat_targets" not in observation
    assert observation["desktop_count"] == 4
    assert [desktop["seat_id"] for desktop in observation["desktops"]] == [
        "seat_running_qa",
        "seat_old_stopped",
        "seat_old_destroyed",
        "seat_old_failed",
    ]


def test_mimo_coding_company_desktop_monitor_reports_missing_running_frame_without_refresh(monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from blocks.sandbox import api as sandbox_api

    calls = []

    def fake_desktop_api(payload, context):
        calls.append((payload, context))
        if payload == {"_handler": "desktops_list"}:
            return {
                "status": "ok",
                "data": {
                    "desktops": [
                        {
                            "seat_id": "seat_running_browser",
                            "name": "MiMo browser QA",
                            "status": "running",
                            "template_id": "desktop.browser",
                            "assigned_agent": "browser_qa",
                            "startup": {
                                "starter": "browser_url",
                                "browser_url": "http://127.0.0.1:18766/chat?chat=qa-loop",
                            },
                            "frame": None,
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected sandbox API call: {payload!r}")

    monkeypatch.setattr(sandbox_api, "run", fake_desktop_api)

    observation = MimoCodingCompanyRuntime._desktop_monitoring_observation()
    message = MimoCodingCompanyRuntime._desktop_monitoring_message(observation)

    assert observation["status"] == "degraded"
    assert observation["signal"] == "desktop_frame_missing"
    assert observation["desktops"][0]["assigned_agent"] == "browser_qa"
    assert "frame" not in observation["desktops"][0]
    assert observation["frame_issues"][0]["reason"] == "missing"
    assert "live snapshot is missing or stale" in message
    assert ("seat_running_browser" in message)
    assert calls == [({"_handler": "desktops_list"}, {"source": "mimo_observability"})]


def test_mimo_coding_company_desktop_monitor_flags_missing_running_frame_when_refresh_fails(monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from blocks.sandbox import api as sandbox_api

    def fake_desktop_api(payload, context):
        if payload == {"_handler": "desktops_list"}:
            return {
                "status": "ok",
                "data": {
                    "desktops": [
                        {
                            "seat_id": "seat_running_browser",
                            "status": "running",
                            "template_id": "desktop.browser",
                            "assigned_agent": "browser_qa",
                            "startup": {
                                "starter": "browser_url",
                                "browser_url": "http://127.0.0.1:18766/chat?chat=qa-loop",
                            },
                        }
                    ]
                },
            }
        if payload == {"_handler": "desktop_frame", "seat_id": "seat_running_browser"}:
            return {"status_code": 403, "error": {"message": "Desktop access denied"}}
        raise AssertionError(f"unexpected sandbox API call: {payload!r}")

    monkeypatch.setattr(sandbox_api, "run", fake_desktop_api)

    observation = MimoCodingCompanyRuntime._desktop_monitoring_observation()
    message = MimoCodingCompanyRuntime._desktop_monitoring_message(observation)

    assert observation["status"] == "degraded"
    assert observation["signal"] == "desktop_frame_missing"
    assert observation["frame_issues"][0]["seat_id"] == "seat_running_browser"
    assert observation["frame_issues"][0]["assigned_agent"] == "browser_qa"
    assert "live snapshot is missing or stale" in observation["blocker"]
    assert "seat_running_browser" in message


def test_mimo_coding_company_rebootstrap_refreshes_existing_schedule_messages(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        docker_personas=["first_time_user"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    qa_schedule_id = next(
        schedule["id"]
        for schedule in first["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "qa_loop"
    )
    stale_heartbeat = Scheduler().create_schedule(
        "interval",
        {
            "message": "Stale heartbeat should not keep firing.",
            "model": "stub/default",
            "conversation_id": first["conversation_id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "scheduler",
            "tools": ["rumi_api", "todo", "subagent"],
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": first["conversation_id"],
                "loop_key": "heartbeat",
            },
        },
        {"value": 30, "unit": "minutes"},
        name="MiMo Coding Company heartbeat",
    )
    paused_current_qa = Scheduler().pause_schedule(qa_schedule_id)
    assert paused_current_qa is not None and paused_current_qa["status"] == "paused"

    second = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=120,
        qa_interval_minutes=90,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3001"],
        docker_personas=["power_user", "impatient_user"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    qa_schedule = next(
        schedule
        for schedule in second["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "qa_loop"
    )
    improvement_schedule = next(
        schedule
        for schedule in second["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "improvement_loop"
    )

    assert qa_schedule["id"] == qa_schedule_id
    assert qa_schedule["status"] == "active"
    assert "http://127.0.0.1:3001" in qa_schedule["task"]["message"]
    assert "Power user" in qa_schedule["task"]["message"]
    assert "desktop_create" in qa_schedule["task"]["tools"]
    assert qa_schedule["config"] == {"value": 90, "unit": "minutes"}
    assert improvement_schedule["config"] == {"value": 120, "unit": "minutes"}
    assert second["harness"]["qa_swarm_plan"]["workers"][0]["persona_id"] == "power_user"
    assert second["harness"]["qa_swarm_plan"]["workers"][0]["qa_target"] == "http://127.0.0.1:3001"
    assert second["harness"]["docker_swarm"]["project_name"] == first["harness"]["docker_swarm"]["project_name"]
    assert second["harness"]["docker_swarm"]["workers"][0]["container_name"] == first["harness"]["docker_swarm"]["workers"][0]["container_name"]
    assignment = json.loads(Path(second["harness"]["docker_swarm"]["workers"][0]["assignment_path"]).read_text(encoding="utf-8"))
    compose_text = Path(second["harness"]["docker_swarm"]["compose_path"]).read_text(encoding="utf-8")
    assert assignment["persona_id"] == "power_user"
    assert assignment["qa_target"] == "http://127.0.0.1:3001"
    assert "http://127.0.0.1:3001" in compose_text
    assert "rumi.project_name" in compose_text
    assert Scheduler().get_schedule(stale_heartbeat["id"])["status"] == "paused"

    for schedule in second["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    Scheduler().delete_schedule(stale_heartbeat["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_rebootstrap_pauses_state_external_duplicate_loop(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        SCHEDULE_LOOP_KEYS,
        MimoCodingCompanyRuntime,
    )
    from domain.agent.schedule_store import append_history, load_history
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    first_by_loop = {
        schedule["task"]["metadata"]["loop_key"]: schedule
        for schedule in first["schedules"]
    }
    duplicate_qa = Scheduler().create_schedule(
        "interval",
        {
            "message": "Stale duplicate QA loop should not keep firing.",
            "model": "opencode-go/mimo-v2.5",
            "conversation_id": first["conversation_id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tools": ["rumi_api", "todo", "browser_use"],
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": first["conversation_id"],
                "loop_key": "qa_loop",
            },
        },
        {"value": 5, "unit": "minutes"},
        name="MiMo Coding Company qa loop stale duplicate",
    )
    append_history(
        duplicate_qa["id"],
        {
            "schedule_id": duplicate_qa["id"],
            "execution_id": "exec-stale-duplicate-qa",
            "trigger": "scheduled",
            "status": "completed",
            "started_at": "2026-06-28T23:50:00Z",
            "completed_at": "2026-06-28T23:50:08Z",
            "result": "stale duplicate history must be preserved",
        },
    )

    second = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3001"],
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    second_by_loop = {
        schedule["task"]["metadata"]["loop_key"]: schedule
        for schedule in second["schedules"]
    }
    active_by_loop: dict[str, list[str]] = {loop_key: [] for loop_key in SCHEDULE_LOOP_KEYS}
    for schedule in Scheduler().list_schedules():
        task = schedule.get("task") if isinstance(schedule.get("task"), dict) else {}
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("profile_id") != "defaultspack.mimo_coding_company":
            continue
        if metadata.get("company_id") != "mimo-coding-company":
            continue
        loop_key = str(metadata.get("loop_key") or "")
        if loop_key in active_by_loop and schedule.get("status") == "active":
            active_by_loop[loop_key].append(schedule["id"])

    assert second_by_loop["qa_loop"]["id"] == first_by_loop["qa_loop"]["id"]
    assert Scheduler().get_schedule(duplicate_qa["id"])["status"] == "paused"
    assert active_by_loop["qa_loop"] == [second_by_loop["qa_loop"]["id"]]
    assert all(len(schedule_ids) <= 1 for schedule_ids in active_by_loop.values())
    entries, total = load_history(duplicate_qa["id"])
    assert total == 1
    assert entries[0]["execution_id"] == "exec-stale-duplicate-qa"

    for schedule in second["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    Scheduler().delete_schedule(duplicate_qa["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_rebootstrap_recovers_running_qa_after_chat_target_refresh(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        MimoCodingCompanyRuntime,
        QA_LOOP_SCHEDULE_TIMEOUT_SECONDS,
    )
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:8766/chat"],
        docker_worker_count=0,
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    qa_schedule = next(
        schedule
        for schedule in first["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "qa_loop"
    )
    qa_schedule_id = qa_schedule["id"]
    qa_conversation_id = first["loop_conversation_ids"]["qa_loop"]
    current_target = f"http://127.0.0.1:18766/chat?chat={qa_conversation_id}"
    old_bare_target = "http://127.0.0.1:18766/chat"
    old_message = qa_schedule["task"]["message"].replace(current_target, old_bare_target)
    assert old_message != qa_schedule["task"]["message"]

    scheduler = Scheduler()
    active_execution_id = "sexec-old-bare-chat-target"
    started_at = "2026-06-30T00:00:00Z"
    persisted = load_schedule(qa_schedule_id)
    persisted["task"]["message"] = old_message
    persisted["running_execution"] = {
        "execution_id": active_execution_id,
        "schedule_id": qa_schedule_id,
        "started_at": started_at,
        "trigger": "scheduled",
        "timeout_seconds": QA_LOOP_SCHEDULE_TIMEOUT_SECONDS,
    }
    persisted["running_started_at"] = started_at
    persisted["updated_at"] = started_at
    save_schedule(persisted)
    with scheduler._lock:
        scheduler._schedules[qa_schedule_id] = persisted

    second = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:8766/chat"],
        docker_worker_count=0,
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    refreshed_qa = next(
        schedule
        for schedule in second["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "qa_loop"
    )

    assert refreshed_qa["id"] == qa_schedule_id
    assert current_target in refreshed_qa["task"]["message"]
    assert "running_execution" not in refreshed_qa

    saved = load_schedule(qa_schedule_id)
    assert "running_execution" not in saved
    entries, total = load_history(qa_schedule_id)
    assert total == 1
    assert entries[0]["execution_id"] == active_execution_id
    assert entries[0]["status"] == "obsolete"
    assert entries[0]["obsolete_reason"] == "execution_input_changed"
    assert entries[0]["recovered_obsolete_running_execution"] is True
    assert entries[0]["error"] is None

    for schedule in second["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_bootstrap_defers_overdue_loop_schedule_arming_until_state_saved(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.schedule_store import save_schedule
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    state_path = tmp_path / "mimo" / "state.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(state_path))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    loop_keys = ["kickoff_review", "heartbeat", "improvement_loop", "qa_loop"]
    schedule_ids = {loop_key: "sched_existing_" + loop_key for loop_key in loop_keys}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"schema_version": 1, "schedule_ids": schedule_ids}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    overdue = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent_ids = {
        "kickoff_review": "project_manager",
        "heartbeat": "scheduler",
        "improvement_loop": "project_manager",
        "qa_loop": "browser_qa",
    }
    for loop_key, schedule_id in schedule_ids.items():
        is_once = loop_key == "kickoff_review"
        save_schedule(
            {
                "id": schedule_id,
                "name": "MiMo Coding Company " + loop_key.replace("_", " "),
                "description": "Persisted overdue MiMo loop schedule.",
                "type": "once" if is_once else "interval",
                "task": {
                    "message": "overdue " + loop_key,
                    "model": "stub/default",
                    "conversation_id": "conv_previous",
                    "timeout": 600,
                    "profile_id": "defaultspack.mimo_coding_company",
                    "agent_id": agent_ids[loop_key],
                    "tools": ["rumi_api"],
                    "metadata": {
                        "profile_id": "defaultspack.mimo_coding_company",
                        "company_id": "mimo-coding-company",
                        "conversation_id": "conv_previous",
                        "loop_key": loop_key,
                    },
                },
                "config": {"run_at": overdue} if is_once else {"value": 30, "unit": "minutes"},
                "status": "active",
                "execution_count": 0,
                "last_executed_at": None,
                "next_execution_at": overdue,
                "created_at": overdue,
                "updated_at": overdue,
            }
        )

    arm_observations: list[dict[str, object]] = []

    def record_arm(self, schedule_id):
        schedule = getattr(self, "_schedules", {}).get(schedule_id, {})
        task = schedule.get("task") if isinstance(schedule.get("task"), dict) else {}
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("profile_id") != "defaultspack.mimo_coding_company":
            return
        if metadata.get("company_id") != "mimo-coding-company":
            return
        state_payload = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        arm_observations.append(
            {
                "schedule_id": schedule_id,
                "loop_key": metadata.get("loop_key"),
                "last_bootstrapped_at": bool(state_payload.get("last_bootstrapped_at")),
            }
        )

    monkeypatch.setattr(Scheduler, "_arm_timer", record_arm)

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    status = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=120,
        qa_interval_minutes=90,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3001"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )

    assert status["bootstrapped"] is True
    assert arm_observations
    assert {item["loop_key"] for item in arm_observations} >= set(loop_keys)
    assert all(item["last_bootstrapped_at"] for item in arm_observations)

    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    bootstrapped_at = datetime.fromisoformat(saved_state["last_bootstrapped_at"].replace("Z", "+00:00"))
    schedules_by_loop = {schedule["task"]["metadata"]["loop_key"]: schedule for schedule in status["schedules"]}
    assert schedules_by_loop["kickoff_review"]["task"]["timeout"] == 900
    assert schedules_by_loop["heartbeat"]["task"]["timeout"] == 1800
    assert schedules_by_loop["improvement_loop"]["task"]["timeout"] == 30 * 24 * 60 * 60
    assert schedules_by_loop["qa_loop"]["task"]["timeout"] == 30 * 24 * 60 * 60
    qa_schedule = schedules_by_loop["qa_loop"]
    qa_next = datetime.fromisoformat(qa_schedule["next_execution_at"].replace("Z", "+00:00"))
    assert qa_schedule["status"] == "active"
    assert qa_next > bootstrapped_at

    for schedule in status["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_bootstrap_recovers_orphaned_running_loop_execution(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        MimoCodingCompanyRuntime,
        QA_LOOP_SCHEDULE_TIMEOUT_SECONDS,
    )
    from domain.agent.schedule_store import load_history, load_schedule, save_schedule
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:18766/chat"],
        docker_worker_count=0,
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    qa_schedule = next(
        schedule
        for schedule in first["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "qa_loop"
    )
    qa_schedule_id = qa_schedule["id"]
    execution_id = "sexec-orphaned-qa-loop"
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    persisted = load_schedule(qa_schedule_id)
    persisted["running_execution"] = {
        "execution_id": execution_id,
        "schedule_id": qa_schedule_id,
        "started_at": started_at,
        "trigger": "scheduled",
        "timeout_seconds": QA_LOOP_SCHEDULE_TIMEOUT_SECONDS,
    }
    persisted["running_started_at"] = started_at
    save_schedule(persisted)
    scheduler = Scheduler()
    with scheduler._lock:
        scheduler._schedules[qa_schedule_id] = persisted

    second = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:18766/chat"],
        docker_worker_count=0,
        docker_enabled=False,
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    refreshed_qa = next(
        schedule
        for schedule in second["schedules"]
        if schedule["task"]["metadata"]["loop_key"] == "qa_loop"
    )

    assert "running_execution" not in refreshed_qa
    saved = load_schedule(qa_schedule_id)
    assert "running_execution" not in saved
    entries, _total = load_history(qa_schedule_id, limit=10)
    recovered = next(entry for entry in entries if entry["execution_id"] == execution_id)
    assert recovered["status"] == "obsolete"
    assert recovered["obsolete_reason"] == "manager_bootstrap_restarted"
    assert recovered["recovered_bootstrap_orphaned_running_execution"] is True
    assert recovered["error"] is None

    for schedule in second["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_rebootstrap_replenishes_completed_stream_task(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.company.task_store import CompanyTaskStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    stream_task_id = first["harness"]["stream_task_ids"]["provider_search_coverage"]
    store = CompanyTaskStore()
    updated = store.update(
        "mimo-coding-company",
        stream_task_id,
        {"status": "completed"},
    )
    assert updated is not None and updated["status"] == "completed"

    second = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    replacement_task_id = second["harness"]["stream_task_ids"]["provider_search_coverage"]
    queued_tasks = store.list("mimo-coding-company", status="queued", limit=50, offset=0)

    assert replacement_task_id != stream_task_id
    assert second["harness"]["open_task_count"] == 6
    assert queued_tasks is not None and queued_tasks[1] == 6

    for schedule in second["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_status_aggregates_worker_runtime_status(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    first = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    worker = first["harness"]["docker_swarm"]["workers"][0]
    status_path = Path(worker["status_path"])
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "worker_id": worker["worker_id"],
                "persona_id": worker["persona_id"],
                "started_at": "2026-05-27T00:00:00Z",
                "assignment": {
                    "worker_id": worker["worker_id"],
                    "persona_id": worker["persona_id"],
                },
                "browser_launch": {"attempted": True, "start_url": "http://127.0.0.1:3000"},
                "display": ":99",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    second = runtime.status()
    monitoring = second["harness"]["docker_swarm"]["monitoring"]
    supervisor = json.loads(Path(second["harness"]["docker_swarm"]["supervisor_path"]).read_text(encoding="utf-8"))

    assert monitoring["total_workers"] == len(second["harness"]["docker_swarm"]["workers"])
    assert monitoring["reported_workers"] == 1
    assert monitoring["browser_launch_attempted_workers"] == 1
    assert monitoring["workers"][0]["assignment_match"] is True
    assert second["company"]["metadata"]["docker_swarm"]["monitoring"]["reported_workers"] == 1
    assert supervisor["monitoring"]["reported_workers"] == 1
    assert supervisor["workers"][0]["container_name"] == second["harness"]["docker_swarm"]["workers"][0]["container_name"]
    assert supervisor["commands"]["supervisor"].startswith("cat ")

    refreshed = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    heartbeat_schedule = next(schedule for schedule in refreshed["schedules"] if schedule["task"]["metadata"]["loop_key"] == "heartbeat")
    qa_schedule = next(schedule for schedule in refreshed["schedules"] if schedule["task"]["metadata"]["loop_key"] == "qa_loop")
    assert "1/3 workers reported status" in heartbeat_schedule["task"]["message"]
    assert "1/3 attempted browser launch" in qa_schedule["task"]["message"]

    for schedule in refreshed["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_status_does_not_recover_schedule_approvals_by_default(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    recovery_calls = []

    def record_recovery(self, scheduler, schedule_id):
        recovery_calls.append(schedule_id)
        raise AssertionError("status should not synchronously recover scheduled approvals")

    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_recover_scheduled_approval_for_schedule",
        record_recovery,
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    bootstrapped = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    observed = runtime.status(include_desktop_monitoring=True)

    assert bootstrapped["schedules"]
    assert observed["schedules"]
    assert recovery_calls == []

    runtime.status(sync_observability=True, recover_scheduled_approvals=True)
    assert recovery_calls

    for schedule in observed["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_status_syncs_observability_to_team_workspace(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.schedule_store import append_history
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "empty",
            "desktop_count": 0,
            "desktops": [],
            "signal": "desktops_empty",
        }),
    )
    status = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        qa_targets=["http://127.0.0.1:3000"],
        seed_knowledge=False,
        run_initial_review_now=False,
    )
    heartbeat_schedule = next(schedule for schedule in status["schedules"] if schedule["task"]["metadata"]["loop_key"] == "heartbeat")
    append_history(
        heartbeat_schedule["id"],
        {
            "schedule_id": heartbeat_schedule["id"],
            "execution_id": "exec_subagent_timeout",
            "trigger": "heartbeat",
            "status": "completed",
            "started_at": "2026-06-27T00:00:00Z",
            "completed_at": "2026-06-27T00:00:05Z",
            "result": "subagent delegation timed out; rumi_api returned Handler execution failed",
        },
    )
    append_history(
        heartbeat_schedule["id"],
        {
            "schedule_id": heartbeat_schedule["id"],
            "execution_id": "exec_text_tool_call",
            "trigger": "heartbeat",
            "status": "completed",
            "started_at": "2026-06-27T00:01:00Z",
            "completed_at": "2026-06-27T00:01:05Z",
            "result": "<tool_call>\n<function=todo>\n<parameter=action>list</parameter>\n</function>\n</tool_call>",
        },
    )

    chat_store = ChatStore()
    child = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        parent_conversation_id=status["conversation_id"],
        conversation_kind="subagent",
        agent_id="subagent",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    chat_store.update_conversation(child["id"], {"title": "Subagent capability probe"})
    chat_store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Simple test: List 3 things you can do as a subagent."}],
        },
    )
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    chat_store._conversations[child["id"]]["created_at"] = old_timestamp
    chat_store._conversations[child["id"]]["updated_at"] = old_timestamp
    chat_store._save_conversations()
    recent_child = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        parent_conversation_id=status["conversation_id"],
        conversation_kind="subagent",
        agent_id="subagent",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    chat_store.update_conversation(recent_child["id"], {"title": "Recently started subagent probe"})
    chat_store.add_message(
        recent_child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "This subagent was just started and may still be running."}],
        },
    )
    future_timestamp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000)
    chat_store._conversations[recent_child["id"]]["created_at"] = future_timestamp
    chat_store._conversations[recent_child["id"]]["updated_at"] = future_timestamp
    chat_store._save_conversations()

    observed = runtime.status(sync_observability=True, include_desktop_monitoring=True)
    observability = observed["harness"]["observability"]
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=20, offset=0)

    signals = {item["signal"] for item in observability["schedule_history"]["signals"]}
    assert {"subagent_timeout", "text_tool_call_not_executed"} <= signals
    assert observability["subagents"]["checked"] == 2
    assert observability["subagents"]["repaired_count"] == 0
    assert observability["subagents"]["repaired"] == []
    assert observability["subagents"]["unanswered_count"] == 0
    assert observability["subagents"]["unanswered"] == []
    assert observability["subagents"]["failed_count"] == 1
    assert observability["subagents"]["failed"][0]["child_conversation_id"] == child["id"]
    assert observability["desktop_monitoring"]["status"] in {"empty", "ok", "error"}
    company_subagents = observed["company"]["metadata"]["observability"]["subagents"]
    assert company_subagents["repaired_count"] == 0
    assert company_subagents["repaired"] == []
    assert company_subagents["unanswered_count"] == 0
    assert company_subagents["failed_count"] == 1
    assert recent_child["id"] not in {
        str(message["metadata"].get("child_conversation_id") or "")
        for message in messages
        if isinstance(message.get("metadata"), dict)
    }
    message_signals = {
        message["metadata"].get("signal")
        for message in messages
        if isinstance(message.get("metadata"), dict)
    }
    assert "text_tool_call_not_executed" in message_signals
    assert "subagent_failed" in message_signals
    assert total == 4
    assert {message["metadata"]["sync_source"] for message in messages} == {
        "mimo_schedule_history",
        "mimo_desktop_monitor",
        "mimo_subagent_monitor",
    }

    runtime.status(sync_observability=True, include_desktop_monitoring=True)
    _messages_again, total_again = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=20, offset=0)
    assert total_again == total

    for schedule in observed["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_company_status_reports_scheduler_subagent_blocker_signals(tmp_path, monkeypatch):
    from domain.company.runtime_store import CompanyRuntimeStore
    from domain.company.service import CompanyService
    from domain.company.store import CompanyStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))

    store = CompanyStore()
    store.ensure_company(
        company_id="mimo-coding-company",
        name="MiMo Coding Company",
        description="MiMo harness workspace",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
        conversation_group_id="company:mimo-coding-company",
    )
    runtime_store = CompanyRuntimeStore()
    runtime_store.add_message(
        "mimo-coding-company",
        channel_id="ops-company",
        sender_id="scheduler",
        content="**MiMo scheduler provider-health blocker**",
        metadata={
            "sync_source": "mimo_schedule_history",
            "sync_key": "schedule:exec-provider-blocked",
            "schedule_id": "schedule-heartbeat",
            "execution_id": "exec-provider-blocked",
            "signal": "provider_health_blocker",
            "external_blocker": True,
            "provider_health": {
                "configured_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "reason": "credits_error",
            },
        },
    )
    runtime_store.add_message(
        "mimo-coding-company",
        channel_id="ops-company",
        sender_id="subagent-monitor",
        content="**MiMo subagent child conversation failed before a reply**",
        metadata={
            "sync_source": "mimo_subagent_monitor",
            "sync_key": "subagent:child-1",
            "child_conversation_id": "child-1",
            "signal": "subagent_failed",
        },
    )
    runtime_store.add_message(
        "mimo-coding-company",
        channel_id="ops-company",
        sender_id="subagent-monitor",
        content="**MiMo subagent child conversation recovered**",
        metadata={
            "sync_source": "mimo_subagent_monitor",
            "sync_key": "subagent:child-1:repaired",
            "child_conversation_id": "child-1",
            "signal": "subagent_repaired",
        },
    )

    status = CompanyService().status("mimo-coding-company")
    blocker_signals = status["reporting"]["blocker_signals"]
    company_blocker_signals = status["company"]["runtime_blocker_signals"]

    assert status["runtime"]["messages"] == 3
    assert blocker_signals["blocker_count"] == 2
    assert [item["signal"] for item in blocker_signals["signals"]] == [
        "subagent_failed",
        "provider_health_blocker",
    ]
    assert blocker_signals["latest_signal"]["child_conversation_id"] == "child-1"
    assert company_blocker_signals == blocker_signals
    assert blocker_signals["signals"][1]["provider_health"]["reason"] == "credits_error"
    assert all(item["signal"] != "subagent_repaired" for item in blocker_signals["signals"])

    _reset_defaultspack_singletons()


def test_mimo_company_workspace_channels_sync_schedule_history(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        DEFAULT_FAST_MODEL,
        DEFAULT_MAIN_MODEL,
        DEFAULT_VISION_MODEL,
        MimoCodingCompanyRuntime,
    )
    from blocks.company import channels, messages
    from domain.agent.schedule_store import append_history
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company import mimo_sync
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setattr(mimo_sync, "_last_sync_at", 0.0)
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
        }),
    )

    parent = ChatStore().create_conversation(
        model=DEFAULT_MAIN_MODEL,
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        agent_id="client_manager",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run MiMo heartbeat.",
            "model": DEFAULT_MAIN_MODEL,
            "conversation_id": parent["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "scheduler",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "heartbeat",
            },
        },
        {"run_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")},
        name="MiMo Coding Company heartbeat",
    )
    state_path = tmp_path / "mimo" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "main_model": DEFAULT_MAIN_MODEL,
                "vision_model": DEFAULT_VISION_MODEL,
                "fast_model": DEFAULT_FAST_MODEL,
                "schedule_ids": {"heartbeat": schedule["id"]},
                "loop_conversation_ids": {"heartbeat": parent["id"]},
            }
        ),
        encoding="utf-8",
    )
    for index in range(6):
        append_history(
            schedule["id"],
            {
                "schedule_id": schedule["id"],
                "execution_id": f"exec_after_workspace_stale_{index}",
                "trigger": "scheduled",
                "status": "completed",
                "started_at": f"2026-06-30T17:{10 + index:02d}:00Z",
                "completed_at": f"2026-06-30T17:{11 + index:02d}:00Z",
                "result": "MiMo schedule continued after the Team Workspace count last refreshed.",
            },
        )

    _existing, total_before = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=5, offset=0)
    listed = channels.run({"company_id": "mimo-coding-company"}, {})
    listed_messages = messages.run({"company_id": "mimo-coding-company", "order": "desc"}, {})

    assert total_before == 0
    assert listed["status"] == "ok"
    ops_channel = next(channel for channel in listed["data"]["channels"] if channel["id"] == "ops-company")
    assert ops_channel["message_count"] == 6
    assert ops_channel["last_message_at"]
    assert listed_messages["data"]["total"] == 6
    synced_execution_ids = {
        message["metadata"]["execution_id"]
        for message in listed_messages["data"]["messages"]
        if isinstance(message.get("metadata"), dict)
    }
    assert synced_execution_ids == {f"exec_after_workspace_stale_{index}" for index in range(6)}
    assert {
        message["metadata"]["sync_source"]
        for message in listed_messages["data"]["messages"]
        if isinstance(message.get("metadata"), dict)
    } == {"mimo_schedule_history"}

    scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_repairs_stale_scheduled_draft(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
            "desktops": [],
        }),
    )

    chat_store = ChatStore()
    parent = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        agent_id="client_manager",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    loop = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        parent_conversation_id=parent["id"],
        conversation_kind="mimo_coding_company_loop",
        agent_id="scheduler",
        group_id="company:mimo-coding-company",
        metadata={
            "company_id": "mimo-coding-company",
            "profile_id": "defaultspack.mimo_coding_company",
            "parent_conversation_id": parent["id"],
            "loop_key": "heartbeat",
        },
    )
    user = chat_store.add_message(
        loop["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Run scheduled MiMo heartbeat."}],
            "metadata": {"source": "scheduler"},
        },
    )
    draft = chat_store.add_message(
        loop["id"],
        {
            "role": "assistant",
            "parent_id": user["id"],
            "content": [],
            "raw_text": "",
            "finish_reason": "streaming",
            "metadata": {
                "draft": True,
                "streaming": True,
                "thinking": {"state": "running"},
            },
        },
    )
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    chat_store._conversations[loop["id"]]["messages"][-1]["created_at"] = old_timestamp
    chat_store._conversations[loop["id"]]["messages"][-1]["updated_at"] = old_timestamp
    chat_store._save_conversations()

    scheduler = Scheduler()
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Scheduled heartbeat.",
            "model": "stub/default",
            "conversation_id": loop["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "scheduler",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": loop["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "heartbeat",
            },
        },
        {"run_at": run_at},
        name="MiMo Coding Company heartbeat",
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    summary = runtime._sync_company_observability(
        {
            "conversation_id": parent["id"],
            "conversation_group_id": "company:mimo-coding-company",
            "loop_conversation_ids": {"heartbeat": loop["id"]},
            "schedule_ids": {"heartbeat": schedule["id"]},
        }
    )
    loop_after = ChatStore().get_conversation(loop["id"])
    repaired = loop_after["messages"][-1]
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=10, offset=0)

    assert summary["scheduled_drafts"]["checked"] == 1
    assert summary["scheduled_drafts"]["stale_count"] == 1
    assert summary["scheduled_drafts"]["repaired"] == [draft["id"]]
    assert repaired["id"] == draft["id"]
    assert repaired["finish_reason"] == "error"
    assert repaired["metadata"]["status"] == "error"
    assert repaired["metadata"]["error_code"] == "SCHEDULED_MIMO_DRAFT_STALE"
    assert "draft" not in repaired["metadata"]
    assert "streaming" not in repaired["metadata"]
    assert total == 1
    assert messages[0]["metadata"]["sync_source"] == "mimo_scheduled_draft_monitor"
    assert messages[0]["metadata"]["signal"] == "scheduled_draft_stale"

    scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_repairs_stale_scheduled_user_message(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
            "desktops": [],
        }),
    )

    chat_store = ChatStore()
    parent = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        agent_id="client_manager",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    loop = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        parent_conversation_id=parent["id"],
        conversation_kind="mimo_coding_company_loop",
        agent_id="scheduler",
        group_id="company:mimo-coding-company",
        metadata={
            "company_id": "mimo-coding-company",
            "profile_id": "defaultspack.mimo_coding_company",
            "parent_conversation_id": parent["id"],
            "loop_key": "qa_loop",
        },
    )

    scheduler = Scheduler()
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Scheduled QA.",
            "model": "stub/default",
            "conversation_id": loop["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": loop["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "qa_loop",
            },
        },
        {"run_at": run_at},
        name="MiMo Coding Company qa loop",
    )

    user = chat_store.add_message(
        loop["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Continue this approved scheduled task."}],
            "metadata": {
                "source": "scheduler_approval_followup",
                "schedule_id": schedule["id"],
                "schedule_execution_id": "sexec-stale-user",
                "loop_key": "qa_loop",
                "company_id": "mimo-coding-company",
                "profile_id": "defaultspack.mimo_coding_company",
            },
        },
    )
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    chat_store._conversations[loop["id"]]["messages"][-1]["created_at"] = old_timestamp
    chat_store._conversations[loop["id"]]["messages"][-1]["updated_at"] = old_timestamp
    chat_store._save_conversations()

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    summary = runtime._sync_company_observability(
        {
            "conversation_id": parent["id"],
            "conversation_group_id": "company:mimo-coding-company",
            "loop_conversation_ids": {"qa_loop": loop["id"]},
            "schedule_ids": {"qa_loop": schedule["id"]},
        }
    )
    loop_after = ChatStore().get_conversation(loop["id"])
    repaired = loop_after["messages"][-1]
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=10, offset=0)

    assert summary["scheduled_user_gaps"]["checked"] == 1
    assert summary["scheduled_user_gaps"]["stale_count"] == 1
    assert summary["scheduled_user_gaps"]["repaired"] == [repaired["id"]]
    assert repaired["role"] == "assistant"
    assert repaired["parent_id"] == user["id"]
    assert repaired["finish_reason"] == "error"
    assert repaired["metadata"]["status"] == "error"
    assert repaired["metadata"]["error_code"] == "SCHEDULED_MIMO_USER_ORPHANED"
    assert loop_after["current_node_id"] == repaired["id"]
    assert total == 1
    assert messages[0]["metadata"]["sync_source"] == "mimo_scheduled_user_gap_monitor"
    assert messages[0]["metadata"]["signal"] == "scheduled_user_orphaned"

    scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_scheduled_user_gaps_reads_history_snapshot_without_chatstore_load(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    storage_path = tmp_path / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))

    parent_id = "mimo-parent-history-only"
    loop_id = "mimo-loop-history-only"
    schedule_id = "sched-history-only"
    user_id = "scheduled-user-history-only"
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(json.dumps({"schema_version": 1, "updated_at": 0, "conversations": {}}), encoding="utf-8")

    def write_history(conversation):
        history_path = storage_path.parent / "conversations" / conversation["id"] / "history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({"schema_version": 1, "updated_at": 1, "conversation": conversation}),
            encoding="utf-8",
        )
        return history_path

    write_history(
        {
            "id": parent_id,
            "title": "MiMo parent",
            "created_at": old_timestamp,
            "updated_at": old_timestamp,
            "model": "stub/default",
            "conversation_kind": "mimo_coding_company",
            "current_node_id": None,
            "child_conversation_ids": [],
            "messages": [],
        }
    )
    loop_history_path = write_history(
        {
            "id": loop_id,
            "title": "MiMo QA loop",
            "created_at": old_timestamp,
            "updated_at": old_timestamp,
            "model": "stub/default",
            "conversation_kind": "mimo_coding_company_loop",
            "parent_conversation_id": parent_id,
            "current_node_id": user_id,
            "child_conversation_ids": [],
            "metadata": {
                "company_id": "mimo-coding-company",
                "profile_id": "defaultspack.mimo_coding_company",
                "parent_conversation_id": parent_id,
                "loop_key": "qa_loop",
            },
            "messages": [
                {
                    "id": user_id,
                    "conversation_id": loop_id,
                    "role": "user",
                    "parent_id": None,
                    "children_ids": [],
                    "content": [{"type": "text", "text": "Continue this approved scheduled task."}],
                    "raw_text": "Continue this approved scheduled task.",
                    "created_at": old_timestamp,
                    "updated_at": old_timestamp,
                    "metadata": {
                        "source": "scheduler_approval_followup",
                        "schedule_id": schedule_id,
                        "schedule_execution_id": "sexec-history-only",
                        "loop_key": "qa_loop",
                        "company_id": "mimo-coding-company",
                        "profile_id": "defaultspack.mimo_coding_company",
                    },
                }
            ],
        }
    )

    chat_loads = []

    def fail_if_chat_loaded(self, requested_conversation_id):
        del self
        chat_loads.append(requested_conversation_id)
        raise AssertionError("scheduled user gap scan should use history.json snapshots")

    class NoRunningSchedules:
        def get_schedule(self, requested_schedule_id):
            assert requested_schedule_id == schedule_id
            return None

    monkeypatch.setattr(ChatStore, "get_conversation", fail_if_chat_loaded)
    summary = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")._scheduled_user_gaps(
        {
            "conversation_id": parent_id,
            "loop_conversation_ids": {"qa_loop": loop_id},
            "schedule_ids": {"qa_loop": schedule_id},
        },
        scheduler=NoRunningSchedules(),
    )
    repaired_loop = json.loads(loop_history_path.read_text(encoding="utf-8"))["conversation"]
    repaired = repaired_loop["messages"][-1]

    assert chat_loads == []
    assert summary["checked"] == 1
    assert summary["repaired"] == [repaired["id"]]
    assert repaired["role"] == "assistant"
    assert repaired["parent_id"] == user_id
    assert repaired["metadata"]["error_code"] == "SCHEDULED_MIMO_USER_ORPHANED"
    assert repaired_loop["current_node_id"] == repaired["id"]
    _reset_defaultspack_singletons()


def test_mimo_coding_company_scheduled_draft_monitor_skips_running_schedule(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.schedule_store import save_schedule
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    chat_store = ChatStore()
    parent = chat_store.create_conversation(model="stub/default", conversation_kind="mimo_coding_company")
    loop = chat_store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="mimo_coding_company_loop",
        metadata={"parent_conversation_id": parent["id"], "loop_key": "qa_loop"},
    )
    user = chat_store.add_message(loop["id"], {"role": "user", "content": [{"type": "text", "text": "Run QA."}]})
    draft = chat_store.add_message(
        loop["id"],
        {
            "role": "assistant",
            "parent_id": user["id"],
            "finish_reason": "streaming",
            "metadata": {"draft": True, "streaming": True},
        },
    )
    scheduler = Scheduler()
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Run QA.",
            "model": "stub/default",
            "conversation_id": loop["id"],
            "metadata": {"profile_id": "defaultspack.mimo_coding_company", "loop_key": "qa_loop"},
        },
        {"run_at": run_at},
        name="MiMo Coding Company qa loop",
    )
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    schedule["running_execution"] = {"execution_id": "sexec_active", "started_at": started_at, "timeout_seconds": 1800}
    save_schedule(schedule)
    scheduler._schedules[schedule["id"]] = schedule

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    result = runtime._scheduled_draft_gaps(
        {
            "conversation_id": parent["id"],
            "loop_conversation_ids": {"qa_loop": loop["id"]},
            "schedule_ids": {"qa_loop": schedule["id"]},
        },
        scheduler,
    )
    loop_after = ChatStore().get_conversation(loop["id"])

    assert result == {"checked": 0, "stale": [], "repaired": []}
    assert loop_after["messages"][-1]["id"] == draft["id"]
    assert loop_after["messages"][-1]["finish_reason"] == "streaming"

    scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_discovers_mimo_schedule_outside_state(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.schedule_store import append_history
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
            "desktops": [],
        }),
    )

    parent = ChatStore().create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        agent_id="client_manager",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    scheduler = Scheduler()
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    tracked_schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Tracked state schedule.",
            "model": "stub/default",
            "conversation_id": parent["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "scheduler",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "heartbeat",
            },
        },
        {"run_at": run_at},
        name="MiMo Coding Company heartbeat",
    )
    dedicated_schedule = scheduler.create_schedule(
        "once",
        {
            "message": "Dedicated manager schedule.",
            "model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
            "conversation_id": parent["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "project_manager",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "dedicated_manager",
            },
        },
        {"run_at": run_at},
        name="MiMo Coding Company dedicated manager",
    )
    append_history(
        dedicated_schedule["id"],
        {
            "schedule_id": dedicated_schedule["id"],
            "execution_id": "exec_dedicated_after_state",
            "trigger": "scheduled",
            "status": "completed",
            "started_at": "2026-06-28T23:50:00Z",
            "completed_at": "2026-06-28T23:50:08Z",
            "result": "Dedicated MiMo manager schedule continued after the state schedule list stopped.",
        },
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    state = {
        "conversation_id": parent["id"],
        "conversation_group_id": "company:mimo-coding-company",
        "schedule_ids": {"heartbeat": tracked_schedule["id"]},
    }
    summary = runtime._sync_company_observability(state)
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=10, offset=0)

    assert dedicated_schedule["id"] not in state["schedule_ids"].values()
    assert summary["status"] == "ok"
    assert summary["schedule_history"]["checked"] == 1
    assert summary["team_workspace"]["synced_messages"] == 1
    assert total == 1
    assert messages[0]["metadata"]["sync_source"] == "mimo_schedule_history"
    assert messages[0]["metadata"]["schedule_id"] == dedicated_schedule["id"]
    assert messages[0]["metadata"]["loop_key"] == "dedicated_manager"
    assert messages[0]["metadata"]["execution_id"] == "exec_dedicated_after_state"

    runtime._sync_company_observability(state)
    _messages_again, total_again = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=10, offset=0)
    assert total_again == total

    scheduler.delete_schedule(tracked_schedule["id"])
    scheduler.delete_schedule(dedicated_schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_ignores_stale_schedules_outside_state(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.schedule_store import append_history, save_schedule
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
            "desktops": [],
        }),
    )

    parent = ChatStore().create_conversation(
        model="opencode-go/mimo-v2.5",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        agent_id="client_manager",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    scheduler = Scheduler()
    future_run_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    past_run_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

    def mimo_task(loop_key: str, *, model: str, agent_id: str, message: str) -> dict[str, object]:
        return {
            "message": message,
            "model": model,
            "conversation_id": parent["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": agent_id,
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": loop_key,
            },
        }

    state_kickoff = scheduler.create_schedule(
        "once",
        mimo_task(
            "kickoff_review",
            model="opencode-go/mimo-v2.5",
            agent_id="project_manager",
            message="State-owned completed kickoff review.",
        ),
        {"run_at": future_run_at},
        name="MiMo Coding Company kickoff review",
    )
    state_kickoff["status"] = "completed"
    state_kickoff["next_execution_at"] = None
    save_schedule(state_kickoff)
    append_history(
        state_kickoff["id"],
        {
            "schedule_id": state_kickoff["id"],
            "execution_id": "exec_state_completed_kickoff",
            "trigger": "scheduled",
            "status": "completed",
            "started_at": "2026-06-29T00:00:00Z",
            "completed_at": "2026-06-29T00:00:08Z",
            "result": "State kickoff completed and remains part of the current harness state.",
        },
    )

    current_mimo_qa = scheduler.create_schedule(
        "interval",
        mimo_task(
            "qa_loop",
            model="xiaomi-token-plan-sgp/mimo-v2-omni",
            agent_id="browser_qa",
            message="Current MiMo browser QA loop.",
        ),
        {"value": 240, "unit": "minutes"},
        name="MiMo Coding Company MiMo QA loop",
    )
    append_history(
        current_mimo_qa["id"],
        {
            "schedule_id": current_mimo_qa["id"],
            "execution_id": "exec_current_mimo_qa",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-29T00:05:00Z",
            "completed_at": "2026-06-29T00:05:08Z",
            "error": "Current MiMo QA desktop blocker.",
        },
    )

    paused_xiaomi = scheduler.create_schedule(
        "interval",
        mimo_task(
            "heartbeat",
            model="opencode-go/mimo-v2.5",
            agent_id="scheduler",
            message="Expired Xiaomi pre-expiry heartbeat.",
        ),
        {"value": 30, "unit": "minutes"},
        name="MiMo Xiaomi dedicated pre-expiry heartbeat",
    )
    scheduler.pause_schedule(paused_xiaomi["id"])
    append_history(
        paused_xiaomi["id"],
        {
            "schedule_id": paused_xiaomi["id"],
            "execution_id": "exec_paused_xiaomi_timeout",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-28T23:30:00Z",
            "completed_at": "2026-06-28T23:30:08Z",
            "error": "Old paused Xiaomi schedule timed out.",
        },
    )

    completed_dedicated = scheduler.create_schedule(
        "once",
        mimo_task(
            "dedicated_manager",
            model="opencode-go/mimo-v2.5",
            agent_id="project_manager",
            message="Old dedicated MiMo loop.",
        ),
        {"run_at": future_run_at},
        name="MiMo old dedicated manager",
    )
    completed_dedicated["status"] = "completed"
    completed_dedicated["next_execution_at"] = None
    save_schedule(completed_dedicated)
    append_history(
        completed_dedicated["id"],
        {
            "schedule_id": completed_dedicated["id"],
            "execution_id": "exec_completed_dedicated_error",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-28T22:00:00Z",
            "completed_at": "2026-06-28T22:00:08Z",
            "error": "Old completed dedicated schedule error.",
        },
    )

    active_stub = scheduler.create_schedule(
        "interval",
        mimo_task(
            "qa_loop",
            model="stub/default",
            agent_id="browser_qa",
            message="Stub/default test QA loop.",
        ),
        {"value": 15, "unit": "minutes"},
        name="MiMo stub/default test loop",
    )
    append_history(
        active_stub["id"],
        {
            "schedule_id": active_stub["id"],
            "execution_id": "exec_active_stub_error",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-29T00:10:00Z",
            "completed_at": "2026-06-29T00:10:08Z",
            "error": "Active stub/default test loop error.",
        },
    )

    active_xiaomi = scheduler.create_schedule(
        "interval",
        mimo_task(
            "qa_loop",
            model="opencode-go/mimo-v2.5",
            agent_id="browser_qa",
            message="Expired Xiaomi active QA loop.",
        ),
        {"value": 15, "unit": "minutes"},
        name="MiMo expired Xiaomi active loop",
    )
    append_history(
        active_xiaomi["id"],
        {
            "schedule_id": active_xiaomi["id"],
            "execution_id": "exec_active_xiaomi_error",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-29T00:11:00Z",
            "completed_at": "2026-06-29T00:11:08Z",
            "error": "Active expired Xiaomi loop should not be current.",
        },
    )

    expired_active = scheduler.create_schedule(
        "once",
        mimo_task(
            "improvement_loop",
            model="opencode-go/mimo-v2.5",
            agent_id="project_manager",
            message="Expired Xiaomi pre-expiry improvement sprint.",
        ),
        {"run_at": past_run_at},
        name="MiMo Xiaomi expired active one-shot",
    )
    append_history(
        expired_active["id"],
        {
            "schedule_id": expired_active["id"],
            "execution_id": "exec_expired_active_xiaomi",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-28T20:00:00Z",
            "completed_at": "2026-06-28T20:00:08Z",
            "error": "Expired Xiaomi one-shot should not be current.",
        },
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    state = {
        "conversation_id": parent["id"],
        "conversation_group_id": "company:mimo-coding-company",
        "schedule_ids": {"kickoff_review": state_kickoff["id"]},
    }
    summary = runtime._sync_company_observability(state)
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=20, offset=0)

    observed_schedule_ids = {item["schedule_id"] for item in summary["schedule_history"]["latest"]}
    assert observed_schedule_ids == {state_kickoff["id"], current_mimo_qa["id"]}
    assert summary["schedule_history"]["checked"] == 2
    assert summary["team_workspace"]["synced_messages"] == 2
    assert total == 2
    assert {message["metadata"]["schedule_id"] for message in messages} == observed_schedule_ids
    assert "schedule_error" in {item["signal"] for item in summary["schedule_history"]["signals"]}
    assert not {
        paused_xiaomi["id"],
        completed_dedicated["id"],
        active_stub["id"],
        active_xiaomi["id"],
        expired_active["id"],
    } & observed_schedule_ids

    for schedule in (
        state_kickoff,
        current_mimo_qa,
        paused_xiaomi,
        completed_dedicated,
        active_stub,
        active_xiaomi,
        expired_active,
    ):
        scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_suppresses_expected_schedule_noise(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.schedule_store import append_history
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", raising=False)
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
        }),
    )

    parent = ChatStore().create_conversation(
        model="opencode-go/mimo-v2.5",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "QA loop.",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "conversation_id": parent["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "browser_qa",
            "tool_policy": {
                "profile_id": "defaultspack.mimo_coding_company",
                "schedule_suppress_external_issue_on": [
                    "already_running",
                    "conversation_running",
                    "scheduled_timeout",
                ],
            },
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "qa_loop",
                "schedule_suppress_external_issue_on": [
                    "already_running",
                    "conversation_running",
                    "scheduled_timeout",
                ],
            },
        },
        {"value": 30, "unit": "minutes"},
        name="MiMo Coding Company qa loop",
    )
    append_history(
        schedule["id"],
        {
            "schedule_id": schedule["id"],
            "execution_id": "exec_overlap",
            "trigger": "scheduled",
            "status": "skipped",
            "started_at": "2026-06-29T00:00:00Z",
            "completed_at": "2026-06-29T00:00:00Z",
            "error": "conversation is already running: " + parent["id"],
            "error_code": "CONVERSATION_RUNNING",
            "skipped_reason": "conversation_running",
        },
    )
    append_history(
        schedule["id"],
        {
            "schedule_id": schedule["id"],
            "execution_id": "exec_timeout",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-29T00:30:00Z",
            "completed_at": "2026-06-29T01:00:00Z",
            "error": "scheduled task timed out after 1800 seconds",
        },
    )
    append_history(
        schedule["id"],
        {
            "schedule_id": schedule["id"],
            "execution_id": "exec_real_error",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-29T01:30:00Z",
            "completed_at": "2026-06-29T01:30:08Z",
            "error": "handler execution failed for desktop_frame",
        },
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    summary = runtime._sync_company_observability(
        {
            "conversation_id": parent["id"],
            "conversation_group_id": "company:mimo-coding-company",
            "schedule_ids": {"qa_loop": schedule["id"]},
        }
    )
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=20, offset=0)

    latest_by_execution = {item["execution_id"]: item for item in summary["schedule_history"]["latest"]}
    assert latest_by_execution["exec_overlap"]["suppressed"] is True
    assert latest_by_execution["exec_overlap"]["suppressed_reason"] == "conversation_running"
    assert latest_by_execution["exec_timeout"]["suppressed"] is True
    assert latest_by_execution["exec_timeout"]["suppressed_reason"] == "scheduled_timeout"
    assert latest_by_execution["exec_real_error"]["signal"] == "tool_handler_failure"
    assert summary["team_workspace"]["synced_messages"] == 1
    assert total == 1
    assert messages[0]["metadata"]["execution_id"] == "exec_real_error"

    scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_suppresses_timeout_without_schedule_config():
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    reason = MimoCodingCompanyRuntime._schedule_noise_suppression_reason(
        None,
        {"status": "error", "error": "scheduled task timed out after 900 seconds"},
    )

    assert reason == "scheduled_timeout"


def test_mimo_coding_company_observability_classifies_provider_credit_blocker(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        DEFAULT_MAIN_MODEL,
        DEFAULT_VISION_MODEL,
        MimoCodingCompanyRuntime,
    )
    from domain.agent.schedule_store import append_history
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
        }),
    )

    parent = ChatStore().create_conversation(
        model=DEFAULT_MAIN_MODEL,
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "Improvement loop.",
            "model": DEFAULT_MAIN_MODEL,
            "conversation_id": parent["id"],
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "project_manager",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "company_id": "mimo-coding-company",
                "conversation_id": parent["id"],
                "conversation_group_id": "company:mimo-coding-company",
                "loop_key": "improvement_loop",
            },
        },
        {"value": 30, "unit": "minutes"},
        name="MiMo Coding Company improvement loop",
    )
    append_history(
        schedule["id"],
        {
            "schedule_id": schedule["id"],
            "execution_id": "exec_mimo_credits_blocker",
            "trigger": "scheduled",
            "status": "error",
            "started_at": "2026-06-29T01:30:00Z",
            "completed_at": "2026-06-29T01:30:08Z",
            "error": (
                "CreditsError: insufficient balance for xiaomi-token-plan-sgp/mimo-v2.5-pro. "
                "HTTP 401 Unauthorized. Authorization: Bearer sk-test-secret"
            ),
        },
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    summary = runtime._sync_company_observability(
        {
            "conversation_id": parent["id"],
            "conversation_group_id": "company:mimo-coding-company",
            "main_model": DEFAULT_MAIN_MODEL,
            "vision_model": DEFAULT_VISION_MODEL,
            "schedule_ids": {"improvement_loop": schedule["id"]},
        }
    )
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=20, offset=0)

    latest = summary["schedule_history"]["latest"][0]
    provider_health = summary["provider_health"]
    assert summary["status"] == "provider_blocked"
    assert latest["signal"] == "provider_health_blocker"
    assert latest["external_blocker"] is True
    assert latest["provider_health"]["configured_model"] == DEFAULT_MAIN_MODEL
    assert latest["provider_health"]["reason"] == "credits_error"
    assert {"CreditsError", "insufficient balance", "HTTP 401"} <= set(latest["provider_health"]["evidence"])
    assert provider_health["status"] == "blocked"
    assert provider_health["blocked"] is True
    assert provider_health["configured_model"] == DEFAULT_MAIN_MODEL
    assert provider_health["blocked_reason"] == "credits_error"
    assert provider_health["signals"][0]["signal"] == "provider_health_blocker"
    assert total == 1
    assert messages[0]["metadata"]["signal"] == "provider_health_blocker"
    assert messages[0]["metadata"]["external_issue_policy"] == "provider_health_only"
    assert messages[0]["metadata"]["provider_health"]["configured_model"] == DEFAULT_MAIN_MODEL
    assert "provider-health blocker" in messages[0]["content"]
    assert DEFAULT_MAIN_MODEL in messages[0]["content"]
    assert "CreditsError" in messages[0]["content"]
    assert "insufficient balance" in messages[0]["content"]
    assert "HTTP 401" in messages[0]["content"]
    assert "Do not create GitHub issues" in messages[0]["content"]
    assert "MiMo vision QA monitoring active" in messages[0]["content"]
    assert "Authorization" not in messages[0]["content"]
    assert "sk-test-secret" not in messages[0]["content"]

    scheduler.delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_observability_resolves_stale_subagent_unanswered_message(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_desktop_monitoring_observation",
        staticmethod(lambda: {
            "surface": "desktops",
            "expected_api": "GET /api/desktops",
            "status": "ok",
            "desktop_count": 1,
            "desktops": [],
        }),
    )

    chat_store = ChatStore()
    parent = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        conversation_kind="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    child = chat_store.create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        agent_id="subagent",
        group_id="company:mimo-coding-company",
        metadata={"company_id": "mimo-coding-company", "profile_id": "defaultspack.mimo_coding_company"},
    )
    chat_store.update_conversation(child["id"], {"title": "Previously stale subagent"})
    chat_store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Please answer once the child runner resumes."}],
        },
    )
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    chat_store._conversations[child["id"]]["created_at"] = old_timestamp
    chat_store._conversations[child["id"]]["updated_at"] = old_timestamp
    chat_store._save_conversations()

    runtime_store = CompanyRuntimeStore()
    runtime_store.add_message(
        "mimo-coding-company",
        channel_id="ops-company",
        sender_id="scheduler",
        content="**MiMo subagent child conversation has no assistant reply**",
        metadata={
            "sync_source": "mimo_subagent_monitor",
            "sync_key": "subagent_gap:" + child["id"],
            "child_conversation_id": child["id"],
            "parent_conversation_id": parent["id"],
            "signal": "subagent_unanswered",
        },
    )

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    summary = runtime._sync_company_observability({"conversation_id": parent["id"], "schedule_ids": {}})
    messages, total = CompanyRuntimeStore().list_messages("mimo-coding-company", limit=5, offset=0, order="desc")

    assert summary["status"] == "ok"
    assert summary["subagents"]["repaired"] == []
    assert summary["subagents"]["unanswered_count"] == 0
    assert summary["subagents"]["unanswered"] == []
    assert summary["subagents"]["failed_count"] == 1
    assert summary["subagents"]["failed"][0]["child_conversation_id"] == child["id"]
    assert summary["subagents"]["resolved_message_count"] == 1
    assert total == 2
    assert messages[0]["metadata"]["signal"] == "subagent_failed"
    assert messages[0]["metadata"].get("resolved") is not True
    assert "failed before a successful assistant reply" in messages[0]["content"]
    assert messages[1]["metadata"]["signal"] == "subagent_repaired"
    assert messages[1]["metadata"].get("resolved") is True

    _reset_defaultspack_singletons()


def test_mimo_coding_company_static_knowledge_and_docker_bundles_exist():
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    runtime = MimoCodingCompanyRuntime()
    manifest = runtime.manifest()
    docker_paths = manifest["docker"]["template_paths"]
    knowledge_docs = manifest["knowledge_bundle"]["documents"]

    assert Path(docker_paths["compose"]).is_file()
    assert Path(docker_paths["dockerfile"]).is_file()
    assert Path(docker_paths["entrypoint"]).is_file()
    assert Path(docker_paths["personas"]).is_file()
    assert knowledge_docs
    assert all(Path(path).is_file() for path in knowledge_docs)


def test_mimo_coding_company_manifest_uses_explicit_mimo_and_vision_model_allowlist(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))
    runtime = MimoCodingCompanyRuntime()
    allowlist = set(runtime.manifest()["model_self_selection"]["allowlist"])

    assert allowlist == {
        "xiaomi-token-plan-sgp/mimo-v2.5-pro",
        "xiaomi-token-plan-sgp/mimo-v2.5",
        "xiaomi-token-plan-sgp/mimo-v2-pro",
        "xiaomi-token-plan-sgp/mimo-v2-omni",
        "xiaomi-token-plan-sgp/mimo-v2-flash",
        "gitlawb-opengateway/mimo-v2.5-pro",
        "gitlawb-opengateway/mimo-v2.5",
        "gitlawb-opengateway/mimo-v2-pro",
        "gitlawb-opengateway/mimo-v2-omni",
        "gitlawb-opengateway/mimo-v2-flash",
        "groq/openai/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        "stub/default",
    }


def test_mimo_coding_company_bootstrap_block_rejects_catalog_and_free_models(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.blocks.agent.mimo_company import bootstrap

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    for model in (
        "groq/openai/gpt-oss-20b",
        "cerebras/zai-glm-4.7",
        "opencode-go/minimax-m3",
    ):
        result = bootstrap.run(
            {
                "start_nonstop": True,
                "heartbeat_minutes": 30,
                "review_interval_minutes": 180,
                "qa_interval_minutes": 240,
                "model": model,
                "vision_model": "stub/default",
                "fast_model": "stub/default",
                "seed_knowledge": False,
                "run_initial_review_now": False,
            },
            {},
        )

        assert result["status"] == "error"
        assert result["error"]["code"] == "MODEL_NOT_ALLOWED"
    _reset_defaultspack_singletons()


def test_mimo_coding_company_bootstrap_block_accepts_non_docker_worker_mode(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.blocks.agent.mimo_company import bootstrap
    from domain.agent.scheduler import Scheduler

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))

    result = bootstrap.run(
        {
            "start_nonstop": True,
            "heartbeat_minutes": 30,
            "review_interval_minutes": 180,
            "qa_interval_minutes": 240,
            "model": "stub/default",
            "vision_model": "stub/default",
            "fast_model": "stub/default",
            "qa_targets": ["http://127.0.0.1:3000"],
            "worker_mode": "managed_desktop",
            "seed_knowledge": False,
            "run_initial_review_now": False,
        },
        {},
    )

    assert result["status"] == "ok"
    docker_swarm = result["data"]["harness"]["docker_swarm"]
    assert docker_swarm["enabled"] is False
    assert docker_swarm["worker_count"] == 0
    assert docker_swarm["monitoring"]["disabled"] is True
    assert result["data"]["harness"]["qa_swarm_plan"]["runtime_mode"] == "managed_desktop"

    for schedule in result["data"]["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_conversation_resolves_pack_system_prompt():
    from blocks.chat.send import _conversation_system_prompt
    from domain.prompt.manager import get_manager

    prompt = _conversation_system_prompt({"system_prompt_id": "mimo_coding_company"}, get_manager())

    assert "MiMo Coding Company" in prompt
    assert "Toolsmith builds missing tools or skills instead of stopping" in prompt


def test_operations_heartbeat_trigger_persists_into_single_client_conversation(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.operations_company import OperationsCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_OPERATIONS_STATE_PATH", str(tmp_path / "ops" / "state.json"))

    status = OperationsCompanyRuntime().bootstrap(start_nonstop=True, heartbeat_minutes=30, model="stub/default")
    conversation_id = status["conversation_id"]
    schedule_id = status["schedules"][0]["id"]

    result = Scheduler().trigger_now(schedule_id)

    assert result["status"] == "completed"
    conversation = ChatStore().get_conversation(conversation_id)
    roles = [message["role"] for message in conversation["messages"]]
    assert roles == ["user", "assistant"]
    assert conversation["messages"][0]["metadata"]["source"] == "scheduler"
    assert conversation["messages"][0]["metadata"]["profile_id"] == "defaultspack.operations_company"

    persisted = json.loads((tmp_path / "chat" / "conversations.json").read_text(encoding="utf-8"))
    assert persisted["conversations"][conversation_id]["messages"][0]["metadata"]["schedule_id"] == schedule_id

    Scheduler().delete_schedule(schedule_id)
    _reset_defaultspack_singletons()


def test_rumi_api_tool_lists_routes_and_requires_mutation_approval():
    from ecosystem.rumi_default_tools_pack.domain.tool.rumi_api import run

    listed = run({"action": "list_routes"}, {})
    mutation = run(
        {
            "action": "request",
            "method": "POST",
            "path": "/api/agent/company/bootstrap",
            "body": {"start_nonstop": True},
            "allow_mutation": True,
        },
        {"profile_policy": {"yolo_mode": False}},
    )

    assert listed["status"] == "ok"
    assert any(route["path"] == "/api/agent/company/status" for route in listed["data"]["routes"])
    assert any(route["path"] == "/api/agent/mimo-company/status" for route in listed["data"]["routes"])
    assert mutation["status"] == "ok"
    assert mutation["data"]["approval_required"] is True
