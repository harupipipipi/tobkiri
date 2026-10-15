"""Contracts for the retired agent-services Pack compatibility projection."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from ecosystem.setup_pack.pack_selector import PackSelector


pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
LEGACY_PACK_ID = "rumi_agent_services_pack"
PROJECTION_ID = "tobkiri.profile-content.agent-services.v1"
PROJECTION_DIR = ROOT / "profile_projections" / "agent-services"
ALIAS_DIR = ROOT / "ecosystem" / LEGACY_PACK_ID
SETUP_DIR = ROOT / "ecosystem" / "setup_pack" / LEGACY_PACK_ID
AUTHORITY_ARTIFACTS = {
    "ecosystem.json",
    "pack.v4.json",
    "contracts.v4.json",
    "executables.v4.json",
    "artifact-index.v4.json",
}


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _yaml_files() -> list[Path]:
    return sorted(PROJECTION_DIR.rglob("*.yaml"))


def test_agent_services_alias_is_read_only_projection_not_pack_authority() -> None:
    alias = _read_json(ALIAS_DIR / "compatibility-alias.v1.json")

    assert alias["legacy_pack_id"] == LEGACY_PACK_ID
    assert alias["projection_id"] == PROJECTION_ID
    assert alias["artifact_root"] == "profile_projections/agent-services"
    assert alias["read_only"] is True
    assert alias["runtime_authority"] is False
    assert [name for name in AUTHORITY_ARTIFACTS if (ALIAS_DIR / name).exists()] == []
    assert not SETUP_DIR.exists()
    available = {
        item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()
    }
    assert LEGACY_PACK_ID not in available


def test_agent_services_projection_assets_and_yaml_ids() -> None:
    required = [
        "catalog/capabilities.yaml",
        "coordination/guardrails.yaml",
        "coordination/handoff_contract.yaml",
        "coordination/roles.yaml",
        "coordination/routing.matrix.yaml",
        "coordination/service_workflows.yaml",
        "profiles/service_director.profile.yaml",
        "prompts/service_director.system.md",
    ]
    assert [path for path in required if not (PROJECTION_DIR / path).is_file()] == []

    yaml_files = _yaml_files()
    assert yaml_files
    for path in yaml_files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path
        assert any(key.endswith("_id") or key == "id" for key in data), path


def test_agent_services_profiles_remain_local_first_and_network_denied() -> None:
    profiles = sorted((PROJECTION_DIR / "profiles").glob("*.profile.yaml"))
    assert profiles
    for path in profiles:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        policy = data.get("policy", {})

        assert data["profile_id"].startswith("rumi_agent_services.")
        assert policy.get("local_first") is True
        assert policy.get("network_default") == "deny"


def test_agent_services_membership_and_workflow_owners_are_closed() -> None:
    available_packs = {
        item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()
    }
    capabilities = yaml.safe_load(
        (PROJECTION_DIR / "catalog" / "capabilities.yaml").read_text(
            encoding="utf-8"
        )
    )
    provided_packs = {
        pack_id
        for capability in capabilities["capabilities"].values()
        for pack_id in capability.get("provided_by", [])
    }
    provided_projections = {
        projection_id
        for capability in capabilities["capabilities"].values()
        for projection_id in capability.get("provided_by_projections", [])
    }
    assert provided_packs <= available_packs
    assert provided_projections <= {
        "tobkiri.profile-content.agent-services.v1",
        "tobkiri.profile-content.local-agent.v1",
    }

    roles = yaml.safe_load(
        (PROJECTION_DIR / "coordination" / "roles.yaml").read_text(
            encoding="utf-8"
        )
    )["roles"]
    workflows = yaml.safe_load(
        (PROJECTION_DIR / "coordination" / "service_workflows.yaml").read_text(
            encoding="utf-8"
        )
    )["workflows"]
    owners = {
        stage["owner"]
        for workflow in workflows.values()
        for stage in workflow["stages"]
    }
    assert owners - {"delegated_roles"} <= set(roles)
    assert "service_director" in owners
    assert "quality_reviewer" in owners


def test_agent_services_projection_contains_no_secret_like_literals() -> None:
    secret_patterns = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in [
            r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_-]{12,}",
            r"bearer\s+[A-Za-z0-9._-]{16,}",
            r"sk-[A-Za-z0-9]{16,}",
            r"password\s*[:=]\s*['\"][^'\"]+['\"]",
            r"oauth[_-]?client[_-]?secret\s*[:=]",
        ]
    ]
    checked_files = [
        path
        for root in (PROJECTION_DIR, ALIAS_DIR)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".yaml", ".md"}
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in checked_files
        if any(
            pattern.search(path.read_text(encoding="utf-8"))
            for pattern in secret_patterns
        )
    ]
    assert offenders == []
