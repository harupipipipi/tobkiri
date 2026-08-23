from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from ecosystem.rumi_team_state_store_pack.runtime.store import (
    CompanyStateStore,
    TeamStateStore,
    create_company_action,
)
from scripts.quality.check_team_vocabulary import check as check_team_vocabulary


ROOT = Path(__file__).resolve().parent.parent
CANONICAL_PACKS = {
    "rumi_team_state_store_pack": "rumi_company_state_store_pack",
    "rumi_team_coordinator_pack": "rumi_company_coordinator_pack",
    "rumi_team_agent_work_adapter_pack": "rumi_company_agent_adapter_pack",
    "rumi_team_console_pack": "rumi_company_surface_pack",
    "rumi_connector_team_adapter_pack": "rumi_connector_company_adapter_pack",
    "rumi_operations_team_pack": "rumi_operations_company_pack",
    "rumi_run_lifecycle_pack": "rumi_agent_workroom_pack",
}


def _legacy_path(root: Path) -> Path:
    return (
        root
        / "packs"
        / "rumi_company_state_store_pack"
        / "profiles"
        / "default"
        / "companies.json"
    )


def _write_legacy(root: Path, *, payload: dict | None = None) -> tuple[Path, bytes]:
    path = _legacy_path(root)
    path.parent.mkdir(parents=True)
    value = payload or {
        "version": "rumi.company-state.v1",
        "profile_id": "default",
        "revision": 7,
        "companies": {
            "alpha": {
                "id": "alpha",
                "name": "Alpha",
                "description": "",
                "status": "active",
                "settings": {},
                "metadata": {"legacy": True},
                "conversation_group_id": "team:alpha",
                "roles": {},
                "members": {},
                "channels": {},
                "tasks": {},
                "routes": {},
                "inbound": [],
                "messages": [],
                "created_at_ms": 1,
                "updated_at_ms": 1,
            }
        },
    }
    encoded = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(encoded)
    return path, encoded


def test_catalog_exposes_only_canonical_team_pack_identities() -> None:
    catalog = json.loads(
        (ROOT / "schemas" / "pack_v4_catalog.v1.json").read_text(encoding="utf-8")
    )
    records = {record["pack_id"]: record for record in catalog["packs"]}
    for canonical, legacy in CANONICAL_PACKS.items():
        assert canonical in records
        assert legacy not in records
        assert canonical not in records[canonical]["legacy_ids"]
        assert legacy in records[canonical]["legacy_ids"]

    contracts = {
        contract["contract_id"]
        for pack_id in CANONICAL_PACKS
        for contract in records[pack_id].get("provided_contracts", [])
    }
    assert {
        "tobkiri.resource.team.v1",
        "tobkiri.action.team.state.v1",
        "tobkiri.action.team.coordinate.v1",
        "tobkiri.resource.team.runtime.v1",
        "tobkiri.action.team.work.v1",
    } <= contracts


def test_canonical_catalog_passes_team_vocabulary_guard() -> None:
    assert check_team_vocabulary(ROOT) == []


def test_legacy_state_migration_is_digest_bound_idempotent_and_rollback_safe(
    tmp_path: Path,
) -> None:
    legacy_path, source = _write_legacy(tmp_path)
    store = TeamStateStore("default", root=tmp_path)

    snapshot = store.snapshot()
    assert snapshot["revision"] == 7
    assert [team["id"] for team in snapshot["teams"]] == ["alpha"]
    canonical_path = store.path
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    evidence = canonical["migrations"]["company-to-team.v1"]
    assert evidence["source_digest"] == "sha256:" + hashlib.sha256(source).hexdigest()
    assert evidence["canonical_team_ids"] == ["alpha"]
    assert evidence["activation"] == "committed"
    assert evidence["unresolved_conflicts"] == []
    assert legacy_path.read_bytes() == source

    first = canonical_path.read_bytes()
    TeamStateStore("default", root=tmp_path)
    assert canonical_path.read_bytes() == first
    assert legacy_path.read_bytes() == source


def test_failed_legacy_state_migration_does_not_activate_team_state(
    tmp_path: Path,
) -> None:
    path = _legacy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt"):
        TeamStateStore("default", root=tmp_path)

    canonical = (
        tmp_path
        / "packs"
        / "rumi_team_state_store_pack"
        / "profiles"
        / "default"
        / "teams.json"
    )
    assert not canonical.exists()
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_company_facade_reads_only_canonical_state_and_emits_bounded_telemetry(
    tmp_path: Path,
) -> None:
    legacy_path, _ = _write_legacy(tmp_path)
    facade = CompanyStateStore("default", root=tmp_path)

    assert facade.snapshot()["companies"][0]["id"] == "alpha"
    assert facade.get("alpha")["id"] == "alpha"
    assert legacy_path.is_file()
    telemetry = json.loads(
        (facade.root / "compatibility_usage.v1.json").read_text(encoding="utf-8")
    )
    assert telemetry["sunset_at"] == "2027-12-31"
    assert set(telemetry["aliases"]) == {
        "CompanyStateStore.snapshot",
        "CompanyStateStore.get",
    }
    assert len(telemetry["aliases"]) <= 64


def test_legacy_write_cannot_bypass_canonical_team_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict]] = []

    class DenyingClient:
        def invoke(self, contract_id: str, operation: str, payload: dict) -> dict:
            calls.append((contract_id, operation, payload))
            return {"authorized": False, "reason": "denied by test"}

    monkeypatch.setattr(
        "ecosystem.rumi_team_state_store_pack.runtime.store.USER_DATA_DIR",
        tmp_path,
    )
    action = create_company_action(DenyingClient())
    with pytest.raises(PermissionError, match="denied by test"):
        action(
            "company.create",
            {
                "profile_id": "default",
                "company_id": "blocked",
                "name": "Blocked",
                "expected_revision": 0,
                "authority_receipt": "legacy-receipt",
            },
        )

    assert calls[0][0:2] == ("rumi.service.host.authorize.v1", "redeem")
    redeemed = calls[0][2]
    assert redeemed["service_pack_id"] == "rumi_team_state_store_pack"
    assert redeemed["operation"] == "team.state.team.create"
    assert redeemed["authority"] == "team.state.manage"
    assert redeemed["arguments"]["team_id"] == "blocked"
    assert not (
        tmp_path
        / "packs"
        / "rumi_team_state_store_pack"
        / "profiles"
        / "default"
        / "teams.json"
    ).exists()


@pytest.mark.parametrize(
    ("legacy_module", "symbol"),
    [
        ("rumi_company_state_store_pack.runtime.store", "CompanyStateStore"),
        ("rumi_company_coordinator_pack.runtime.coordinator", "CompanyCoordinator"),
        (
            "rumi_company_agent_adapter_pack.runtime.adapter",
            "CompanyAgentAdapter",
        ),
        (
            "rumi_connector_company_adapter_pack.runtime.adapter",
            "ConnectorCompanyAdapter",
        ),
    ],
)
def test_legacy_python_imports_are_compatibility_shims(
    legacy_module: str,
    symbol: str,
) -> None:
    module = importlib.import_module(f"ecosystem.{legacy_module}")
    assert getattr(module, symbol).__module__.startswith("ecosystem.rumi_")
