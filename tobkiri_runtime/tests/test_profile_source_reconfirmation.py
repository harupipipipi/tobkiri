"""Source additions use the same exact-confirmation activation transaction."""

from copy import deepcopy
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.profile_registry import register_bootstrap_definition
from core_runtime.bootstrap.profile_source_update import interrupted_source_update_predecessor
from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore, ProfileDefinitionStoreConflict,
)
from core_runtime.profile_runtime_port import require_profile_runtime
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_runtime_capture_inputs,
)
from ecosystem.defaultspack.domain.runtime_v4 import ActivationStore, ProfileResolutionDenied
from tests.test_profile_architecture_review_c import _packaged_catalog_revision, _resolve
from tobkiri_protocol.canonical import canonical_digest


@pytest.mark.parametrize("unpinned_scope", [False, True])
@pytest.mark.parametrize("same_catalog", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_source_additions_require_their_own_confirmation_and_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unpinned_scope: bool,
    same_catalog: bool, interrupted: bool,
) -> None:
    predecessor_catalog = _packaged_catalog_revision(tmp_path / "predecessor", b"before")
    successor_catalog = (
        predecessor_catalog if same_catalog
        else _packaged_catalog_revision(tmp_path / "successor", b"after")
    )
    runtime = require_profile_runtime()
    previous_definition = deepcopy(predecessor_catalog.profiles["defaults"])
    previous_definition["display_name"] = "My retained Defaults"
    if unpinned_scope:
        for edge in previous_definition["requested_edges"]:
            edge["requested_scope_template"].pop("semantics_digest", None)
    added_packs = {
        "tobkiri_ui_settings_pack", "rumi_conversation_store_pack",
        "rumi_turn_runtime_pack",
    }
    previous_definition["packs"] = [
        row for row in previous_definition["packs"] if row["pack_id"] not in added_packs
    ]
    added_operations = {
        "command.catalog.read",
        "rumi_model_registry_pack.model-profile-resource",
        "tobkiri_ui_settings_pack.catalog-read",
        "tobkiri_ui_settings_pack.settings-read",
        "rumi_conversation_store_pack.conversation-resource",
        "tobkiri_ui_settings_pack.preferences-write",
        "rumi_conversation_store_pack.conversation-manage",
        "rumi_model_registry_pack.model-profile-manage",
    }
    added_functions = {
        function["id"]
        for pack_id in added_packs
        for function in predecessor_catalog.packs[pack_id]["functions"]
    }
    previous_definition["requested_edges"] = [
        edge
        for edge in previous_definition["requested_edges"]
        if edge["operation_id"] not in added_operations
        and edge["caller_function_id"] not in added_functions
        and edge["target_provider_id"] not in added_functions
    ]
    predecessor_catalog = runtime.catalog_with_profiles(
        predecessor_catalog, {**predecessor_catalog.profiles, "defaults": previous_definition}
    )
    user_data = tmp_path / "user-data"
    workspace = user_data / "workspaces" / "defaults"
    workspace.mkdir(parents=True)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = ActivationStore(
            workspace / "activation",
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=predecessor_catalog,
        )
        store.activate(
            _resolve(predecessor_catalog),
            activation_id="activation:source-update-before",
            created_at="2026-09-08T00:00:00Z",
        )
        previous = store.load_active_snapshot()
    register_bootstrap_definition(user_data, previous_definition)
    profile_capture._publish_host_active_pointer(
        previous, user_data=user_data, replace_existing=False
    )
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base=None: successor_catalog.root)
    before = ProfileDefinitionStore(user_data).snapshot()
    pointer_path = workspace / "activation" / "active.json"
    pointer_before = pointer_path.read_bytes()

    unchanged, old_confirmation = profile_capture.prepare_bootstrap_profile_review()
    proposed, confirmation = profile_capture.prepare_bootstrap_profile_review(
        include_source_additions=True
    )
    retained, _ = profile_capture._bootstrap_review_candidate()
    assert not added_packs & {
        row["pack_id"] for row in retained.profiles["defaults"]["packs"]
    }
    assert {row["pack_id"] for row in unchanged.profiles["defaults"]["packs"]} == {
        row["pack_id"] for row in previous.resolved.profile["packs"]
    }
    assert added_packs <= {row["pack_id"] for row in proposed.profiles["defaults"]["packs"]}
    assert proposed.profiles["defaults"]["display_name"] == "My retained Defaults"
    if unpinned_scope and not same_catalog:
        assert all(
            "semantics_digest" in edge["requested_scope_template"]
            for edge in unchanged.profiles["defaults"]["requested_edges"]
        )
    assert confirmation != old_confirmation
    assert ProfileDefinitionStore(user_data).snapshot() == before
    assert pointer_path.read_bytes() == pointer_before
    with pytest.raises(ProfileResolutionDenied, match="explicit confirmation"):
        profile_capture.capture_bootstrap_profile(include_source_additions=True)
    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_bootstrap_profile(
            include_source_additions=True, confirmation=old_confirmation
        )
    assert ProfileDefinitionStore(user_data).snapshot() == before
    assert pointer_path.read_bytes() == pointer_before

    if interrupted:
        method = "activate" if same_catalog else "reconcile_active"

        def fail_before_activation(*args, **kwargs):
            raise OSError("activation commit interrupted")

        with monkeypatch.context() as failure:
            failure.setattr(ActivationStore, method, fail_before_activation)
            with pytest.raises(OSError, match="commit interrupted"):
                profile_capture.capture_bootstrap_profile(
                    include_source_additions=True, confirmation=confirmation,
                )
        assert pointer_path.read_bytes() == pointer_before
        registered_after_failure = ProfileDefinitionStore(user_data).snapshot()
        assert registered_after_failure != before
        _, confirmation = profile_capture.prepare_bootstrap_profile_review(
            include_source_additions=True,
        )
        assert ProfileDefinitionStore(user_data).snapshot() == registered_after_failure
        assert pointer_path.read_bytes() == pointer_before
        with pytest.raises(ProfileResolutionDenied, match="explicit confirmation"):
            profile_capture.capture_bootstrap_profile(include_source_additions=True)

    active = profile_capture.capture_bootstrap_profile(
        include_source_additions=True, confirmation=confirmation
    )
    assert added_packs <= {row["pack_id"] for row in active.resolved.profile["packs"]}
    # The update must restore the signed application's actual HTTP contract
    # coverage, not merely add rows to the selected-Pack inventory.
    inputs = defaultspack_runtime_capture_inputs(
        active, bundle_root=successor_catalog.root,
    )
    assert any(
        binding.path == "/api/chat/turn" and binding.method == "POST"
        for binding in inputs.contract_bindings
    )
    assert profile_capture.capture_bootstrap_profile() == active
    with pytest.raises(ProfileResolutionDenied, match="requires reconfirmation"):
        profile_capture.capture_bootstrap_profile(
            include_source_additions=True, confirmation=confirmation
        )
    assert (
        ProfileDefinitionStore(user_data).get_profile("defaults").display_name
        == "My retained Defaults"
    )


@pytest.mark.parametrize("change", [
    "unrelated_edit", "wrong_parent", "tampered_predecessor", "stale_current",
    "tombstone", "wrong_profile",
])
def test_interrupted_update_recovery_rejects_unrelated_registry_changes(change: str) -> None:
    """Historical ancestry alone cannot authorize an arbitrary registered edit."""
    previous = {
        "profile_id": "defaults", "base": {"pack_id": "base"},
        "shell": {"pack_id": "shell"}, "packs": [{"pack_id": "old"}],
        "requested_edges": [],
    }
    source = deepcopy(previous)
    source["packs"].append({"pack_id": "new"})
    registered = deepcopy(source)
    if change == "unrelated_edit":
        registered["display_name"] = "Unrelated edit"
    prior_digest = canonical_digest(previous)
    current_digest = canonical_digest(registered)
    entry = {
        "profile_id": "defaults", "tombstone": False,
        "current_revision": current_digest,
        "revisions": [
            {"profile_revision": prior_digest, "profile": previous},
            {"profile_revision": current_digest, "profile": registered,
             "parent_revision": prior_digest},
        ],
    }
    if change == "wrong_parent":
        entry["revisions"][1]["parent_revision"] = "different"
    elif change == "tampered_predecessor":
        entry["revisions"][0]["profile"]["display_name"] = "Changed history"
    elif change == "stale_current":
        entry["current_revision"] = "different"
    elif change == "tombstone":
        entry["tombstone"] = True
    elif change == "wrong_profile":
        entry["profile_id"] = "another-profile"
    with pytest.raises(ProfileDefinitionStoreConflict, match="verified interrupted"):
        interrupted_source_update_predecessor(
            {"profiles": [entry]}, registered, prior_digest, source,
            successor_required=False,
        )


def test_source_update_cannot_create_an_unconfirmed_initial_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_data = tmp_path / "absent-user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    with pytest.raises(ProfileResolutionDenied, match="requires an active Profile"):
        profile_capture.capture_bootstrap_profile(
            include_source_additions=True, confirmation={}
        )
    assert not user_data.exists()
