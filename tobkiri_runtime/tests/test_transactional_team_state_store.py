from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from ecosystem.rumi_company_state_store_pack.runtime import store as store_module
from ecosystem.rumi_company_state_store_pack.runtime.store import (
    CompanyStateStore,
    create_company_action,
)
from ecosystem.rumi_company_state_store_pack.runtime.team_store import (
    LEGACY_VERSION,
    TeamStateConflict,
    TeamStateQuarantined,
    TransactionalTeamStore,
)


def _create(store: CompanyStateStore, team_id: str) -> None:
    store.apply(
        "company.create",
        {
            "company_id": team_id,
            "expected_revision": 0,
            "name": team_id,
            "description": "",
            "settings": {},
            "metadata": {},
            "conversation_group_id": f"company:{team_id}",
        },
    )


def _owner_root(root: Path) -> Path:
    return root / "packs" / "rumi_company_state_store_pack" / "profiles" / "default"


def test_different_teams_progress_with_independent_revisions(tmp_path: Path) -> None:
    store = CompanyStateStore("default", root=tmp_path)
    _create(store, "team-a")
    _create(store, "team-b")

    def append(team_id: str) -> int:
        local = CompanyStateStore("default", root=tmp_path)
        return int(
            local.apply(
                "message.append",
                {
                    "company_id": team_id,
                    "expected_revision": 1,
                    "record": {"id": f"message-{team_id}", "text": team_id},
                },
            )["revision"]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(append, ("team-a", "team-b"))) == [2, 2]
    assert store.get("team-a")["revision"] == 2
    assert store.get("team-b")["revision"] == 2


def test_authority_redeems_exact_normalized_team_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redeemed: list[dict[str, object]] = []

    class AuthorityClient:
        def invoke(
            self, contract: str, operation: str, payload: dict[str, object]
        ) -> dict[str, object]:
            assert contract == "rumi.service.host.authorize.v1"
            assert operation == "redeem"
            redeemed.append(payload)
            return {"authorized": True}

    monkeypatch.setattr(store_module, "USER_DATA_DIR", tmp_path)
    action = create_company_action(AuthorityClient())
    result = action(
        "company.create",
        {
            "profile_id": "default",
            "company_id": "team",
            "expected_revision": 0,
            "name": "Team",
            "description": "",
            "settings": {},
            "metadata": {},
            "conversation_group_id": "company:team",
            "authority_receipt": "receipt",
            "caller_id": "caller",
            "caller_pack_id": "caller-pack",
            "caller_function_id": "caller.function",
            "session_id": "session",
        },
    )

    assert result["revision"] == 1
    assert redeemed == [
        {
            "receipt": "receipt",
            "service_pack_id": "rumi_company_state_store_pack",
            "operation": "company.state.company.create",
            "authority": "company.state.manage",
            "caller_id": "caller",
            "caller_pack_id": "caller-pack",
            "caller_function_id": "caller.function",
            "profile_id": "default",
            "workspace_id": "",
            "session_id": "session",
            "arguments": {
                "company_id": "team",
                "expected_revision": 0,
                "name": "Team",
                "settings": {},
                "description": "",
                "metadata": {},
                "conversation_group_id": "company:team",
            },
        }
    ]


def test_same_team_compare_and_set_has_one_retryable_winner(tmp_path: Path) -> None:
    store = CompanyStateStore("default", root=tmp_path)
    _create(store, "team")

    def append(index: int) -> str:
        local = CompanyStateStore("default", root=tmp_path)
        try:
            local.apply(
                "message.append",
                {
                    "company_id": "team",
                    "expected_revision": 1,
                    "record": {"id": f"message-{index}", "text": "safe"},
                },
            )
            return "committed"
        except TeamStateConflict as exc:
            assert exc.retryable is True
            assert exc.diagnostic()["code"] == "TEAM_STATE_CONFLICT"
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(append, (1, 2))) == ["committed", "stale"]
    assert store.get("team")["counts"]["messages"] == 1


def test_entity_cas_and_default_channel_membership(tmp_path: Path) -> None:
    store = CompanyStateStore("default", root=tmp_path)
    _create(store, "team")
    store.apply(
        "role.upsert",
        {
            "company_id": "team",
            "expected_revision": 1,
            "expected_entity_revision": 0,
            "record_id": "role",
            "record": {"id": "role", "name": "Role"},
        },
    )
    store.apply(
        "channel.upsert",
        {
            "company_id": "team",
            "expected_revision": 2,
            "record_id": "general",
            "record": {"id": "general", "name": "General", "is_default": True},
        },
    )
    store.apply(
        "member.upsert",
        {
            "company_id": "team",
            "expected_revision": 3,
            "record_id": "member-b",
            "record": {"id": "member-b", "role_id": "role"},
        },
    )
    store.apply(
        "member.upsert",
        {
            "company_id": "team",
            "expected_revision": 4,
            "record_id": "member-a",
            "record": {"id": "member-a", "role_id": "role"},
        },
    )
    assert store.get("team")["channels"]["general"]["member_ids"] == [
        "member-a",
        "member-b",
    ]
    with pytest.raises(TeamStateConflict) as failure:
        store.apply(
            "role.upsert",
            {
                "company_id": "team",
                "expected_revision": 5,
                "expected_entity_revision": 0,
                "record_id": "role",
                "record": {"id": "role", "name": "stale"},
            },
        )
    assert failure.value.entity_id == "role"
    assert store.get("team")["revision"] == 5


def test_reads_are_pure_and_timeline_is_stably_paginated(tmp_path: Path) -> None:
    store = CompanyStateStore("default", root=tmp_path)
    _create(store, "team")
    for index in range(5):
        store.apply(
            "message.append",
            {
                "company_id": "team",
                "expected_revision": index + 1,
                "record": {"id": f"message-{index}", "text": str(index)},
            },
        )
    before = store.get("team")
    duplicate = store.apply(
        "message.append",
        {
            "company_id": "team",
            "expected_revision": 6,
            "record": {"id": "message-4", "text": "4"},
        },
    )
    assert duplicate["deduplicated"] is True
    assert duplicate["revision"] == 6
    first = store.list_timeline("team", limit=2)
    store.get("team")
    after = store.get("team")
    second = store.list_timeline("team", limit=2, after_sequence=int(first["next_cursor"]))
    assert before == after
    assert [row["id"] for row in first["records"]] == ["message-0", "message-1"]
    assert [row["id"] for row in second["records"]] == ["message-2", "message-3"]


def test_claim_is_atomic_idempotent_and_fence_survives_restart(tmp_path: Path) -> None:
    store = CompanyStateStore("default", root=tmp_path)
    _create(store, "team")
    store.apply(
        "role.upsert",
        {
            "company_id": "team",
            "expected_revision": 1,
            "record_id": "role",
            "record": {"id": "role"},
        },
    )
    store.apply(
        "member.upsert",
        {
            "company_id": "team",
            "expected_revision": 2,
            "record_id": "member",
            "record": {"id": "member", "role_id": "role"},
        },
    )
    store.apply(
        "task.upsert",
        {
            "company_id": "team",
            "expected_revision": 3,
            "record_id": "work",
            "record": {"id": "work", "title": "Work"},
        },
    )
    claim = store.claim_work(
        "team",
        "work",
        "member",
        expected_revision=4,
        lease_duration_ms=60_000,
        idempotency_key="claim-1",
    )
    restarted = CompanyStateStore("default", root=tmp_path)
    repeated = restarted.claim_work(
        "team",
        "work",
        "member",
        expected_revision=4,
        lease_duration_ms=60_000,
        idempotency_key="claim-1",
    )
    assert repeated == claim
    assert restarted.get("team")["execution_leases"]["work"]["fencing_token"] == 1
    renewed = restarted.renew_lease(
        "team",
        "work",
        fencing_token=1,
        expected_revision=5,
        lease_duration_ms=120_000,
    )
    assert renewed["revision"] == 6
    with pytest.raises(TeamStateConflict):
        restarted.renew_lease(
            "team",
            "work",
            fencing_token=0,
            expected_revision=6,
            lease_duration_ms=120_000,
        )


def test_json_migration_is_idempotent_private_and_crash_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner_root(tmp_path)
    owner.mkdir(parents=True)
    legacy = {
        "version": LEGACY_VERSION,
        "profile_id": "default",
        "revision": 7,
        "companies": {
            "team": {
                "id": "team",
                "name": "Team",
                "description": "",
                "status": "active",
                "settings": {},
                "metadata": {},
                "conversation_group_id": "company:team",
                "roles": {},
                "members": {},
                "channels": {},
                "tasks": {},
                "routes": {},
                "inbound": [],
                "messages": [],
                "created_at_ms": 10,
                "updated_at_ms": 20,
            }
        },
        "migrations": {},
    }
    legacy_path = owner / "companies.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    original = TransactionalTeamStore._import_legacy_team

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected crash")

    monkeypatch.setattr(TransactionalTeamStore, "_import_legacy_team", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        CompanyStateStore("default", root=tmp_path)
    assert legacy_path.exists()
    monkeypatch.setattr(TransactionalTeamStore, "_import_legacy_team", original)
    migrated = CompanyStateStore("default", root=tmp_path)
    assert migrated.get("team")["updated_at_ms"] == 20
    assert CompanyStateStore("default", root=tmp_path).get("team")["name"] == "Team"
    backups = list((owner / "migration-backups").glob("companies-*.json"))
    assert len(backups) == 1


def test_malformed_legacy_state_is_quarantined_and_recoverable(tmp_path: Path) -> None:
    owner = _owner_root(tmp_path)
    owner.mkdir(parents=True)
    legacy_path = owner / "companies.json"
    legacy_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(TeamStateQuarantined) as failure:
        CompanyStateStore("default", root=tmp_path)
    assert failure.value.quarantine_path.is_file()
    legacy_path.unlink()
    assert CompanyStateStore("default", root=tmp_path).snapshot()["companies"] == []


def test_sqlite_only_runtime_migrates_and_is_idempotent(tmp_path: Path) -> None:
    owner = _owner_root(tmp_path)
    owner.mkdir(parents=True)
    source = owner / "company_runtime.db"
    connection = sqlite3.connect(source)
    connection.execute(
        "CREATE TABLE company_messages(message_id TEXT PRIMARY KEY,company_id TEXT,"
        "channel_id TEXT,sender_id TEXT,content TEXT,metadata_json TEXT,"
        "created_at TEXT,updated_at TEXT)"
    )
    connection.execute(
        "INSERT INTO company_messages VALUES(?,?,?,?,?,?,?,?)",
        (
            "message",
            "team",
            "general",
            "member",
            "hello",
            "{}",
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    store = CompanyStateStore("default", root=tmp_path)
    assert store.get("team")["messages"][0]["text"] == "hello"
    assert store.migrate_legacy_sqlite(source)["deduplicated"] is True


def test_schema_has_all_independent_team_entities(tmp_path: Path) -> None:
    store = CompanyStateStore("default", root=tmp_path)
    assert store.lookup("team")["state"] == "missing"
    assert store.lookup("team", authorized=False)["state"] == "unauthorized"
    with closing(store.connection()) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "teams",
        "members",
        "departments",
        "member_pools",
        "channels",
        "team_conversation_bindings",
        "work_items",
        "assignments",
        "execution_attempts",
        "execution_leases",
        "inbound_receipts",
        "timeline_events",
        "inbox_items",
        "connections",
        "inbound_routes",
        "summaries",
        "migration_records",
        "idempotency_records",
    } <= tables
