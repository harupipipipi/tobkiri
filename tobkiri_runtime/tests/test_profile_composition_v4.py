"""Editing a Profile definition never grants or activates its Pack choices."""
from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.bootstrap.profile_capture import host_profile_catalog
from core_runtime.profile_composition_v4 import (
    build_profile_composition,
    project_composition_catalog,
)
from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from tobkiri_protocol.canonical import canonical_digest
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root


@pytest.fixture
def composition_context(tmp_path: Path):
    user_data = tmp_path / "user-data"
    catalog = host_profile_catalog(
        bundle_root=packaged_profile_bundle_root(), user_data_root=user_data
    )
    source = next(iter(catalog.profiles.values()))
    projection = project_composition_catalog(catalog)
    request = {
        "pack_ids": [row["pack_id"] for row in source["packs"] if row["role"] != "application"],
        "profile_catalog_digest": projection["profile_catalog_digest"],
        "bundle_lock_digest": projection["bundle_lock_digest"],
    }
    return user_data, catalog, source, request


def test_composition_persists_successor_without_authority_or_execution(composition_context):
    user_data, catalog, source, request = composition_context
    removed_id = request["pack_ids"].pop()
    successor = build_profile_composition(
        catalog, source["profile_id"], canonical_digest(source), request
    )
    assert removed_id not in {row["pack_id"] for row in successor["packs"]}
    assert successor["base"] == source["base"]
    assert successor["shell"] == source["shell"]
    assert successor["state"] == "needs_resolution"
    assert successor["authority_references"] == []
    assert successor["profile_authority_snapshot_digest"] is None
    store = ProfileDefinitionStore(user_data)
    generation = store.snapshot()["generation"]
    stored = store.update_profile(
        source["profile_id"], successor,
        expected_profile_revision=canonical_digest(source),
        expected_store_generation=generation,
    )
    assert stored.parent_revision == canonical_digest(source)
    assert store.get_profile(source["profile_id"]).profile == successor
    with pytest.raises(ProfileDefinitionStoreConflict):
        store.update_profile(
            source["profile_id"], successor,
            expected_profile_revision=canonical_digest(source),
            expected_store_generation=generation,
        )
    assert not (user_data / "authority").exists()
    assert not list(user_data.rglob("*active*"))


def test_composition_adds_verified_pack_with_host_derived_requests(composition_context):
    _, catalog, source, request = composition_context
    available = [pack_id for pack_id, manifest in catalog.packs.items()
                 if pack_id not in request["pack_ids"]
                 and manifest["pack"]["kind"] not in {"base", "shell", "application"}
                 and not manifest["requirements"]["pack_dependencies"]]
    assert available
    added_id = available[0]
    request["pack_ids"].append(added_id)
    successor = build_profile_composition(
        catalog, source["profile_id"], canonical_digest(source), request
    )
    assert next(row for row in successor["packs"] if row["pack_id"] == added_id)[
        "artifact_digest"
    ] == catalog.packs[added_id]["pack"]["artifact_digest"]
    assert successor["authority_references"] == []


@pytest.mark.parametrize("change", [
    {"approved": True},
    {"pack_ids": ["missing-pack"]},
    {"pack_ids": ["shell.tauri.default"]},
    {"pack_ids": []},
    {"pack_ids": [1]},
    {"profile_catalog_digest": "sha256:" + "0" * 64},
    {"bundle_lock_digest": "sha256:" + "0" * 64},
])
def test_composition_rejects_injected_authority_unknown_choices_and_stale_bindings(
    composition_context, change
):
    _, catalog, source, request = composition_context
    with pytest.raises(ValueError):
        build_profile_composition(
            catalog, source["profile_id"], canonical_digest(source), {**request, **change}
        )
