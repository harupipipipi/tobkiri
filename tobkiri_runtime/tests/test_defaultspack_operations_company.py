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


def test_operations_company_catalog_owns_its_company_profiles():
    from domain.capability.catalog import CapabilityCatalog

    manifest = CapabilityCatalog(DEFAULTSPACK_ROOT).manifest()
    profile_ids = {profile["profile_id"] for profile in manifest["profiles"]}

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


def test_mimo_coding_company_bootstrap_requires_captured_operation(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/company/mimo/bootstrap",
        "tobkiri.operations-company.v1",
        "rumi_operations_company.bootstrap",
    )


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


def test_mimo_company_schedule_arming_requires_captured_operation(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/company/mimo/schedules/arm",
        "tobkiri.operations-company.v1",
        "rumi_operations_company.schedule-arm",
    )


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
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    chat_store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Simple test: List 3 things you can do as a subagent."}],
            "created_at": old_timestamp,
            "updated_at": old_timestamp,
        },
    )
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
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_conversation_age_seconds",
        staticmethod(
            lambda conversation: 600
            if conversation.get("id") == child["id"]
            else 0
        ),
    )

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


def test_mimo_company_workspace_channels_use_selected_state_without_schedule_sync(tmp_path, monkeypatch):
    from blocks.company import bootstrap, channels, messages

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))

    company_id = bootstrap.run(
        {"metadata": {"name": "MiMo Coding Company"}},
        {},
    )["data"]["company"]["id"]
    channel = channels.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "channel": {"id": "ops-company", "name": "Operations"},
        },
        {},
    )
    created = messages.run(
        {
            "action": "create",
            "company_id": company_id,
            "channel_id": "ops-company",
            "sender_id": "scheduler",
            "content": "Selected Company state message.",
            "metadata": {"sync_source": "explicit_test"},
        },
        {},
    )
    listed = channels.run({"company_id": company_id}, {})
    listed_messages = messages.run(
        {"company_id": company_id, "channel_id": "ops-company"},
        {},
    )

    assert channel["status"] == "ok"
    assert created["status"] == "ok"
    assert [item["id"] for item in listed["data"]["channels"]] == ["ops-company"]
    assert listed_messages["data"]["total"] == 1
    assert listed_messages["data"]["messages"][0]["text"] == "Selected Company state message."
    assert "message_count" not in listed["data"]["channels"][0]


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
    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
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
            "created_at": old_timestamp,
            "updated_at": old_timestamp,
        },
    )

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

    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
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
            "created_at": old_timestamp,
            "updated_at": old_timestamp,
        },
    )

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


def test_mimo_coding_company_scheduled_user_gaps_uses_selected_conversation_owner(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", str(tmp_path / "schedules"))

    old_timestamp = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    schedule_id = "sched-owner-history"
    chat_store = ChatStore()
    parent = chat_store.create_conversation(
        model="stub/default",
        conversation_kind="mimo_coding_company",
    )
    loop = chat_store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="mimo_coding_company_loop",
        metadata={
            "company_id": "mimo-coding-company",
            "profile_id": "defaultspack.mimo_coding_company",
            "parent_conversation_id": parent["id"],
            "loop_key": "qa_loop",
        },
    )
    user = chat_store.add_message(
        loop["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Continue this approved scheduled task."}],
            "created_at": old_timestamp,
            "updated_at": old_timestamp,
            "metadata": {
                "source": "scheduler_approval_followup",
                "schedule_id": schedule_id,
                "schedule_execution_id": "sexec-owner-history",
                "loop_key": "qa_loop",
            },
        },
    )

    class NoRunningSchedules:
        def get_schedule(self, requested_schedule_id):
            assert requested_schedule_id == schedule_id
            return None

    summary = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")._scheduled_user_gaps(
        {
            "conversation_id": parent["id"],
            "loop_conversation_ids": {"qa_loop": loop["id"]},
            "schedule_ids": {"qa_loop": schedule_id},
        },
        scheduler=NoRunningSchedules(),
    )
    repaired_loop = ChatStore().get_conversation(loop["id"])
    repaired = repaired_loop["messages"][-1]

    assert summary["checked"] == 1
    assert summary["repaired"] == [repaired["id"]]
    assert repaired["role"] == "assistant"
    assert repaired["parent_id"] == user["id"]
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


def test_mimo_company_observability_requires_captured_operation(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/company/mimo/observability",
        "tobkiri.operations-company.v1",
        "rumi_operations_company.observe",
    )


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
    monkeypatch.setattr(
        MimoCodingCompanyRuntime,
        "_conversation_age_seconds",
        staticmethod(lambda conversation: 600 if conversation.get("id") == child["id"] else 0),
    )

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


def test_operations_heartbeat_requires_captured_operation(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/company/operations/heartbeat",
        "tobkiri.operations-company.v1",
        "rumi_operations_company.heartbeat",
    )


def test_rumi_api_tool_has_zero_legacy_routes_and_requires_dispatch_approval():
    from ecosystem.rumi_default_tools_pack.domain.tool.rumi_api import run

    listed = run({"action": "list_routes"}, {})
    pending = run(
        {
            "action": "dispatch",
            "contract_id": "company.operations.v1",
            "operation_id": "company.bootstrap",
            "payload": {"start_nonstop": True},
        },
        {"profile_policy": {"yolo_mode": False}},
    )

    assert listed["status"] == "ok"
    assert listed["data"] == {
        "routes": [],
        "count": 0,
        "dispatch": "captured_v4_qualified_operations_only",
    }
    assert pending["status"] == "ok"
    assert pending["data"]["approval_required"] is True
    assert pending["data"]["contract_id"] == "company.operations.v1"
    assert pending["data"]["operation_id"] == "company.bootstrap"
