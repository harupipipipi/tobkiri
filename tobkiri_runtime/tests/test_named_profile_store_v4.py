from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from core_runtime.active_profile_store_v4 import (
    ActiveProfileStore,
    ActiveProfileStoreConflict,
    ActiveProfileStoreIntegrityError,
)
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    activation_audit_receipt,
    capture_active_profile,
    capture_default_profile,
    capture_profile,
    host_profile_catalog,
    prepare_default_profile_confirmation,
    prepare_profile_confirmation,
    repair_legacy_active_profile_pointer,
    runtime_user_data_root,
)
from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreError,
    ProfileDefinitionStoreIntegrityError,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    RuntimeSurfaceError,
    RuntimeSurfaceErrorCode,
    RuntimeSurfaceService,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.validation import validate_document


def _definition(profile_id: str, name: str) -> dict[str, object]:
    return {
        "profile_api_version": "io.tobkiri.profile.v5",
        "profile_id": profile_id,
        "display_name": name,
        "mode": "interactive",
        "packs": [{"pack_id": "example-pack", "artifact_digest": None}],
    }


def _legacy_runtime_collection() -> dict[str, object]:
    """Return the localized v3 shape observed on a macOS CI E2E disk."""

    profiles = []
    for index, (profile_id, name) in enumerate(
        (
            ("default-profile", "Default Profile"),
            ("new-custom-profile", "New custom profile"),
            ("new-custom-profile-2", "New custom profile"),
        )
    ):
        profiles.append(
            {
                "profile_id": profile_id,
                "name": name,
                "display_name": {"en": name, "ja": name},
                "locale": "ja",
                "kind": "runtime_profile",
                "base_pack": "defaultspack",
                "packs": ["defaultspack"],
                "last_runtime_profile_key": (
                    "runtime_profile.defaultspack.startup.defaultspack.startup"
                    if index == 0
                    else None
                ),
                "created_at": 1700000000 + index,
                "updated_at": 1700000100 + index,
                "metadata": {"fixture": "macos-ci-e2e"},
            }
        )
    return {
        "version": 3,
        "schema_version": None,
        "active_profile_id": "default-profile",
        "last_launched_profile_id": "default-profile",
        "profiles": profiles,
    }


def _write_activation(
    root: Path,
    profile_id: str,
    marker: str,
) -> tuple[dict[str, object], dict[str, object], str]:
    profile_revision = canonical_digest({"profile": profile_id, "marker": marker})
    plan_digest = canonical_digest({"plan": profile_id, "marker": marker})
    lock_digest = canonical_digest({"lock": profile_id, "marker": marker})
    activation_id = f"activation:{profile_id}-{marker}"
    snapshot = {
        "profile": {"profile_id": profile_id},
        "lock": {"lock_digest": lock_digest},
        "plan": {
            "profile_revision": profile_revision,
            "plan_digest": plan_digest,
        },
        "activation": {
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "activation_id": activation_id,
            "plan_digest": plan_digest,
            "lock_digest": lock_digest,
            "state": "active",
        },
    }
    relative = (
        Path("workspaces")
        / profile_id
        / "activation"
        / "activations"
        / f"{activation_id.removeprefix('activation:')}.json"
    )
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(snapshot) + b"\n")
    return dict(snapshot["activation"]), snapshot, relative.as_posix()


def test_named_profile_crud_creates_immutable_successors_and_tombstones(
    tmp_path: Path,
) -> None:
    store = ProfileDefinitionStore(tmp_path, clock=lambda: 100)
    defaults = store.bootstrap_defaults(_definition("defaults", "Defaults"))
    profile_a = store.create_profile(_definition("profile-a", "Profile A"))
    updated = store.update_profile(
        "profile-a",
        patch={"display_name": "Profile A edited"},
        expected_profile_revision=profile_a.profile_revision,
    )
    duplicate = store.duplicate_profile(
        "profile-a",
        new_profile_id="profile-b",
        expected_profile_revision=updated.profile_revision,
    )
    deleted = store.delete_profile(
        "profile-b",
        expected_profile_revision=duplicate.profile_revision,
    )

    assert defaults.profile_id == "defaults"
    assert updated.parent_revision == profile_a.profile_revision
    assert updated.profile_revision != profile_a.profile_revision
    assert duplicate.profile_id == "profile-b"
    assert deleted.tombstone is True
    assert store.get_profile("profile-b") is None
    assert store.get_profile("profile-b", include_tombstone=True) == deleted
    revisions = store.snapshot()["profiles"][1]["revisions"]
    assert [item["profile_revision"] for item in revisions] == [
        profile_a.profile_revision,
        updated.profile_revision,
    ]


def test_profile_store_migrates_legacy_v1_bootstrap_metadata_without_identity_guessing(
    tmp_path: Path,
) -> None:
    store = ProfileDefinitionStore(tmp_path, clock=lambda: 100)
    store.create_profile(_definition("named-profile", "Named Profile"))
    document = strict_loads(store.path.read_bytes())
    document.pop("bootstrap")
    document["store_digest"] = canonical_digest(
        {key: value for key, value in document.items() if key != "store_digest"}
    )
    store.path.write_bytes(canonical_json(document) + b"\n")

    assert ProfileDefinitionStore(tmp_path).bootstrap_state() == {
        "state": "not_required",
        "template_profile_revision": None,
    }


def test_profile_store_rejects_rehashed_tampered_bootstrap_revision(
    tmp_path: Path,
) -> None:
    store = ProfileDefinitionStore(tmp_path, clock=lambda: 100)
    store.bootstrap_defaults(_definition("template", "Template"))
    document = strict_loads(store.path.read_bytes())
    document["bootstrap"]["template_profile_revision"] = canonical_digest(
        {"tampered": True}
    )
    document["store_digest"] = canonical_digest(
        {key: value for key, value in document.items() if key != "store_digest"}
    )
    store.path.write_bytes(canonical_json(document) + b"\n")

    with pytest.raises(ProfileDefinitionStoreIntegrityError):
        ProfileDefinitionStore(tmp_path).snapshot()


def test_host_profile_control_routes_match_inventory_and_reject_other_ops() -> None:
    from ecosystem.defaultspack.defaultspack.runtime_surface_targets import (
        HOST_PROFILE_CONTROL_OPERATIONS,
        host_profile_control_bindings,
    )
    from core_runtime.pack_control_v4 import (
        CONTROL_PRESENTATION_CONTRACT,
        HostProfileControlSession,
        PackControlUnapproved,
    )

    operations = {
        target.operation_id
        for binding in host_profile_control_bindings()
        for target in binding.targets
    }
    assert operations == HOST_PROFILE_CONTROL_OPERATIONS
    assert HostProfileControlSession._OPERATIONS == HOST_PROFILE_CONTROL_OPERATIONS
    session = HostProfileControlSession.__new__(HostProfileControlSession)
    with pytest.raises(PackControlUnapproved):
        session.assert_operation_ready(CONTROL_PRESENTATION_CONTRACT, "profile.read")
    with pytest.raises(PackControlUnapproved):
        session.assert_operation_ready("tobkiri.host.pack-control.v4", "catalog.read")


def test_active_pointer_switch_restart_cas_and_workspace_isolation(
    tmp_path: Path,
) -> None:
    pointer_store = ActiveProfileStore(tmp_path, clock=lambda: 200)
    committed = None
    snapshots: dict[str, tuple[dict[str, object], dict[str, object], str]] = {}
    for profile_id in ("defaults", "profile-a", "profile-b", "profile-a"):
        marker = f"run-{len(snapshots)}-{profile_id}"
        activation, snapshot, relative = _write_activation(
            tmp_path,
            profile_id,
            marker,
        )
        snapshots[profile_id] = (activation, snapshot, relative)
        committed = pointer_store.commit_activation(
            activation,
            activation_snapshot=snapshot,
            activation_snapshot_path=relative,
            expected=committed,
        )
        workspace = tmp_path / "workspaces" / profile_id
        for relative_state in (
            "packs/closure.json",
            "conversation/history.json",
            "settings/runtime.json",
            "credentials/provider.ref",
            "handoff/shell.json",
        ):
            state_path = workspace / relative_state
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(marker, encoding="utf-8")

    restarted = ActiveProfileStore(tmp_path).require(verify_snapshot=True)
    assert restarted.profile_id == "profile-a"
    assert restarted.generation == 4
    for profile_id in ("defaults", "profile-a", "profile-b"):
        expected_marker = snapshots[profile_id][0]["activation_id"].removeprefix(
            f"activation:{profile_id}-"
        )
        workspace = tmp_path / "workspaces" / profile_id
        values = {
            (workspace / relative_state).read_text(encoding="utf-8")
            for relative_state in (
                "packs/closure.json",
                "conversation/history.json",
                "settings/runtime.json",
                "credentials/provider.ref",
                "handoff/shell.json",
            )
        }
        assert values == {expected_marker}

    defaults_workspace = tmp_path / "workspaces" / "defaults"
    shutil.rmtree(defaults_workspace)
    assert ActiveProfileStore(tmp_path).require().profile_id == "profile-a"

    activation_b, snapshot_b, relative_b = snapshots["profile-b"]
    with pytest.raises(ActiveProfileStoreConflict):
        pointer_store.commit_activation(
            activation_b,
            activation_snapshot=snapshot_b,
            activation_snapshot_path=relative_b,
            expected=None,
        )


def test_active_pointer_rejects_memory_disk_mismatch(tmp_path: Path) -> None:
    store = ActiveProfileStore(tmp_path)
    activation, snapshot, relative = _write_activation(
        tmp_path,
        "profile-a",
        "run-tamper",
    )
    tampered = {**snapshot, "profile": {"profile_id": "profile-b"}}
    with pytest.raises(ActiveProfileStoreIntegrityError):
        store.commit_activation(
            activation,
            activation_snapshot=tampered,
            activation_snapshot_path=relative,
        )


@pytest.mark.parametrize(
    ("case", "snapshot_path"),
    (
        (
            "cross-profile",
            "workspaces/profile-b/activation/activations/profile-a-path.json",
        ),
        (
            "invalid-filename",
            "workspaces/profile-a/activation/activations/not-the-activation.json",
        ),
    ),
)
def test_active_pointer_rejects_unbound_snapshot_path_on_commit(
    tmp_path: Path,
    case: str,
    snapshot_path: str,
) -> None:
    """Commit cannot point Profile A at another workspace or activation file."""

    root = tmp_path / case
    store = ActiveProfileStore(root)
    activation, snapshot, _canonical_path = _write_activation(
        root,
        "profile-a",
        "path",
    )
    path = root / snapshot_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(snapshot) + b"\n")

    with pytest.raises(ActiveProfileStoreIntegrityError, match="snapshot path"):
        store.commit_activation(
            activation,
            activation_snapshot=snapshot,
            activation_snapshot_path=snapshot_path,
        )
    assert store.load() is None


@pytest.mark.parametrize(
    ("case", "snapshot_path"),
    (
        (
            "cross-profile",
            "workspaces/profile-b/activation/activations/profile-a-reload.json",
        ),
        (
            "invalid-filename",
            "workspaces/profile-a/activation/activations/not-the-activation.json",
        ),
    ),
)
def test_active_pointer_rejects_unbound_snapshot_path_on_reload(
    tmp_path: Path,
    case: str,
    snapshot_path: str,
) -> None:
    """Reload cannot follow a persisted pointer outside its bound activation."""

    root = tmp_path / case
    store = ActiveProfileStore(root)
    activation, snapshot, canonical_path = _write_activation(
        root,
        "profile-a",
        "reload",
    )
    store.commit_activation(
        activation,
        activation_snapshot=snapshot,
        activation_snapshot_path=canonical_path,
    )

    path = root / snapshot_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(snapshot) + b"\n")
    pointer = dict(store.load().to_dict())
    pointer["activation_snapshot_path"] = snapshot_path
    pointer["pointer_digest"] = canonical_digest(
        {key: value for key, value in pointer.items() if key != "pointer_digest"}
    )
    store.path.write_bytes(canonical_json(pointer) + b"\n")

    with pytest.raises(ActiveProfileStoreIntegrityError, match="snapshot path"):
        store.load(verify_snapshot=False)
    with pytest.raises(ActiveProfileStoreIntegrityError, match="snapshot path"):
        store.require(verify_snapshot=True)


def test_legacy_collection_preserves_order_selection_timestamps_and_workspaces(
    tmp_path: Path,
) -> None:
    store = ProfileDefinitionStore(tmp_path, clock=lambda: 999)
    store.bootstrap_defaults(_definition("defaults", "Defaults"))
    legacy_root = tmp_path / "legacy"
    for profile_id in ("Work A", "work-b"):
        workspace = legacy_root / "profiles" / profile_id
        workspace.mkdir(parents=True)
        (workspace / "state.db").write_text(profile_id, encoding="utf-8")
    legacy = {
        "version": 3,
        "active_profile_id": "work-b",
        "last_launched_profile_id": "Work A",
        "profiles": [
            {
                "profile_id": "Work A",
                "name": "Work A",
                "created_at": 10,
                "updated_at": 11,
                "graph_ports": {"input": "node-a"},
            },
            {
                "profile_id": "work-b",
                "name": "Work B",
                "created_at": 20,
                "updated_at": 21,
                "node_overrides": {"tool": "node-b"},
            },
        ],
    }

    receipt = store.import_legacy_collection(
        legacy,
        legacy_workspace_root=legacy_root,
    )
    profiles = store.list_profiles()
    assert [item.profile_id for item in profiles] == ["defaults", "work-a", "work-b"]
    assert [item.order for item in profiles] == [0, 1, 2]
    assert (profiles[1].created_at, profiles[1].updated_at) == (10, 11)
    assert profiles[1].profile["graph_ports"] == {"input": "node-a"}
    assert profiles[2].profile["node_overrides"] == {"tool": "node-b"}
    assert receipt.active_profile_id == "work-b"
    assert receipt.last_launched_profile_id == "work-a"
    assert (tmp_path / "workspaces" / "work-a" / "state.db").read_text() == "Work A"
    assert (tmp_path / "workspaces" / "work-b" / "state.db").read_text() == "work-b"
    assert store.legacy_state()["source_document"] == legacy


def test_localized_legacy_names_are_lossless_deterministic_and_restart_safe(
    tmp_path: Path,
) -> None:
    legacy = _legacy_runtime_collection()
    first = ProfileDefinitionStore(tmp_path / "first", clock=lambda: 1700000200)
    second = ProfileDefinitionStore(tmp_path / "second", clock=lambda: 1700000200)

    first_receipt = first.import_legacy_collection(legacy, copy_workspaces=False)
    second_receipt = second.import_legacy_collection(legacy, copy_workspaces=False)

    assert first_receipt.source_digest == second_receipt.source_digest
    assert first_receipt.profile_ids == (
        "default-profile",
        "new-custom-profile",
        "new-custom-profile-2",
    )
    assert first.snapshot() == second.snapshot()
    assert first.repair_legacy_display_names() == 0
    committed = first.snapshot()
    assert ProfileDefinitionStore(tmp_path / "first").snapshot() == committed
    assert first.legacy_state()["source_document"] == legacy
    profiles = first.list_profiles()
    assert [profile.display_name for profile in profiles] == [
        "Default Profile",
        "New custom profile",
        "New custom profile",
    ]
    for profile, source in zip(profiles, legacy["profiles"], strict=True):
        assert profile.profile["legacy_display_name"] == source["display_name"]
        assert profile.profile["display_name"] == source["display_name"]["ja"]


def test_real_disk_legacy_profiles_publish_review_only_v4_successors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migrate the three observed macOS Profiles without selecting one."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    settings = user_data / "settings"
    settings.mkdir(parents=True)
    legacy = _legacy_runtime_collection()
    settings.joinpath("startup_profiles.json").write_bytes(
        canonical_json(legacy) + b"\n"
    )
    identity_files = (
        "packs/closure.json",
        "conversation/history.json",
        "credentials/provider.ref",
    )
    for source in legacy["profiles"]:
        profile_id = str(source["profile_id"])
        workspace = user_data / "profiles" / profile_id
        for relative in identity_files:
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{relative}:{profile_id}", encoding="utf-8")
    legacy_pointer = user_data / "profiles" / "active_profile.json"
    legacy_pointer.write_bytes(
        canonical_json({"version": 1, "active_profile_id": "default-profile"}) + b"\n"
    )

    preexisting = ProfileDefinitionStore(user_data)
    preexisting.import_legacy_collection(settings / "startup_profiles.json")
    opaque = preexisting.get_profile("new-custom-profile")
    assert opaque is not None
    assert "profile_api_version" not in opaque.profile
    assert ActiveProfileStore(user_data).load() is None

    catalog = host_profile_catalog()
    definitions = ProfileDefinitionStore(user_data)
    expected_ids = (
        "default-profile",
        "new-custom-profile",
        "new-custom-profile-2",
    )
    assert (
        tuple(
            profile_id for profile_id in expected_ids if profile_id in catalog.profiles
        )
        == expected_ids
    )
    assert definitions.legacy_state()["source_document"] == legacy
    assert definitions.legacy_state()["active_profile_id"] == "default-profile"
    assert ActiveProfileStore(user_data).load() is None
    assert legacy_pointer.is_file()
    assert not (user_data / "authority" / "v4.sqlite3").exists()

    snapshot = definitions.snapshot()
    entries = {
        str(entry["profile_id"]): entry
        for entry in snapshot["profiles"]
        if entry["profile_id"] in expected_ids
    }
    for source in legacy["profiles"]:
        profile_id = str(source["profile_id"])
        stored = definitions.get_profile(profile_id)
        assert stored is not None
        assert stored.profile["profile_api_version"] == "io.tobkiri.profile.v4"
        assert stored.profile["profile_id"] == profile_id
        assert stored.profile["display_name"] == source["display_name"]["ja"]
        assert stored.profile["state"] == "needs_resolution"
        migrated_pack_ids = [item["pack_id"] for item in stored.profile["packs"]]
        assert migrated_pack_ids[0] == "defaultspack"
        assert migrated_pack_ids[-1] == "runtime.tauri.application.default"
        assert "rumi_ai_gateway_pack" in migrated_pack_ids
        assert "rumi_provider_adapters_pack" in migrated_pack_ids
        assert stored.profile["authority_references"] == []
        assert stored.profile["profile_authority_snapshot_digest"] is None
        validate_document(stored.profile, "profile")

        revisions = entries[profile_id]["revisions"]
        assert len(revisions) == 2
        assert revisions[1]["parent_revision"] == revisions[0]["profile_revision"]
        assert revisions[0]["profile"]["legacy_display_name"] == source["display_name"]
        assert revisions[0]["profile"]["display_name"] == source["display_name"]["ja"]
        for relative in identity_files:
            migrated = user_data / "workspaces" / profile_id / relative
            assert migrated.read_text(encoding="utf-8") == f"{relative}:{profile_id}"

    confirmation = prepare_profile_confirmation("new-custom-profile")
    assert confirmation["profile_id"] == "new-custom-profile"
    assert confirmation["operation_id"] == "profile.activate"
    assert ActiveProfileStore(user_data).load() is None
    assert not (user_data / "authority" / "v4.sqlite3").exists()


def test_missing_optional_display_name_is_a_restart_safe_noop(
    tmp_path: Path,
) -> None:
    normal = _definition("profile-a", "Profile A")
    del normal["display_name"]
    normal_store = ProfileDefinitionStore(tmp_path / "normal", clock=lambda: 10)
    created = normal_store.create_profile(normal)
    normal_snapshot = normal_store.snapshot()

    assert created.display_name == "profile-a"
    assert "display_name" not in created.profile
    assert normal_store.repair_legacy_display_names() == 0
    assert normal_store.snapshot() == normal_snapshot
    assert ProfileDefinitionStore(tmp_path / "normal").snapshot() == normal_snapshot

    legacy_store = ProfileDefinitionStore(tmp_path / "legacy", clock=lambda: 20)
    legacy_store.import_legacy_collection(
        {
            "version": 4,
            "profiles": [{"profile_id": "profile-b", "locale": "ja"}],
        },
        copy_workspaces=False,
    )
    legacy_snapshot = legacy_store.snapshot()
    imported = legacy_store.get_profile("profile-b")

    assert imported is not None
    assert imported.display_name == "profile-b"
    assert "display_name" not in imported.profile
    assert legacy_store.repair_legacy_display_names() == 0
    assert legacy_store.snapshot() == legacy_snapshot
    assert ProfileDefinitionStore(tmp_path / "legacy").snapshot() == legacy_snapshot


def test_existing_localized_revisions_are_repaired_transactionally_and_idempotently(
    tmp_path: Path,
) -> None:
    store = ProfileDefinitionStore(tmp_path, clock=lambda: 1700000300)
    store.create_profile(_definition("profile-a", "Profile A"))
    poisoned = store.snapshot()
    entry = poisoned["profiles"][0]
    legacy_profile = entry["revisions"][0]["profile"]
    legacy_profile.update(
        {
            "name": "Legacy A",
            "locale": "ja",
            "display_name": {"en": "English A", "ja": "日本語 A"},
        }
    )
    legacy_revision = canonical_digest(legacy_profile)
    entry["current_revision"] = legacy_revision
    entry["revisions"][0]["profile_revision"] = legacy_revision
    poisoned["store_digest"] = canonical_digest(
        {key: value for key, value in poisoned.items() if key != "store_digest"}
    )
    store.path.write_bytes(canonical_json(poisoned) + b"\n")

    assert store.repair_legacy_display_names() == 1
    repaired = store.snapshot()
    revisions = repaired["profiles"][0]["revisions"]
    assert len(revisions) == 2
    assert revisions[0]["profile"]["display_name"] == {
        "en": "English A",
        "ja": "日本語 A",
    }
    assert revisions[1]["parent_revision"] == legacy_revision
    assert revisions[1]["profile"]["display_name"] == "日本語 A"
    assert revisions[1]["profile"]["legacy_display_name"] == {
        "en": "English A",
        "ja": "日本語 A",
    }
    assert store.repair_legacy_display_names() == 0
    assert store.snapshot() == repaired
    assert ProfileDefinitionStore(tmp_path).snapshot() == repaired


@pytest.mark.parametrize(
    "display_name",
    (
        None,
        "",
        {},
        {"ja": ""},
        {"ja": 7},
        {"fr": "Nom"},
        {"ja": "A", "JA": "B"},
    ),
)
def test_invalid_or_ambiguous_localized_legacy_names_fail_closed(
    tmp_path: Path,
    display_name: object,
) -> None:
    legacy = {
        "profiles": [
            {
                "profile_id": "profile-a",
                "display_name": display_name,
            }
        ]
    }

    with pytest.raises(ProfileDefinitionStoreIntegrityError):
        ProfileDefinitionStore(tmp_path).import_legacy_collection(
            legacy,
            copy_workspaces=False,
        )
    assert not ProfileDefinitionStore(tmp_path).exists()


def test_legacy_pointer_requires_same_identity_ceremony_and_never_becomes_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 selection cannot be relabeled as the Defaults execution identity."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    settings = user_data / "settings"
    settings.mkdir(parents=True)
    settings.joinpath("startup_profiles.json").write_bytes(
        canonical_json(_legacy_runtime_collection()) + b"\n"
    )
    profiles_root = user_data / "profiles"
    profiles_root.mkdir()
    legacy_pointer = profiles_root / "active_profile.json"
    legacy_pointer.write_bytes(
        canonical_json({"version": 1, "active_profile_id": "default-profile"}) + b"\n"
    )
    host_profile_catalog()
    definitions = ProfileDefinitionStore(user_data)
    assert [item.profile_id for item in definitions.list_profiles()] == [
        "default-profile",
        "new-custom-profile",
        "new-custom-profile-2",
    ]
    pointer_store = ActiveProfileStore(user_data)

    repaired = repair_legacy_active_profile_pointer()

    assert repaired is None
    assert pointer_store.load() is None
    assert legacy_pointer.is_file()
    assert definitions.legacy_state()["active_profile_id"] == "default-profile"
    assert definitions.get_profile("default-profile") is not None
    assert definitions.get_profile("defaults") is None
    assert repair_legacy_active_profile_pointer() is None
    assert pointer_store.load() is None


def test_legacy_defaults_id_is_preserved_when_no_bootstrap_template_exists(
    tmp_path: Path,
) -> None:
    """Preserve legacy identity without reserving a product-favored ID."""

    store = ProfileDefinitionStore(tmp_path)
    receipt = store.import_legacy_collection(
        {
            "profiles": [{"profile_id": "defaults", "display_name": "My old Defaults"}],
            "active_profile_id": "defaults",
        },
        copy_workspaces=False,
    )

    assert receipt.legacy_id_map == {"defaults": "defaults"}
    assert receipt.active_profile_id == "defaults"
    assert store.get_profile("defaults") is not None


def test_legacy_profile_map_key_and_extra_fields_are_lossless(tmp_path: Path) -> None:
    """Preserve map-key identity and unknown legacy fields during migration."""

    store = ProfileDefinitionStore(tmp_path)
    legacy = {
        "version": 4,
        "active_profile_id": "map-key-a",
        "profiles": {
            "map-key-a": {
                "name": "Mapped A",
                "created_at": 7,
                "unknown_runtime_field": {"nested": [1, 2, 3]},
            }
        },
        "legacy_selection": {"last_view": "conversation"},
    }

    receipt = store.import_legacy_collection(legacy, copy_workspaces=False)

    assert receipt.legacy_id_map == {"map-key-a": "map-key-a"}
    imported = store.get_profile("map-key-a")
    assert imported is not None
    assert imported.profile["unknown_runtime_field"] == {"nested": [1, 2, 3]}
    assert store.legacy_state()["source_document"] == legacy


def test_legacy_workspace_failure_rolls_back_registry(tmp_path: Path) -> None:
    store = ProfileDefinitionStore(tmp_path)
    legacy_root = tmp_path / "legacy"
    workspace = legacy_root / "profiles" / "profile-a"
    workspace.mkdir(parents=True)
    (workspace / "bad-link").symlink_to(tmp_path / "outside")
    legacy = {"profiles": [{"profile_id": "profile-a", "name": "A"}]}

    with pytest.raises(ProfileDefinitionStoreError):
        store.import_legacy_collection(
            legacy,
            legacy_workspace_root=legacy_root,
        )
    assert store.list_profiles() == ()
    assert not store.exists()


@pytest.mark.parametrize(
    "legacy_id",
    (
        "../escape",
        "../../escape",
        "/absolute/escape",
        "nested/profile",
        r"..\escape",
        r"C:\absolute\escape",
        "C:/absolute/escape",
    ),
)
@pytest.mark.parametrize(
    "source_case",
    ("copy-disabled", "source-none", "profiles-missing"),
)
def test_legacy_workspace_path_lookup_rejects_unsafe_ids_without_commit(
    tmp_path: Path,
    legacy_id: str,
    source_case: str,
) -> None:
    """Raw IDs fail before copy policy or missing-root early returns."""

    store = ProfileDefinitionStore(tmp_path / "user-data")
    legacy_root = tmp_path / "legacy"
    if source_case == "profiles-missing":
        legacy_root.mkdir()
    outside = tmp_path / "escape"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("must-not-copy", encoding="utf-8")

    import_kwargs: dict[str, object] = {}
    if source_case == "copy-disabled":
        import_kwargs["copy_workspaces"] = False
    elif source_case == "source-none":
        import_kwargs["legacy_workspace_root"] = None
    else:
        import_kwargs["legacy_workspace_root"] = legacy_root
    with pytest.raises(ProfileDefinitionStoreIntegrityError, match="unsafe"):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": legacy_id, "name": "Attacker"}]},
            **import_kwargs,
        )

    assert not store.exists()
    assert store.list_profiles() == ()
    assert not (tmp_path / "user-data" / "workspaces").exists()
    assert secret.read_text(encoding="utf-8") == "must-not-copy"


@pytest.mark.parametrize("symlink_kind", ("root", "ancestor"))
def test_legacy_workspace_source_ancestor_symlinks_fail_closed(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    """Every source ancestor is opened from the filesystem root with no-follow."""

    store = ProfileDefinitionStore(tmp_path / "user-data")
    real_parent = tmp_path / "real-parent"
    real_root = real_parent / "legacy"
    workspace = real_root / "profiles" / "profile-a"
    workspace.mkdir(parents=True)
    (workspace / "state.db").write_text("must-not-copy", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("unchanged", encoding="utf-8")

    if symlink_kind == "root":
        source_root = tmp_path / "legacy-alias"
        source_root.symlink_to(real_root, target_is_directory=True)
    else:
        parent_alias = tmp_path / "parent-alias"
        parent_alias.symlink_to(real_parent, target_is_directory=True)
        source_root = parent_alias / "legacy"

    with pytest.raises(ProfileDefinitionStoreIntegrityError, match="root"):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": "profile-a", "name": "A"}]},
            legacy_workspace_root=source_root,
        )

    assert not store.exists()
    assert not (tmp_path / "user-data" / "workspaces").exists()
    assert secret.read_text(encoding="utf-8") == "unchanged"


def test_legacy_workspace_profiles_root_symlink_fails_closed_without_commit(
    tmp_path: Path,
) -> None:
    """An intermediate profiles-root symlink must not be resolved for import."""

    store = ProfileDefinitionStore(tmp_path / "user-data")
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    outside = tmp_path / "outside"
    (outside / "profile-a").mkdir(parents=True)
    (outside / "profile-a" / "secret.txt").write_text("must-not-copy", encoding="utf-8")
    (legacy_root / "profiles").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProfileDefinitionStoreIntegrityError, match="profiles root"):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": "profile-a", "name": "A"}]},
            legacy_workspace_root=legacy_root,
        )

    assert not store.exists()
    assert store.list_profiles() == ()
    assert not (tmp_path / "user-data" / "workspaces").exists()
    assert (outside / "profile-a" / "secret.txt").read_text() == "must-not-copy"


def test_legacy_workspace_copy_rejects_path_replacement_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source path replaced during copy must fail closed before publication."""

    from core_runtime import profile_definition_store_v4 as store_module

    store = ProfileDefinitionStore(tmp_path / "user-data")
    legacy_root = tmp_path / "legacy"
    workspace = legacy_root / "profiles" / "profile-a"
    workspace.mkdir(parents=True)
    source_file = workspace / "state.db"
    source_file.write_text("original", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must-not-copy", encoding="utf-8")

    original_open = store_module.os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "state.db" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            source_file.unlink()
            source_file.symlink_to(outside / "secret.txt")
        return descriptor

    monkeypatch.setattr(store_module.os, "open", racing_open)
    with pytest.raises(ProfileDefinitionStoreIntegrityError, match="changed"):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": "profile-a", "name": "A"}]},
            legacy_workspace_root=legacy_root,
        )

    assert replaced
    assert not store.exists()
    assert store.list_profiles() == ()
    assert not (tmp_path / "user-data" / "workspaces").exists()


@pytest.mark.parametrize("swap_kind", ("user-data", "workspaces"))
def test_legacy_workspace_publication_parent_swap_cannot_escape_or_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_kind: str,
) -> None:
    """A parent rename/symlink swap is detected after descriptor-safe publication."""

    from core_runtime import profile_definition_store_v4 as store_module

    user_data = tmp_path / "user-data"
    store = ProfileDefinitionStore(user_data)
    legacy_root = tmp_path / "legacy"
    source_workspace = legacy_root / "profiles" / "profile-a"
    source_workspace.mkdir(parents=True)
    (source_workspace / "state.db").write_text("original", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")

    original_rename = store_module.os.rename
    swapped = False

    def racing_rename(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("src_dir_fd") is not None:
            swapped = True
            if swap_kind == "user-data":
                moved = tmp_path / "user-data-held"
                original_rename(user_data, moved)
                user_data.symlink_to(outside, target_is_directory=True)
            else:
                workspaces = user_data / "workspaces"
                moved = tmp_path / "workspaces-held"
                original_rename(workspaces, moved)
                workspaces.symlink_to(outside, target_is_directory=True)
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "rename", racing_rename)
    with pytest.raises(ProfileDefinitionStoreError):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": "profile-a", "name": "A"}]},
            legacy_workspace_root=legacy_root,
        )

    assert swapped
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    if swap_kind == "user-data":
        held = tmp_path / "user-data-held"
        assert not (held / "profiles" / "index.json").exists()
        assert not (held / "workspaces").exists()
    else:
        held = tmp_path / "workspaces-held"
        assert not (user_data / "profiles" / "index.json").exists()
        assert not (held / "profile-a" / "state.db").exists()


def test_legacy_workspace_copy_rejects_hardlinks_without_commit(
    tmp_path: Path,
) -> None:
    """Workspace files with st_nlink > 1 never enter the staging tree."""

    store = ProfileDefinitionStore(tmp_path / "user-data")
    legacy_root = tmp_path / "legacy"
    workspace = legacy_root / "profiles" / "profile-a"
    workspace.mkdir(parents=True)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("must-not-copy", encoding="utf-8")
    os.link(outside_file, workspace / "hardlink.txt")

    with pytest.raises(ProfileDefinitionStoreIntegrityError, match="multiple links"):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": "profile-a", "name": "A"}]},
            legacy_workspace_root=legacy_root,
        )

    assert not store.exists()
    assert not (tmp_path / "user-data" / "workspaces").exists()
    assert outside_file.read_text(encoding="utf-8") == "must-not-copy"


def test_legacy_workspace_copy_rejects_in_place_source_mutation_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source file changing while it is read invalidates the whole import."""

    from core_runtime import profile_definition_store_v4 as store_module

    store = ProfileDefinitionStore(tmp_path / "user-data")
    legacy_root = tmp_path / "legacy"
    workspace = legacy_root / "profiles" / "profile-a"
    workspace.mkdir(parents=True)
    source_file = workspace / "state.db"
    source_file.write_text("original", encoding="utf-8")
    original_read = store_module.os.read
    mutated = False

    def racing_read(descriptor, size):
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            source_file.write_text("tamper!!", encoding="utf-8")
        return chunk

    monkeypatch.setattr(store_module.os, "read", racing_read)
    with pytest.raises(ProfileDefinitionStoreIntegrityError, match="changed"):
        store.import_legacy_collection(
            {"profiles": [{"profile_id": "profile-a", "name": "A"}]},
            legacy_workspace_root=legacy_root,
        )

    assert mutated
    assert not store.exists()
    assert not (tmp_path / "user-data" / "workspaces").exists()
    assert source_file.read_text(encoding="utf-8") == "tamper!!"


def test_real_disk_named_profiles_keep_execution_and_workspace_state_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Defaults -> A -> B -> restart -> A on real v4 persistence."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    user_data = runtime_user_data_root()
    host_profile_catalog()
    definitions = ProfileDefinitionStore(user_data)
    defaults = definitions.get_profile("defaults")
    assert defaults is not None
    definitions.duplicate_profile(
        "defaults",
        new_profile_id="profile-a",
        display_name="Profile A",
        expected_profile_revision=defaults.profile_revision,
    )
    definitions.duplicate_profile(
        "defaults",
        new_profile_id="profile-b",
        display_name="Profile B",
        expected_profile_revision=defaults.profile_revision,
    )

    def assert_active(expected_profile_id: str):
        active = capture_active_profile()
        pointer = ActiveProfileStore(user_data).require(verify_snapshot=True)
        assert active.resolved.profile["profile_id"] == expected_profile_id
        assert pointer.profile_id == expected_profile_id
        assert pointer.profile_revision == active.resolved.plan["profile_revision"]
        assert pointer.plan_digest == active.resolved.plan["plan_digest"]
        assert pointer.lock_digest == active.resolved.lock["lock_digest"]
        assert pointer.activation_id == active.activation["activation_id"]
        receipt = activation_audit_receipt(active)
        assert receipt["state"] == "committed"
        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            reservation = authority.active_activation_reservation(
                active.activation["activation_id"]
            )
        assert reservation is not None
        assert reservation["state"] == "active"
        assert reservation["plan_digest"] == active.activation["plan_digest"]
        assert reservation["fencing_token"] == active.activation["fencing_token"]
        return active

    def write_profile_state(profile_id: str) -> None:
        workspace = user_data / "workspaces" / profile_id
        state = {
            "packs/closure.json": f"pack-state:{profile_id}",
            "conversation/history.json": f"conversation:{profile_id}",
            "credentials/provider.ref": f"credential-ref:{profile_id}",
            "handoff/shell.json": f"shell-handoff:{profile_id}",
        }
        for relative, value in state.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")

    defaults_active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    assert_active("defaults")
    write_profile_state("defaults")

    active_a = capture_profile(
        "profile-a",
        confirmation=prepare_profile_confirmation("profile-a"),
    )
    assert_active("profile-a")
    write_profile_state("profile-a")

    active_b = capture_profile(
        "profile-b",
        confirmation=prepare_profile_confirmation("profile-b"),
    )
    assert_active("profile-b")
    write_profile_state("profile-b")

    restarted = ActiveProfileStore(user_data).require(verify_snapshot=True)
    assert restarted.profile_id == "profile-b"
    assert_active("profile-b")

    browsing_surface = RuntimeSurfaceService(
        snapshot_loader=capture_active_profile,
        catalog_loader=host_profile_catalog,
    )
    browsing_profile = browsing_surface.read_profile(selected_profile_id="profile-a")
    assert browsing_profile["data"]["selection"] == {
        "state": "browsing",
        "selected_profile_id": "profile-a",
        "execution_profile_id": "profile-b",
        "execution_profile_revision": active_b.resolved.plan["profile_revision"],
        "execution_activation_id": active_b.activation["activation_id"],
        "execution_plan_digest": active_b.resolved.plan["plan_digest"],
    }
    assert browsing_profile["data"]["resolved_plan"] is None
    assert browsing_profile["data"]["activation_record"] is None
    with pytest.raises(RuntimeSurfaceError) as stale:
        browsing_surface.read_profile(
            selected_profile_id="profile-a",
            expected_profile_revision="sha256:" + "0" * 64,
            expected_plan_digest=active_b.resolved.plan["plan_digest"],
        )
    assert stale.value.code is RuntimeSurfaceErrorCode.STALE_REVISION
    browsing_operations = browsing_surface.read_advanced(
        "operations",
        selected_profile_id="profile-a",
    )["data"]["operations"]
    assert browsing_operations
    assert all(
        item["invokable"] is False and item["invocation_reason"] == "browsing_only"
        for item in browsing_operations
    )
    browsing_settings = browsing_surface.read_settings(selected_profile_id="profile-a")
    assert (
        browsing_settings["data"]["runtime_profile_settings"]["state"]
        == "browsing_only"
    )
    browsing_surface.close()
    assert_active("profile-b")

    active_a_again = capture_profile(
        "profile-a",
        confirmation=prepare_profile_confirmation("profile-a"),
    )
    assert active_a_again.resolved.profile["profile_id"] == "profile-a"
    assert_active("profile-a")

    assert (
        len(
            {
                defaults_active.resolved.plan["plan_digest"],
                active_a.resolved.plan["plan_digest"],
                active_b.resolved.plan["plan_digest"],
            }
        )
        == 3
    )
    assert ActiveProfileStore(user_data).path == user_data / "profiles" / "active.json"
    assert ActiveProfileStore(user_data).path.parent not in {
        user_data / "workspaces" / profile_id
        for profile_id in ("defaults", "profile-a", "profile-b")
    }
    for profile_id in ("defaults", "profile-a", "profile-b"):
        workspace = user_data / "workspaces" / profile_id
        for relative in (
            "packs/closure.json",
            "conversation/history.json",
            "credentials/provider.ref",
            "handoff/shell.json",
        ):
            assert (
                (workspace / relative).read_text(encoding="utf-8").endswith(profile_id)
            )
