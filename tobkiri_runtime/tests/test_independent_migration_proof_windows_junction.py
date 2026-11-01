"""Native Windows proof fixture must use a real escaping reparse point."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreIntegrityError,
)
from scripts.quality import run_independent_migration_proof as proof


@pytest.mark.skipif(os.name != "nt", reason="Windows junction proof")
def test_unsafe_junction_preflight_rejects_without_committing(tmp_path: Path) -> None:
    broken = proof._copy_broken_workspace_fixture(
        proof.PROFILE_WORKSPACE_FIXTURE,
        tmp_path / "broken-legacy",
    )
    outside = tmp_path / "outside-proof-target"
    link = broken / "profiles" / "profile-cleo" / "notes" / "escape"
    assert proof._unsafe_workspace_link(link)
    assert not link.is_symlink()
    assert link.resolve() == outside.resolve()
    assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "outside"

    with pytest.raises(proof.IndependentMigrationProofError, match="unsafe link"):
        proof._workspace_snapshot(broken / "profiles" / "profile-cleo")

    destination = tmp_path / "broken-destination"
    with pytest.raises(ProfileDefinitionStoreIntegrityError):
        ProfileDefinitionStore(destination).import_legacy_collection(
            proof._load_json(proof.PROFILE_FIXTURE),
            legacy_workspace_root=broken,
        )
    proof._assert_uncommitted(destination)
    assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "outside"
