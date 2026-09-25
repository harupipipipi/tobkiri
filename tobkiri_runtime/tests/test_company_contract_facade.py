from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def test_company_facade_filters_and_pages_messages(monkeypatch) -> None:
    """Message compatibility reads keep their legacy filter and paging behavior."""

    import domain.company.contract_facade as contract_facade

    monkeypatch.setattr(contract_facade, "_profile_id", lambda: "test")
    CompanyContractFacade = contract_facade.CompanyContractFacade

    facade = CompanyContractFacade(
        {
            "company_id": "acme",
            "channel_id": "engineering",
            "thread_id": "thread-1",
            "order": "desc",
            "limit": 1,
        },
        {},
    )
    monkeypatch.setattr(
        facade,
        "_raw_company",
        lambda _company_id: {
            "messages": [
                {
                    "id": "message-1",
                    "channel_id": "engineering",
                    "created_at_ms": 1,
                    "metadata": {"thread_id": "thread-1"},
                },
                {
                    "id": "message-2",
                    "channel_id": "engineering",
                    "created_at_ms": 2,
                    "metadata": {"thread_id": "thread-1"},
                },
                {
                    "id": "message-other",
                    "channel_id": "general",
                    "created_at_ms": 3,
                    "metadata": {"thread_id": "thread-1"},
                },
            ]
        },
    )

    assert facade.run("list_messages") == {
        "messages": [
            {
                "id": "message-2",
                "channel_id": "engineering",
                "created_at_ms": 2,
                "metadata": {"thread_id": "thread-1"},
            }
        ],
        "total": 2,
    }


def test_company_messages_block_uses_contract_facade(monkeypatch) -> None:
    """The legacy message block must not reopen local Company stores."""

    from blocks.company import messages

    calls: list[str] = []

    class FakeFacade:
        def __init__(self, input_data, context) -> None:
            self.input_data = input_data

        def run(self, operation: str):
            calls.append(operation)
            if operation == "get":
                return {"id": "acme", "metadata": {}, "settings": {}}
            if operation == "append_message":
                return {"id": "message-1", "text": "hello"}
            raise AssertionError(operation)

    monkeypatch.setattr(messages, "CompanyContractFacade", FakeFacade)

    result = messages.run({"company_id": "acme", "action": "create", "content": "hello"}, {})

    assert result == {"status": "ok", "data": {"id": "message-1", "text": "hello"}}
    assert calls == ["get", "append_message"]


def test_company_status_projects_selected_state_without_runtime_store(
    monkeypatch,
) -> None:
    """Status derives its counts from the selected Company state record."""

    import domain.company.contract_facade as contract_facade

    monkeypatch.setattr(contract_facade, "_profile_id", lambda: "test")
    facade = contract_facade.CompanyContractFacade({"company_id": "acme"}, {})
    monkeypatch.setattr(
        facade,
        "_raw_company",
        lambda _company_id: {
            "id": "acme",
            "name": "Acme",
            "members": {},
            "roles": {},
            "channels": {},
            "messages": [{"id": "message-1"}],
            "tasks": {
                "blocked": {"id": "blocked", "status": "blocked", "updated_at_ms": 2},
                "queued": {"id": "queued", "status": "queued", "updated_at_ms": 1},
            },
            "inbound": [],
        },
    )
    monkeypatch.setattr(
        facade,
        "_resource",
        lambda operation, _payload: {"revision": 9, "companies": []}
        if operation == "list"
        else None,
    )

    status = facade.run("status")

    assert status["runtime"] == {
        "messages": 1,
        "tasks": 2,
        "threads": 0,
        "runs": 0,
        "inbox": 0,
        "summaries": 0,
    }
    assert status["reporting"]["blocker_signals"]["blocker_count"] == 1
    assert status["revision"] == 9


def test_company_facade_resolves_mentions_from_selected_members(monkeypatch) -> None:
    """Mention aliases are resolved from the selected Company member projection."""

    import domain.company.contract_facade as contract_facade

    monkeypatch.setattr(contract_facade, "_profile_id", lambda: "test")
    facade = contract_facade.CompanyContractFacade(
        {"company_id": "acme", "content": "@pm and @missing"}, {}
    )
    monkeypatch.setattr(
        facade,
        "_raw_company",
        lambda _company_id: {
            "members": {
                "project_manager": {
                    "id": "project_manager",
                    "role_id": "project_manager",
                    "display_name": "Project Manager",
                    "mentions": ["project_manager"],
                    "enabled": True,
                    "metadata": {},
                }
            },
            "roles": {
                "project_manager": {
                    "id": "project_manager",
                    "name": "Project Manager",
                    "work_type": "agent",
                }
            },
        },
    )

    assert facade.run("resolve_mentions") == {
        "mentions": ["pm", "missing"],
        "resolved_agents": [
            {
                "id": "project_manager",
                "agent_id": "project_manager",
                "role_key": "project_manager",
                "agent_name": "Project Manager",
                "display_name": "Project Manager",
                "model": "",
                "aliases": ["project_manager"],
                "enabled": True,
                "status": "idle",
                "work_type": "agent",
                "metadata": {},
            }
        ],
        "resolved_agent_ids": ["project_manager"],
        "unresolved": ["missing"],
    }


def test_company_runtime_collections_are_explicit_sunset_shims() -> None:
    """Wave 10 removes runtime-store fallback rather than retaining a writer."""

    from blocks.company import inbox, runs, summary, threads

    for module in (inbox, runs, summary, threads):
        assert module.run({}, {})["error"]["code"] == "COMPANY_RUNTIME_ROUTE_SUNSET"
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "CompanyRuntimeStore" not in source
        assert "CompanyStore" not in source


def test_company_list_and_status_expose_owner_revision(monkeypatch) -> None:
    """Workspace loads can bind reads and writes to the selected owner revision."""

    import domain.company.contract_facade as contract_facade

    monkeypatch.setattr(contract_facade, "_profile_id", lambda: "test")
    facade = contract_facade.CompanyContractFacade({}, {})
    monkeypatch.setattr(
        facade,
        "_resource",
        lambda operation, _payload: (
            {
                "revision": 17,
                "companies": [],
            }
            if operation == "list"
            else None
        ),
    )

    assert facade.run("list")["revision"] == 17
    assert facade.run("status")["revision"] == 17


def test_company_mutation_rejects_stale_client_revision(monkeypatch) -> None:
    """A stale UI revision fails closed before any authority-gated write."""

    import domain.company.contract_facade as contract_facade

    monkeypatch.setattr(contract_facade, "_profile_id", lambda: "test")
    facade = contract_facade.CompanyContractFacade(
        {"expected_revision": 4},
        {},
    )
    monkeypatch.setattr(
        facade,
        "_resource",
        lambda _operation, _payload: {"revision": 5, "companies": []},
    )

    with pytest.raises(contract_facade.CompanyFacadeError) as exc_info:
        facade._mutate("company.update", {"company_id": "acme", "updates": {}})

    assert exc_info.value.code == "REVISION_CONFLICT"
    assert exc_info.value.http_status == 409


def test_company_message_retry_still_uses_authority_gated_owner(monkeypatch) -> None:
    """A timeout retry remains receipt-gated instead of returning from the facade."""

    import domain.company.contract_facade as contract_facade

    monkeypatch.setattr(contract_facade, "_profile_id", lambda: "test")
    operation_id = "company-message-stable-operation"
    facade = contract_facade.CompanyContractFacade(
        {
            "company_id": "acme",
            "operation_id": operation_id,
            "content": "preserve this draft",
            "channel_id": "general",
        },
        {},
    )
    expected_id = (
        "message-"
        + contract_facade.hashlib.sha256(f"acme\0{operation_id}".encode("utf-8")).hexdigest()[:40]
    )
    monkeypatch.setattr(facade, "_raw_company", lambda _company_id: {"id": "acme"})
    calls = []
    monkeypatch.setattr(
        facade,
        "_mutate",
        lambda name, arguments: calls.append((name, arguments))
        or {
            "message": {
                "id": expected_id,
                "text": "preserve this draft",
            },
            "deduplicated": True,
        },
    )

    assert facade.run("append_message")["id"] == expected_id
    assert calls[0][0] == "message.append"
    assert calls[0][1]["record"]["metadata"]["operation_id"] == operation_id


def test_company_owner_deduplicates_operation_records_without_revision_drift(
    tmp_path,
) -> None:
    """Retrying a committed operation does not append or advance state again."""

    from ecosystem.rumi_company_state_store_pack.runtime.store import (
        CompanyStateStore,
    )

    store = CompanyStateStore("test", root=tmp_path)
    store.apply(
        "company.create",
        {
            "company_id": "acme",
            "expected_revision": 0,
            "name": "Acme",
            "description": "",
            "settings": {},
            "metadata": {},
            "conversation_group_id": "company:acme",
        },
    )
    message_arguments = {
        "company_id": "acme",
        "expected_revision": 1,
        "record": {
            "id": "message-operation-1",
            "type": "message",
            "actor_id": "user",
            "channel_id": "general",
            "text": "hello",
            "metadata": {},
        },
    }
    first = store.apply("message.append", message_arguments)
    retry = store.apply(
        "message.append",
        {**message_arguments, "expected_revision": first["revision"]},
    )

    assert retry["deduplicated"] is True
    assert retry["revision"] == first["revision"]
    assert store.snapshot()["revision"] == first["revision"]


def test_company_owner_deduplicates_task_operation_but_allows_new_updates(
    tmp_path,
) -> None:
    """Task retries reuse one operation while later operations can update the task."""

    from ecosystem.rumi_company_state_store_pack.runtime.store import (
        CompanyStateStore,
    )

    store = CompanyStateStore("test", root=tmp_path)
    store.apply(
        "company.create",
        {
            "company_id": "acme",
            "expected_revision": 0,
            "name": "Acme",
            "description": "",
            "settings": {},
            "metadata": {},
            "conversation_group_id": "company:acme",
        },
    )
    first_arguments = {
        "company_id": "acme",
        "expected_revision": 1,
        "record": {
            "id": "task-1",
            "title": "Draft-safe task",
            "status": "queued",
            "idempotency_key": "task-operation-1",
            "metadata": {"operation_id": "task-operation-1"},
        },
    }
    first = store.apply("task.upsert", first_arguments)
    retry = store.apply(
        "task.upsert",
        {**first_arguments, "expected_revision": first["revision"]},
    )
    updated = store.apply(
        "task.upsert",
        {
            **first_arguments,
            "expected_revision": retry["revision"],
            "record": {
                **first_arguments["record"],
                "title": "Updated task",
                "idempotency_key": "task-operation-2",
                "metadata": {"operation_id": "task-operation-2"},
            },
        },
    )

    assert retry["deduplicated"] is True
    assert retry["revision"] == first["revision"]
    assert updated["revision"] == first["revision"] + 1
    assert updated["task"]["title"] == "Updated task"
