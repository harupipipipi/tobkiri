from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


@pytest.fixture(autouse=True)
def _isolate_provider_capability_catalog(monkeypatch):
    monkeypatch.setattr(
        "domain.company.run_dispatcher.get_model_capabilities",
        lambda _model: {"supports_tool_calling": True},
    )
    monkeypatch.setattr(
        "ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company.get_all_known_models",
        lambda provider_id=None: [],
        raising=False,
    )


def _reset_company_store():
    from domain.agent_runtime.run_store import AgentRunStore
    from domain.company.runtime_store import CompanyRuntimeStore
    from domain.company.store import CompanyStore

    AgentRunStore._instance = None
    CompanyRuntimeStore._instance = None
    CompanyStore._instance = None


def _reset_defaultspack_singletons():
    from domain.agent.org_manager import OrgManager
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore
    from domain.company.runtime_store import CompanyRuntimeStore
    from domain.company.store import CompanyStore

    scheduler = Scheduler._instance
    if scheduler is not None:
        for schedule_id in list(getattr(scheduler, "_timers", {}).keys()):
            scheduler._cancel_timer(schedule_id)
    Scheduler._instance = None
    OrgManager._instance = None
    ChatStore._instance = None
    CompanyRuntimeStore._instance = None
    CompanyStore._instance = None


def test_company_store_crud_and_json_persistence(tmp_path, monkeypatch):
    from domain.company.store import CompanyStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    _reset_company_store()

    store = CompanyStore()
    company = store.create_company(
        company_id="acme",
        name="Acme Company",
        description="Test company",
        metadata={"source": "test"},
    )

    assert company["id"] == "acme"
    assert company["settings"]["dispatch_policy"] == "local_queue_only"
    assert "project_manager" in company["agents"]
    assert "ops-company" in company["channels"]

    updated = store.update_company("acme", {"name": "Acme Updated", "metadata": {"tier": "p2p"}})
    assert updated["name"] == "Acme Updated"
    assert updated["metadata"]["source"] == "test"
    assert updated["metadata"]["tier"] == "p2p"

    persisted = json.loads((tmp_path / "companies" / "companies.json").read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 1
    assert persisted["companies"]["acme"]["name"] == "Acme Updated"

    _reset_company_store()
    reloaded = CompanyStore().get_company("acme")
    assert reloaded["metadata"]["tier"] == "p2p"


def test_company_blocks_return_ok_error_envelopes(tmp_path, monkeypatch):
    from blocks.company import create, delete, get, list as company_list, update

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    _reset_company_store()

    created = create.run({"id": "blockco", "name": "Block Co"}, {})
    listed = company_list.run({}, {})
    fetched = get.run({"company_id": "blockco"}, {})
    updated = update.run({"company_id": "blockco", "updates": {"description": "Changed"}}, {})
    missing = get.run({"company_id": "missing"}, {})
    deleted = delete.run({"company_id": "blockco"}, {})

    assert created["status"] == "ok"
    assert listed["data"]["total"] == 1
    assert fetched["data"]["id"] == "blockco"
    assert updated["data"]["description"] == "Changed"
    assert missing["status"] == "error"
    assert missing["error"]["code"] == "NOT_FOUND"
    assert deleted["data"]["deleted"] is True


def test_company_status_bootstraps_employee_group_for_conversation(tmp_path, monkeypatch):
    from blocks.company import bootstrap, status

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    first = status.run({"conversation_id": "chat-main-1", "bootstrap": True}, {})
    second = status.run({"conversation_id": "chat-main-1"}, {})
    default = bootstrap.run({}, {})
    scoped = bootstrap.run(
        {
            "conversation_id": "chat-main-2",
            "scope": "conversation",
            "metadata": {"conversation_id": "chat-main-2", "source": "webapp"},
        },
        {},
    )

    assert first["status"] == "ok"
    assert first["data"]["bootstrapped"] is True
    assert first["data"]["company_id"].startswith("chat-team-chat-main-1")
    assert first["data"]["company"]["metadata"]["conversation_id"] == "chat-main-1"
    assert first["data"]["company"]["agents"]["client_manager"]["display_name"] == "Main Agent"
    assert second["data"]["company_id"] == first["data"]["company_id"]
    assert default["data"]["company"]["id"] == "operations-company"
    assert scoped["data"]["company"]["id"].startswith("chat-team-chat-main-2")


def test_conversation_employee_group_inherits_main_chat_model(tmp_path, monkeypatch):
    from blocks.company import status
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_defaultspack_singletons()

    conversation = ChatStore().create_conversation(model="stub/default")
    result = status.run({"conversation_id": conversation["id"], "bootstrap": True}, {})

    assert result["status"] == "ok"
    assert result["data"]["company"]["metadata"]["employee_model"] == "stub/default"
    assert result["data"]["company"]["agents"]["research_specialist"]["model"] == "stub/default"
    assert result["data"]["company"]["agents"]["coding_engineer"]["model"] == "stub/default"


def test_mentions_create_queued_tasks_and_dispatches_agent_runs(tmp_path, monkeypatch):
    from blocks.company import bootstrap, dispatch, mention, tasks as tasks_block

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    bootstrapped = bootstrap.run({}, {})
    company_id = bootstrapped["data"]["company"]["id"]

    resolved = mention.run(
        {
            "action": "resolve",
            "company_id": company_id,
            "content": "@pm please split this, @coding_engineer implement it, @reviewer review it",
        },
        {},
    )
    assert resolved["data"]["resolved_agent_ids"] == [
        "project_manager",
        "coding_engineer",
        "reviewer",
    ]

    created = mention.run(
        {
            "company_id": company_id,
            "sender_id": "client",
            "content": "@pm hand this to @coding_engineer and have @reviewer check it",
        },
        {},
    )
    mention_tasks = created["data"]["tasks"]
    assert {task["status"] for task in mention_tasks} == {"queued"}
    assert {task["source"] for task in mention_tasks} == {"mention"}
    assert {tuple(task["target_agent_ids"]) for task in mention_tasks} == {
        ("project_manager",),
        ("coding_engineer",),
        ("reviewer",),
    }
    assert created["data"]["message"]["metadata"]["task_ids"] == [
        task["id"] for task in mention_tasks
    ]

    dispatched = dispatch.run(
        {
            "company_id": company_id,
            "task_id": mention_tasks[0]["id"],
            "requested_by": "test",
            "policy": {"direct_tool_execution": True, "mode": "execute_now"},
        },
        {},
    )
    assert dispatched["status"] == "error"
    assert dispatched["error"]["code"] == "COMPANY_DISPATCH_ERROR"

    listed = tasks_block.run({"company_id": company_id, "status": "queued"}, {})
    assert listed["data"]["total"] == 3

    all_mentions = mention.run({"action": "resolve", "company_id": company_id, "content": "@all"}, {})
    assert len(all_mentions["data"]["resolved_agent_ids"]) == 9


def test_company_runs_are_explicit_runtime_route_sunset(tmp_path, monkeypatch):
    from blocks.company import runs

    result = runs.run({"company_id": "acme", "limit": 5}, {})

    assert result["status"] == "error"
    assert result["error"]["code"] == "COMPANY_RUNTIME_ROUTE_SUNSET"


def test_company_runs_do_not_reopen_legacy_runtime_store(tmp_path, monkeypatch):
    from blocks.company import runs

    listed = runs.run(
        {"company_id": "acme", "limit": "2", "offset": "1"}, {}
    )

    assert listed["status"] == "error"
    assert listed["error"]["code"] == "COMPANY_RUNTIME_ROUTE_SUNSET"


def test_inbound_routes_ingest_into_selected_company_state(tmp_path, monkeypatch):
    from blocks.company import bootstrap, inbound_routes, messages

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    route = inbound_routes.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "route": {
                "id": "slack-team",
                "provider": "slack",
                "source": "C123",
                "channel_id": "ops-company",
            },
        },
        {},
    )
    ingested = inbound_routes.run(
        {
            "action": "ingest",
            "company_id": company_id,
            "route_id": "slack-team",
            "sender_id": "U123",
            "content": "@reviewer check the latest build notes",
        },
        {},
    )
    listed_messages = messages.run({"company_id": company_id, "channel_id": "ops-company"}, {})

    assert route["data"]["id"] == "slack-team"
    assert ingested["data"]["actor_id"] == "U123"
    assert ingested["data"]["text"] == "@reviewer check the latest build notes"
    assert ingested["data"]["metadata"]["route_id"] == "slack-team"
    assert listed_messages["data"]["total"] == 0


def test_company_channels_use_selected_state_without_runtime_counts(tmp_path, monkeypatch):
    from blocks.company import bootstrap, channels, messages

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    channel = channels.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "channel": {"id": "ops-company", "name": "ops-company", "visibility": "team"},
        },
        {},
    )
    created = messages.run(
        {
            "action": "create",
            "company_id": company_id,
            "channel_id": "ops-company",
            "sender_id": "scheduler",
            "content": "MiMo workspace visibility check",
        },
        {},
    )
    latest_created = messages.run(
        {
            "action": "create",
            "company_id": company_id,
            "channel_id": "ops-company",
            "sender_id": "scheduler",
            "content": "MiMo workspace latest visibility check",
        },
        {},
    )
    listed = channels.run({"company_id": company_id}, {})
    fetched = channels.run({"action": "get", "company_id": company_id, "channel_id": "ops-company"}, {})

    assert channel["status"] == "ok"
    assert created["status"] == "ok"
    assert latest_created["status"] == "ok"
    ops_channel = next(channel for channel in listed["data"]["channels"] if channel["id"] == "ops-company")
    assert ops_channel["name"] == "ops-company"
    assert "message_count" not in ops_channel
    assert "last_message_at" not in ops_channel
    assert fetched["data"]["id"] == "ops-company"
    assert "message_count" not in fetched["data"]


def test_company_channels_do_not_sync_mimo_runtime_state(tmp_path, monkeypatch):
    from blocks.company import bootstrap, channels

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run(
        {
            "conversation_id": "mimo-channel-test",
            "scope": "conversation",
        },
        {},
    )["data"]["company"]["id"]
    created = channels.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "channel": {"id": "qa", "name": "qa", "visibility": "team"},
        },
        {},
    )
    listed = channels.run({"company_id": company_id}, {})
    fetched = channels.run({"action": "get", "company_id": company_id, "channel_id": "ops-company"}, {})

    assert created["status"] == "ok"
    assert listed["data"]["channels"] == [created["data"]]
    assert "message_count" not in created["data"]
    assert fetched["status"] == "error"
    assert fetched["error"]["code"] == "NOT_FOUND"


def test_company_messages_accept_string_limit_and_offset(tmp_path, monkeypatch):
    from blocks.company import bootstrap, messages

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    for index in range(60):
        messages.run(
            {
                "action": "create",
                "company_id": company_id,
                "channel_id": "ops-company",
                "sender_id": "scheduler",
                "content": f"MiMo sync message {index}",
            },
            {},
        )

    listed = messages.run({"company_id": company_id, "limit": "55", "offset": "2"}, {})

    assert listed["status"] == "ok"
    assert listed["data"]["total"] == 60
    assert len(listed["data"]["messages"]) == 55


def test_company_messages_tail_returns_latest_messages_in_chronological_order(tmp_path, monkeypatch):
    from blocks.company import bootstrap, messages

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    for index in range(100):
        messages.run(
            {
                "action": "create",
                "company_id": company_id,
                "channel_id": "ops-company",
                "sender_id": "scheduler",
                "content": f"MiMo long-run message {index}",
            },
            {},
        )

    listed = messages.run({"company_id": company_id, "limit": 5, "tail": True}, {})

    assert listed["status"] == "ok"
    assert listed["data"]["total"] == 100
    assert [message["text"] for message in listed["data"]["messages"]] == [
        "MiMo long-run message 95",
        "MiMo long-run message 96",
        "MiMo long-run message 97",
        "MiMo long-run message 98",
        "MiMo long-run message 99",
    ]


def test_company_messages_order_desc_returns_newest_first(tmp_path, monkeypatch):
    from blocks.company import bootstrap, messages

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    for index in range(6):
        messages.run(
            {
                "action": "create",
                "company_id": company_id,
                "channel_id": "ops-company",
                "sender_id": "scheduler",
                "content": f"MiMo ordered message {index}",
            },
            {},
        )

    listed = messages.run({"company_id": company_id, "limit": 3, "order": "desc"}, {})

    assert listed["status"] == "ok"
    assert listed["data"]["total"] == 6
    assert [message["text"] for message in listed["data"]["messages"]] == [
        "MiMo ordered message 5",
        "MiMo ordered message 4",
        "MiMo ordered message 3",
    ]


def test_company_get_and_status_project_selected_state_only(tmp_path, monkeypatch):
    from blocks.company import bootstrap, get, messages, status
    from blocks.company import channels

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    channels.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "channel": {"id": "ops-company", "name": "ops-company", "visibility": "team"},
        },
        {},
    )
    created = messages.run(
        {
            "action": "create",
            "company_id": company_id,
            "channel_id": "ops-company",
            "sender_id": "scheduler",
            "content": "MiMo Team Workspace GUI sync check",
        },
        {},
    )

    fetched = get.run({"company_id": company_id}, {})
    runtime_status = status.run({"company_id": company_id}, {})

    assert created["status"] == "ok"
    assert fetched["data"]["channels"]["ops-company"]["id"] == "ops-company"
    assert "message_count" not in fetched["data"]
    assert "runtime_counts" not in fetched["data"]
    assert runtime_status["data"]["runtime"]["messages"] == 1
    assert runtime_status["data"]["runtime"]["tasks"] == 0
    assert "message_count" not in runtime_status["data"]["company"]


def test_company_status_counts_selected_state_records(tmp_path, monkeypatch):
    from blocks.company import bootstrap, messages, status, tasks

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    manual_task = tasks.run(
        {
            "action": "create",
            "company_id": company_id,
            "title": "QA company task persisted 20260703",
        },
        {},
    )
    mention_task = tasks.run(
        {
            "action": "create",
            "company_id": company_id,
            "title": "Mention request for project_manager",
            "target_agent_ids": ["project_manager"],
            "source": "mention",
        },
        {},
    )
    created_message = messages.run(
        {
            "action": "create",
            "company_id": company_id,
            "channel_id": "ops-company",
            "sender_id": "user",
            "content": "QA persisted message 20260703",
        },
        {},
    )

    listed_tasks = tasks.run({"company_id": company_id}, {})
    listed_messages = messages.run({"company_id": company_id}, {})
    runtime_status = status.run({"company_id": company_id}, {})

    assert manual_task["status"] == "ok"
    assert mention_task["status"] == "ok"
    assert created_message["status"] == "ok"
    assert listed_tasks["data"]["total"] == 2
    assert listed_messages["data"]["total"] == 1

    company = runtime_status["data"]["company"]
    assert runtime_status["data"]["runtime"]["tasks"] == listed_tasks["data"]["total"]
    assert (
        runtime_status["data"]["runtime"]["messages"]
        == listed_messages["data"]["total"]
    )
    assert "tasks" not in company
    assert "messages" not in company
    assert "task_count" not in company
    assert "message_count" not in company


def test_company_get_and_status_include_selected_channels(tmp_path, monkeypatch):
    from blocks.company import bootstrap, channels, get, messages, status

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    created_channel = channels.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "channel": {"id": "qa-findings", "name": "QA Findings", "visibility": "team"},
        },
        {},
    )
    created = messages.run(
        {
            "action": "create",
            "company_id": company_id,
            "channel_id": "qa-findings",
            "sender_id": "mimo",
            "content": "MiMo runtime-only channel finding",
        },
        {},
    )

    fetched = get.run({"company_id": company_id}, {})
    runtime_status = status.run({"company_id": company_id}, {})
    listed = channels.run({"company_id": company_id}, {})
    channel_get = channels.run({"action": "get", "company_id": company_id, "channel_id": "qa-findings"}, {})

    assert created["status"] == "ok"
    listed_channel = next(channel for channel in listed["data"]["channels"] if channel["id"] == "qa-findings")
    assert listed_channel == created_channel["data"]
    assert "message_count" not in listed_channel
    assert "message_count" not in channel_get["data"]
    assert "message_count" not in fetched["data"]
    assert fetched["data"]["channels"]["qa-findings"]["id"] == "qa-findings"
    assert runtime_status["data"]["runtime"]["messages"] == 1
    assert runtime_status["data"]["company"]["channels"]["qa-findings"]["id"] == "qa-findings"


def test_company_channel_get_requires_selected_company_record(tmp_path, monkeypatch):
    from blocks.company import bootstrap, channels, delete

    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", str(tmp_path / "company_runtime.db"))
    _reset_company_store()

    company_id = bootstrap.run({}, {})["data"]["company"]["id"]
    created = channels.run(
        {
            "action": "upsert",
            "company_id": company_id,
            "channel": {"id": "qa-findings", "name": "QA Findings", "visibility": "team"},
        },
        {},
    )
    deleted = delete.run({"company_id": company_id}, {})
    fetched = channels.run({"action": "get", "company_id": company_id, "channel_id": "qa-findings"}, {})

    assert created["status"] == "ok"
    assert deleted["status"] == "ok"
    assert deleted["data"]["deleted"] is True
    assert fetched["status"] == "error"
    assert fetched["error"]["code"] == "NOT_FOUND"


def test_operations_company_runtime_syncs_default_company_record(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.operations_company import OperationsCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_OPERATIONS_STATE_PATH", str(tmp_path / "ops" / "state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))

    status = OperationsCompanyRuntime().bootstrap(start_nonstop=True, heartbeat_minutes=30, model="stub/default")

    assert status["bootstrapped"] is True
    assert status["company"]["id"] == "operations-company"
    assert status["company"]["conversation_group_id"] == "company:operations-company"
    assert status["company"]["metadata"]["legacy_org_id"] == status["org_id"]
    assert status["company"]["metadata"]["conversation_id"] == status["conversation_id"]
    assert status["company"]["agents"]["project_manager"]["system_prompt"]
    assert "task decomposition" in status["company"]["agents"]["project_manager"]["system_prompt"]

    conversation = ChatStore().get_conversation(status["conversation_id"])
    assert conversation["group_id"] == "company:operations-company"

    persisted = json.loads((tmp_path / "companies" / "companies.json").read_text(encoding="utf-8"))
    assert persisted["companies"]["operations-company"]["metadata"]["legacy_org_id"] == status["org_id"]

    for schedule in status["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()


def test_mimo_coding_company_runtime_syncs_default_company_record(tmp_path, monkeypatch):
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime
    from domain.agent.scheduler import Scheduler
    from domain.chat.store import ChatStore

    _reset_defaultspack_singletons()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MIMO_CODING_STATE_PATH", str(tmp_path / "mimo" / "state.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(tmp_path / "companies"))

    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path / "ops_pack")
    status = runtime.bootstrap(
        start_nonstop=True,
        heartbeat_minutes=30,
        review_interval_minutes=180,
        qa_interval_minutes=240,
        model="stub/default",
        vision_model="stub/default",
        fast_model="stub/default",
        seed_knowledge=False,
    )

    assert status["bootstrapped"] is True
    assert status["company"]["id"] == "mimo-coding-company"
    assert status["company"]["conversation_group_id"] == "company:mimo-coding-company"
    assert status["company"]["metadata"]["conversation_id"] == status["conversation_id"]
    assert status["company"]["metadata"]["self_improving"] is True
    assert status["company"]["metadata"]["autonomy_board"]["next_focus"][0]["id"] == "initial_harness_review"
    assert status["company"]["metadata"]["qa_swarm_plan"]["workers"][0]["mission"]
    assert len(status["company"]["metadata"]["stream_task_ids"]) == 6
    assert status["company"]["metadata"]["docker_swarm"]["monitoring"]["total_workers"] >= 1
    assert status["company"]["agents"]["toolsmith"]["system_prompt"]
    assert "build the smallest viable one instead of stopping" in status["company"]["agents"]["toolsmith"]["system_prompt"]

    conversation = ChatStore().get_conversation(status["conversation_id"])
    assert conversation["group_id"] == "company:mimo-coding-company"

    persisted = json.loads((tmp_path / "companies" / "companies.json").read_text(encoding="utf-8"))
    assert persisted["companies"]["mimo-coding-company"]["metadata"]["conversation_id"] == status["conversation_id"]

    for schedule in status["schedules"]:
        Scheduler().delete_schedule(schedule["id"])
    _reset_defaultspack_singletons()
