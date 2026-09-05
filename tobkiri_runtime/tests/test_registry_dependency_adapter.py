"""Profile Resolver dependency closure replacing the legacy Registry adapter."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import (
    BundledCatalog,
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.v4_batch_support import (
    assert_legacy_registry_fails_closed,
    authority_bindings_for_profile,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "sha256:" + "9" * 64


def _catalog() -> BundledCatalog:
    return load_packaged_profile_catalog()


def _approved(catalog: BundledCatalog) -> set[str]:
    return {str(item["pack"]["artifact_digest"]) for item in catalog.packs.values()}


def _resolve(catalog: BundledCatalog):
    return resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests=_approved(catalog),
        authority_snapshot_digest=SNAPSHOT,
        authority_bindings=authority_bindings_for_profile(catalog.profiles["defaults"]),
        security_epoch=1,
    )


def test_legacy_registry_module_is_not_an_adapter() -> None:
    assert_legacy_registry_fails_closed()


def test_profile_resolver_delegates_dependency_order_to_effective_set() -> None:
    resolved = _resolve(_catalog())
    effective_order = [item["identity"] for item in resolved.lock["effective_set"]]
    assert effective_order == [
        "defaults-basepack",
        "shell.tauri.default",
        "defaultspack",
        "rumi_file_inspect_pack",
        "tobkiri_host_pack_control",
        "runtime.tauri.application.default",
        "rumi_ai_gateway_pack",
        "rumi_model_catalog_pack",
        "rumi_model_registry_pack",
        "rumi_ai_pipeline_pack",
        "rumi_provider_adapters_pack",
        "rumi_ai_routing_pack",
        "rumi_ai_stream_pack",
        "rumi_ai_tool_bridge_pack",
        "rumi_ai_usage_pack",
        "rumi_provider_registry_pack",
        "rumi_shell_execute_pack",
        "rumi_git_write_pack",
        "rumi_git_publish_pack",
        "rumi_host_authority_bridge_pack",
        "rumi_command_protocol_pack",
        "rumi_workspace_mount_pack",
        "rumi_shell_policy_pack",
        "rumi_git_read_pack",
    ]
    assert effective_order == [
        resolved.profile["base"]["pack_id"],
        resolved.profile["shell"]["pack_id"],
        *(item["pack_id"] for item in resolved.profile["packs"]),
    ]


def test_profile_resolver_fails_closed_for_missing_dependency() -> None:
    catalog = _catalog()
    profile = copy.deepcopy(catalog.profiles["defaults"])
    profile["packs"] = [
        item for item in profile["packs"] if item["pack_id"] != "rumi_file_inspect_pack"
    ]
    missing = replace(catalog, profiles={"defaults": profile})
    with pytest.raises(ProfileResolutionDenied):
        _resolve(missing)


def test_profile_resolver_fails_closed_for_unapproved_dependency() -> None:
    catalog = _catalog()
    approved = _approved(catalog)
    approved.remove(catalog.packs["rumi_file_inspect_pack"]["pack"]["artifact_digest"])
    with pytest.raises(ProfileResolutionDenied, match="not approved"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings=authority_bindings_for_profile(
                catalog.profiles["defaults"]
            ),
            security_epoch=1,
        )


def test_profile_resolver_fails_closed_for_duplicate_selected_pack() -> None:
    catalog = _catalog()
    duplicate = copy.deepcopy(catalog.packs["defaultspack"])
    duplicate["pack"]["id"] = "duplicate-defaultspack"
    duplicate["pack"]["artifact_digest"] = "sha256:" + "8" * 64
    duplicate_catalog = replace(
        catalog,
        packs={**catalog.packs, "duplicate-defaultspack": duplicate},
    )
    with pytest.raises(ProfileResolutionDenied, match="exactly once"):
        resolve_default_profile(
            duplicate_catalog,
            "defaults",
            approved_artifact_digests=_approved(duplicate_catalog),
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings=authority_bindings_for_profile(
                duplicate_catalog.profiles["defaults"]
            ),
            security_epoch=1,
            additional_pack_ids=("duplicate-defaultspack",),
        )
