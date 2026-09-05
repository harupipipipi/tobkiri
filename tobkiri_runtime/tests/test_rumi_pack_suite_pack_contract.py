"""Contracts for the retired pack-suite compatibility projection."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from ecosystem.setup_pack.pack_selector import PackSelector


pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
LEGACY_PACK_ID = "rumi_pack_suite_pack"
PROJECTION_ID = "tobkiri.profile-content.pack-suite.v1"
PROJECTION_DIR = ROOT / "profile_projections" / "pack-suite"
ALIAS_DIR = ROOT / "ecosystem" / LEGACY_PACK_ID
SETUP_DIR = ROOT / "ecosystem" / "setup_pack" / LEGACY_PACK_ID
AUTHORITY_ARTIFACTS = {
    "ecosystem.json",
    "pack.v4.json",
    "contracts.v4.json",
    "executables.v4.json",
    "artifact-index.v4.json",
}
KNOWN_PROJECTIONS = {
    "tobkiri.profile-content.agent-services.v1",
    "tobkiri.profile-content.local-agent.v1",
    "tobkiri.profile-content.pack-suite.v1",
    "tobkiri.ui-projection.reference.v1",
}


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pack_suite_alias_is_read_only_projection_not_pack_authority() -> None:
    alias = _read_json(ALIAS_DIR / "compatibility-alias.v1.json")

    assert alias["legacy_pack_id"] == LEGACY_PACK_ID
    assert alias["projection_id"] == PROJECTION_ID
    assert alias["artifact_root"] == "profile_projections/pack-suite"
    assert alias["read_only"] is True
    assert alias["runtime_authority"] is False
    assert [name for name in AUTHORITY_ARTIFACTS if (ALIAS_DIR / name).exists()] == []
    assert not SETUP_DIR.exists()
    available = {
        item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()
    }
    assert LEGACY_PACK_ID not in available


def test_pack_suite_projection_assets_and_yaml_parse() -> None:
    required = [
        "catalog/bundles.pack_suite.yaml",
        "catalog/overlap_matrix.pack_suite.yaml",
        "catalog/defaultspack_promotion_matrix.yaml",
        "policies/suite_selection_policy.yaml",
        "profiles/pack_suite_curator.profile.yaml",
        "prompts/pack_suite_curator.system.md",
        "presets/all_agent_capabilities.preset.yaml",
        "presets/defaultspack_candidate_review.preset.yaml",
        "examples/choose_browser_bundle.example.yaml",
        "examples/promote_pack.example.yaml",
    ]
    assert [path for path in required if not (PROJECTION_DIR / path).is_file()] == []
    for path in PROJECTION_DIR.rglob("*.yaml"):
        assert isinstance(_read_yaml(path), dict), path


def test_pack_suite_membership_and_owner_surfaces_are_closed() -> None:
    available_packs = {
        item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()
    }
    bundles = _read_yaml(PROJECTION_DIR / "catalog" / "bundles.pack_suite.yaml")
    bundle_ids = {bundle["bundle_id"] for bundle in bundles["bundles"]}
    assert {
        "coding_operator",
        "research_workspace",
        "browser_operator",
        "personal_agent_os",
        "integration_gateway",
    } <= bundle_ids
    bundle_pack_ids = {
        pack_id for bundle in bundles["bundles"] for pack_id in bundle["packs"]
    }
    bundle_projection_ids = {
        projection_id
        for bundle in bundles["bundles"]
        for projection_id in bundle["content_projections"]
    }
    assert bundle_pack_ids <= available_packs
    assert bundle_projection_ids <= KNOWN_PROJECTIONS

    matrix = _read_yaml(
        PROJECTION_DIR / "catalog" / "overlap_matrix.pack_suite.yaml"
    )
    assert matrix["surfaces"]["browser_semantic_dom"] == "rumi_default_tools_pack"
    assert matrix["surfaces"]["schedules_and_wakeups"] == "defaultspack"
    assert set(matrix["surfaces"].values()) <= available_packs | KNOWN_PROJECTIONS


def test_pack_suite_selection_stays_local_first_and_advisory_only() -> None:
    policy = _read_yaml(
        PROJECTION_DIR / "policies" / "suite_selection_policy.yaml"
    )
    decisions = {item["id"]: item["decision"] for item in policy["rules"]}
    assert policy["local_first"] is True
    assert policy["network_default"] == "deny"
    assert decisions["bundle_is_advisory"] == "no_runtime_install"
    assert decisions["overlap_matrix_does_not_override_pack_metadata"] == "require"

    profile = _read_yaml(
        PROJECTION_DIR / "profiles" / "pack_suite_curator.profile.yaml"
    )
    assert profile["projection_id"] == PROJECTION_ID
    assert profile["policy"] == {
        "local_first": True,
        "network_default": "deny",
        "advisory_only": True,
    }


def test_pack_suite_projection_contains_no_secret_like_literals() -> None:
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?"
        r"[A-Za-z0-9_\-]{12,}"
    )
    checked = [
        path
        for root in (PROJECTION_DIR, ALIAS_DIR)
        for path in root.rglob("*")
        if path.is_file()
    ]
    assert [
        str(path.relative_to(ROOT))
        for path in checked
        if pattern.search(path.read_text(encoding="utf-8"))
    ] == []
