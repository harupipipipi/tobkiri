"""Source additions use the same exact-confirmation activation transaction."""

from copy import deepcopy
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.profile_registry import register_bootstrap_definition
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from core_runtime.profile_runtime_port import require_profile_runtime
from ecosystem.defaultspack.domain.runtime_v4 import ActivationStore, ProfileResolutionDenied
from tests.test_profile_architecture_review_c import _packaged_catalog_revision, _resolve


def test_source_additions_require_their_own_confirmation_and_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor_catalog = _packaged_catalog_revision(tmp_path / "predecessor", b"before")
    successor_catalog = _packaged_catalog_revision(tmp_path / "successor", b"after")
    runtime = require_profile_runtime()
    previous_definition = deepcopy(predecessor_catalog.profiles["defaults"])
    previous_definition["display_name"] = "My retained Defaults"
    added_packs = {"tobkiri_ui_settings_pack", "rumi_conversation_store_pack"}
    previous_definition["packs"] = [
        row for row in previous_definition["packs"] if row["pack_id"] not in added_packs
    ]
    added_operations = {
        "command.catalog.read",
        "rumi_model_registry_pack.model-profile-resource",
        "tobkiri_ui_settings_pack.catalog-read",
        "tobkiri_ui_settings_pack.settings-read",
        "rumi_conversation_store_pack.conversation-resource",
    }
    previous_definition["requested_edges"] = [
        edge
        for edge in previous_definition["requested_edges"]
        if edge["operation_id"] not in added_operations
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
    assert not added_packs & {row["pack_id"] for row in unchanged.profiles["defaults"]["packs"]}
    assert added_packs <= {row["pack_id"] for row in proposed.profiles["defaults"]["packs"]}
    assert proposed.profiles["defaults"]["display_name"] == "My retained Defaults"
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

    active = profile_capture.capture_bootstrap_profile(
        include_source_additions=True, confirmation=confirmation
    )
    assert added_packs <= {row["pack_id"] for row in active.resolved.profile["packs"]}
    assert profile_capture.capture_bootstrap_profile() == active
    with pytest.raises(ProfileResolutionDenied, match="requires reconfirmation"):
        profile_capture.capture_bootstrap_profile(
            include_source_additions=True, confirmation=confirmation
        )
    assert (
        ProfileDefinitionStore(user_data).get_profile("defaults").display_name
        == "My retained Defaults"
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
