from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from core_runtime.global_contracts.models import ContractStatus
from core_runtime.resolved_profile import (
    ResolutionInput,
    apply_legacy_selection_migration,
    create_lockfile,
    plan_legacy_selection_migration,
    resolution_input_from_startup_profile,
    resolve_profile,
    read_lockfile,
    refresh_lockfile,
    rollback_legacy_selection_migration,
    validate_lockfile,
)
from core_runtime.resolved_profile_scope import (
    _persisted_startup_pack_ids,
    activate_resolved_profile,
    effective_pack_ids,
    persisted_resolved_profile,
    require_effective_pack,
    restore_resolved_profile,
)


def test_persisted_scope_reads_the_configured_user_data_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker fallback must not accidentally read the bundled app's data."""
    import core_runtime.resolved_profile_scope as scope

    observed: dict[str, str] = {}

    class FakeActiveEcosystemManager:
        def __init__(self, *, config_path: str) -> None:
            observed["config_path"] = config_path

        def get_metadata(self, key: str, default: object) -> list[str]:
            assert key == "startup_packs"
            assert default == []
            return ["defaultspack", "rumi_browser_automation_pack"]

    monkeypatch.setattr(scope, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(scope, "ActiveEcosystemManager", FakeActiveEcosystemManager)

    assert _persisted_startup_pack_ids() == [
        "defaultspack",
        "rumi_browser_automation_pack",
    ]
    assert observed["config_path"] == str(tmp_path / "active_ecosystem.json")


def test_startup_profile_input_accepts_only_host_supplied_verified_trust() -> None:
    resolution_input = resolution_input_from_startup_profile(
        {
            "profile_id": "fixture",
            "base_pack": "defaultspack",
            "packs": ["frontendpack"],
        },
        verified_pack_trust={
            "frontendpack": "verified",
            "defaultspack": "system",
        },
    )

    assert resolution_input.verified_pack_trust == (
        ("defaultspack", "system"),
        ("frontendpack", "verified"),
    )


def test_persisted_profile_restores_verified_system_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import core_runtime.approval_manager as approval_module
    import core_runtime.resolved_profile_scope as scope

    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "startup_profiles.json").write_text(
        json.dumps(
            {
                "active_profile_id": "fixture",
                "profiles": [
                    {
                        "profile_id": "fixture",
                        "base_pack": "defaultspack",
                        "packs": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeApprovalManager:
        def get_verified_pack_trust(
            self, pack_ids: tuple[str, ...]
        ) -> dict[str, str]:
            return {pack_id: "system" for pack_id in pack_ids}

    monkeypatch.setattr(scope, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(scope, "_PERSISTED_PROFILE_CACHE", None)
    monkeypatch.setattr(
        approval_module,
        "get_approval_manager",
        lambda: FakeApprovalManager(),
    )

    plan = persisted_resolved_profile()

    assert plan is not None
    defaultspack = next(
        pack for pack in plan.packs if pack.pack_id == "defaultspack"
    )
    assert defaultspack.authorized is True
    assert defaultspack.trust_class == "system"


def _write_pack(
    root: Path,
    pack_id: str,
    *,
    dependencies: list[str] | None = None,
    components: dict[str, dict[str, object]] | None = None,
    required_capabilities: list[str] | None = None,
) -> Path:
    pack = root / pack_id
    pack.mkdir(parents=True)
    manifest = {
        "pack_id": pack_id,
        "version": "1.0.0",
        "dependencies": dependencies or [],
        "components": components or {},
        "required_capabilities": required_capabilities or [],
    }
    (pack / "ecosystem.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return pack


def _input(
    *pack_ids: str,
    authorized: tuple[str, ...] | None = None,
) -> ResolutionInput:
    return ResolutionInput(
        profile_id="fixture",
        profile_revision="profile-r1",
        platform="fixture-os",
        policy_revision="policy-r1",
        lockfile_revision=None,
        requested_pack_ids=tuple(pack_ids),
        authorized_pack_ids=authorized or (),
        policy_capabilities=("filesystem.read",),
    )


def test_resolution_is_deterministic_immutable_and_dependency_complete(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_pack(
        ecosystem,
        "pack-a",
        dependencies=["pack-b"],
        components={"tool-a": {"id": "tool-a", "type": "tool"}},
    )
    _write_pack(
        ecosystem,
        "pack-b",
        components={"ui-b": {"id": "ui-b", "type": "frontend"}},
    )
    _write_pack(ecosystem, "unrelated-pack")
    resolution_input = _input(
        "pack-a", authorized=("pack-a", "pack-b")
    )

    first = resolve_profile(resolution_input, ecosystem_dir=ecosystem)
    second = resolve_profile(resolution_input, ecosystem_dir=ecosystem)

    assert first.plan_hash == second.plan_hash
    assert first.effective_pack_set == ("pack-a", "pack-b")
    assert "unrelated-pack" not in first.effective_pack_set
    assert {item.resource_id for item in first.projections} == {
        "tool-a",
        "ui-b",
    }
    with pytest.raises(FrozenInstanceError):
        first.profile_id = "changed"  # type: ignore[misc]


def test_selection_is_not_an_authority_grant(tmp_path: Path) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_pack(ecosystem, "pack-a")

    plan = resolve_profile(_input("pack-a"), ecosystem_dir=ecosystem)

    assert plan.selected_pack_ids == ("pack-a",)
    assert plan.authorized_pack_ids == ()
    assert plan.effective_pack_set == ()
    assert any(item.code == "pack_not_authorized" for item in plan.diagnostics)


def test_runtime_scope_is_bound_to_one_plan_revision(tmp_path: Path) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_pack(ecosystem, "pack-a")
    _write_pack(ecosystem, "pack-b")
    plan = resolve_profile(
        _input("pack-a", authorized=("pack-a",)), ecosystem_dir=ecosystem
    )

    token = activate_resolved_profile(plan)
    try:
        assert effective_pack_ids() == {"pack-a"}
        require_effective_pack("pack-a")
        with pytest.raises(PermissionError):
            require_effective_pack("pack-b")
    finally:
        restore_resolved_profile(token)


def test_pack_removal_removes_every_projection(tmp_path: Path) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_pack(
        ecosystem,
        "pack-a",
        components={
            "tool-a": {"id": "tool-a", "type": "tool"},
            "prompt-a": {"id": "prompt-a", "type": "prompt"},
            "route-a": {"id": "route-a", "type": "route"},
            "provider-a": {"id": "provider-a", "type": "provider"},
            "service-a": {"id": "service-a", "type": "service"},
        },
    )
    with_pack = resolve_profile(
        _input("pack-a", authorized=("pack-a",)), ecosystem_dir=ecosystem
    )
    without_pack = resolve_profile(_input(), ecosystem_dir=ecosystem)

    assert {item.kind for item in with_pack.projections} == {
        "tools",
        "prompts",
        "routes",
        "providers",
        "services",
    }
    assert without_pack.projections == ()


def test_effective_permissions_are_policy_intersection(tmp_path: Path) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_pack(
        ecosystem,
        "pack-a",
        required_capabilities=["filesystem.read", "terminal.execute"],
    )

    plan = resolve_profile(
        _input("pack-a", authorized=("pack-a",)), ecosystem_dir=ecosystem
    )

    assert plan.effective_permissions == ("filesystem.read",)


def test_lockfile_detects_pack_content_and_profile_revision_changes(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    pack = _write_pack(
        ecosystem,
        "pack-a",
        components={"tool-a": {"id": "tool-a", "type": "tool"}},
    )
    (pack / "tools").mkdir()
    (pack / "tools" / "tool.json").write_text("{}", encoding="utf-8")
    resolution_input = _input("pack-a", authorized=("pack-a",))
    original = resolve_profile(resolution_input, ecosystem_dir=ecosystem)
    lockfile = create_lockfile(original)

    assert validate_lockfile(lockfile, original).status is ContractStatus.OK

    (pack / "tools" / "tool.json").write_text(
        '{"changed": true}', encoding="utf-8"
    )
    changed = resolve_profile(resolution_input, ecosystem_dir=ecosystem)
    assert (
        validate_lockfile(lockfile, changed).status
        is ContractStatus.STALE_RESOLUTION
    )

    revised = replace(changed, profile_revision="profile-r2")
    assert (
        validate_lockfile(lockfile, revised).status
        is ContractStatus.STALE_RESOLUTION
    )

    lock_path = tmp_path / "profile.lock.json"
    refreshed = refresh_lockfile(lock_path, changed)
    assert read_lockfile(lock_path) == refreshed

    tampered = json.loads(lock_path.read_text(encoding="utf-8"))
    tampered["profile_revision"] = "tampered"
    lock_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_lockfile(lock_path)


def test_legacy_selection_migration_has_dry_run_backup_and_rollback(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "profile.json"
    selection_path = tmp_path / "setup_pack_selection.json"
    original = {"profile_id": "fixture", "packs": ["pack-a"], "user_edit": 7}
    profile_path.write_text(json.dumps(original), encoding="utf-8")
    selection_path.write_text(
        json.dumps({"setup_pack_ids": ["pack-b"]}), encoding="utf-8"
    )

    dry_run = plan_legacy_selection_migration(
        original, {"setup_pack_ids": ["pack-b"]}
    )
    assert dry_run.after_pack_ids == ("pack-a", "pack-b")
    assert json.loads(profile_path.read_text(encoding="utf-8")) == original

    applied = apply_legacy_selection_migration(
        profile_path, selection_path, backup_dir=tmp_path / "backups"
    )
    migrated = json.loads(profile_path.read_text(encoding="utf-8"))
    assert migrated["user_edit"] == 7
    assert migrated["packs"] == ["pack-a", "pack-b"]
    assert applied.backup_path is not None

    rollback_legacy_selection_migration(
        profile_path, Path(applied.backup_path)
    )
    assert json.loads(profile_path.read_text(encoding="utf-8")) == original
