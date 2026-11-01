"""Regression coverage for setup-template registration boundaries."""

from copy import deepcopy
from pathlib import Path

import pytest

from core_runtime.bootstrap.profile_registry import register_bootstrap_definition
from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from tobkiri_protocol.canonical import canonical_digest


def _definition(*, shell_digest: str, provenance: str) -> dict[str, object]:
    return {
        "profile_api_version": "io.tobkiri.profile.v5",
        "profile_id": "defaults",
        "display_name": "Defaults",
        "mode": "interactive",
        "shell": {
            "pack_id": "shell",
            "artifact_digest": shell_digest,
        },
        "packs": [
            {
                "pack_id": "example-pack",
                "artifact_digest": "sha256:" + "3" * 64,
            }
        ],
        "provenance": {"source_revision": provenance},
    }


def test_first_activation_replaces_only_the_unmodified_bootstrap_template(
    tmp_path: Path,
) -> None:
    """The confirmed packaged source may supersede its untouched setup template."""
    template = _definition(
        shell_digest="sha256:" + "1" * 64,
        provenance="generator-source",
    )
    selected_source = _definition(
        shell_digest="sha256:" + "2" * 64,
        provenance="packaged-build",
    )
    store = ProfileDefinitionStore(tmp_path)
    template_record = store.bootstrap_defaults(template)

    register_bootstrap_definition(tmp_path, selected_source)

    registered = store.get_profile("defaults")
    assert registered is not None
    assert registered.profile == selected_source
    assert registered.parent_revision == template_record.profile_revision
    assert registered.profile_revision == canonical_digest(selected_source)
    assert store.bootstrap_state() == {
        "state": "not_required",
        "template_profile_revision": None,
    }
    revisions = store.snapshot()["profiles"][0]["revisions"]
    assert [revision["profile_revision"] for revision in revisions] == [
        canonical_digest(template),
        canonical_digest(selected_source),
    ]


@pytest.mark.parametrize(
    "protected_state",
    ["user_edit", "tombstone", "host_active", "workspace_active"],
)
def test_bootstrap_source_cannot_replace_authoritative_profile_state(
    tmp_path: Path,
    protected_state: str,
) -> None:
    """Edits, tombstones, and either active pointer keep conflicts fail closed."""
    template = _definition(
        shell_digest="sha256:" + "1" * 64,
        provenance="generator-source",
    )
    selected_source = _definition(
        shell_digest="sha256:" + "2" * 64,
        provenance="packaged-build",
    )
    store = ProfileDefinitionStore(tmp_path)
    created = store.bootstrap_defaults(template)
    if protected_state == "user_edit":
        edited = deepcopy(template)
        edited["display_name"] = "My Defaults"
        store.update_profile(
            "defaults",
            edited,
            expected_profile_revision=created.profile_revision,
        )
    elif protected_state == "tombstone":
        store.delete_profile(
            "defaults",
            expected_profile_revision=created.profile_revision,
        )
    else:
        active_path = (
            tmp_path / "profiles" / "active.json"
            if protected_state == "host_active"
            else tmp_path
            / "workspaces"
            / "defaults"
            / "activation"
            / "active.json"
        )
        active_path.parent.mkdir(parents=True, exist_ok=True)
        active_path.write_text("{}\n", encoding="utf-8")

    before = store.snapshot()
    with pytest.raises(ProfileDefinitionStoreConflict, match="conflicts"):
        register_bootstrap_definition(tmp_path, selected_source)
    assert store.snapshot() == before
