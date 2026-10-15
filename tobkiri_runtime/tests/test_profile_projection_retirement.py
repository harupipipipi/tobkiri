"""Retirement contracts for declarative Packs moved into Profile projections."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

from core_runtime.profile_content_projection import (
    ProfileContentProjectionError,
    resolve_intent_projection,
    selected_projection_roots,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from core_runtime.profile_projection_migration import (
    MIGRATION_ID,
    RETIREMENTS,
    migrate_pack_control_envelope,
    migrate_profile_document,
    rollback_pack_control_envelope,
)
from core_runtime.resolved_profile_scope import (
    V4ResolvedProfileView,
    activate_resolved_profile,
    restore_resolved_profile,
)
from tobkiri_protocol.canonical import canonical_digest


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
RETIRED_IDS = {item.legacy_pack_id for item in RETIREMENTS}


def _view(*projections: dict[str, object]) -> V4ResolvedProfileView:
    return V4ResolvedProfileView(
        profile_id="profile-a",
        profile_revision="sha256:" + "1" * 64,
        plan_hash="sha256:" + "2" * 64,
        effective_pack_set=(),
        packs=(),
        providers=(),
        projections=projections,
    )


def test_retired_ids_are_aliases_not_pack_authorities() -> None:
    catalog = json.loads((ROOT / "schemas" / "pack_v4_catalog.v1.json").read_text())
    authority = json.loads(
        (ROOT / "schemas" / "manifest_authority.v1.json").read_text()
    )
    assert set(catalog["excluded_packs"]) == RETIRED_IDS
    assert RETIRED_IDS.isdisjoint(catalog["pack_ids"])
    assert RETIRED_IDS.isdisjoint(authority["packs"])
    for retirement in RETIREMENTS:
        alias_root = ROOT / "ecosystem" / retirement.legacy_pack_id
        alias = json.loads(
            (alias_root / "compatibility-alias.v1.json").read_text()
        )
        assert alias["runtime_authority"] is False
        assert alias["read_only"] is True
        assert alias["projection_id"] == retirement.projection_id
        assert not (alias_root / "pack.v4.json").exists()


def test_profile_store_migration_is_atomic_revisioned_and_idempotent(
    tmp_path: Path,
) -> None:
    store = ProfileDefinitionStore(tmp_path)
    original = {
        "profile_id": "named-a",
        "display_name": "Named A",
        "packs": [
            {"pack_id": "defaultspack", "role": "application"},
            {"pack_id": "rumi_local_agent_pack", "role": "provider"},
            {"pack_id": "rumi_reference_ui_pack", "role": "provider"},
        ],
    }
    created = store.create_profile(original)
    assert store.migrate_retired_pack_projections() == 1
    migrated = store.get_profile("named-a")
    assert migrated is not None
    assert migrated.parent_revision == created.profile_revision
    assert {item["pack_id"] for item in migrated.profile["packs"]} == {
        "defaultspack"
    }
    assert {
        item["source_legacy_pack_id"]
        for item in migrated.profile["content_projections"]
    } == {"rumi_local_agent_pack", "rumi_reference_ui_pack"}
    snapshot = store.snapshot()
    ledger = snapshot["legacy"]["projection_migrations"][MIGRATION_ID]
    receipt = ledger["receipts"][0]
    assert receipt["profile_id"] == "named-a"
    assert receipt["profile_revision"] == migrated.profile_revision
    assert receipt["migrated_count"] == 2
    assert receipt["pre_state_digest"] == created.profile_revision
    assert receipt["post_state_digest"] == migrated.profile_revision
    assert store.migrate_retired_pack_projections() == 0
    assert store.snapshot() == snapshot


def test_pack_control_receipt_survives_restart_and_supports_rollback() -> None:
    original = {
        "version": "io.tobkiri.pack-control-state.v4",
        "profile_id": "named-a",
        "installed": {
            "rumi_local_agent_pack": {"artifact_digest": "sha256:" + "3" * 64},
            "defaultspack": {"artifact_digest": "sha256:" + "4" * 64},
        },
    }
    migrated, receipt = migrate_pack_control_envelope(
        original,
        profile_id="named-a",
        profile_revision="sha256:" + "5" * 64,
        enabled_pack_ids={"rumi_local_agent_pack"},
        approval_digests={"rumi_local_agent_pack": "sha256:" + "6" * 64},
    )
    assert receipt is not None
    assert receipt["migrated_count"] == 1
    assert receipt["approved_count"] == 1
    assert receipt["enabled_count"] == 1
    assert "rumi_local_agent_pack" not in migrated["installed"]
    restarted = json.loads(json.dumps(migrated))
    second, second_receipt = migrate_pack_control_envelope(
        restarted,
        profile_id="named-a",
        profile_revision="sha256:" + "5" * 64,
        enabled_pack_ids=set(),
        approval_digests={},
    )
    assert second == restarted
    assert second_receipt == receipt
    rolled_back = rollback_pack_control_envelope(restarted)
    assert rolled_back["installed"] == original["installed"]


def test_profile_projection_selection_does_not_mix_named_profiles() -> None:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    from domain.capability.catalog import CapabilityCatalog

    local = next(
        item.resolved()
        for item in RETIREMENTS
        if item.legacy_pack_id == "rumi_local_agent_pack"
    )
    services = next(
        item.resolved()
        for item in RETIREMENTS
        if item.legacy_pack_id == "rumi_agent_services_pack"
    )
    token = activate_resolved_profile(_view(local))
    try:
        local_ids = {item["profile_id"] for item in CapabilityCatalog().profiles()}
    finally:
        restore_resolved_profile(token)
    token = activate_resolved_profile(_view(services))
    try:
        service_ids = {
            item["profile_id"] for item in CapabilityCatalog().profiles()
        }
    finally:
        restore_resolved_profile(token)
    assert "defaultspack.local_agent" in local_ids
    assert "rumi_agent_services.service_director" not in local_ids
    assert "rumi_agent_services.service_director" in service_ids
    assert "defaultspack.local_agent" not in service_ids


def test_projection_digest_and_unique_root_fail_closed() -> None:
    local = next(
        item.resolved()
        for item in RETIREMENTS
        if item.legacy_pack_id == "rumi_local_agent_pack"
    )
    stale = copy.deepcopy(local)
    stale["content_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ProfileContentProjectionError, match="stale"):
        selected_projection_roots([stale])
    alias = {**local, "projection_id": "duplicate-root"}
    with pytest.raises(ProfileContentProjectionError, match="same artifact root"):
        selected_projection_roots([local, alias])
    assert canonical_digest(
        {"effective_set": [], "content_projections": [local]}
    ) != canonical_digest(
        {"effective_set": [], "content_projections": [stale]}
    )


def test_projection_rejects_intermediate_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core_runtime.profile_content_projection as projection_module

    runtime_root = tmp_path / "runtime"
    projection_root = runtime_root / "profile_projections"
    real = tmp_path / "real"
    real.mkdir()
    (real / "prompt.md").write_text("safe", encoding="utf-8")
    projection_root.mkdir(parents=True)
    (projection_root / "linked").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(projection_module, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(projection_module, "PROJECTION_ROOT", projection_root)
    with pytest.raises(ProfileContentProjectionError, match="symlink"):
        resolve_intent_projection(
            {
                "projection_id": "linked",
                "kind": "profile_content",
                "artifact_root": "profile_projections/linked",
                "content_digest": None,
            }
        )


def test_migrate_profile_document_preserves_unrelated_pack_roles() -> None:
    source = {
        "packs": [
            {"pack_id": "defaultspack", "role": "application"},
            {"pack_id": "rumi_agent_services_pack", "role": "provider"},
        ]
    }
    migrated, legacy_ids = migrate_profile_document(source)
    assert legacy_ids == ("rumi_agent_services_pack",)
    assert migrated["packs"] == source["packs"][:1]
    assert source.get("content_projections") is None
