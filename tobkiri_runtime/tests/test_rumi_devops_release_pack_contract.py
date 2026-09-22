from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from ecosystem.setup_pack.pack_selector import PackSelector

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM_ROOT = ROOT / "ecosystem"
PACK_ROOT = ECOSYSTEM_ROOT / "rumi_devops_release_pack"
SETUP_PACK_ROOT = ECOSYSTEM_ROOT / "setup_pack"
SETUP_PACK_JSON = SETUP_PACK_ROOT / "rumi_devops_release_pack" / "pack.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pack_files() -> list[Path]:
    return sorted(path for path in PACK_ROOT.rglob("*") if path.is_file())


def test_rumi_devops_release_pack_required_docs_and_assets_exist():
    required = [
        "ecosystem.json",
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "catalog/release_gate_catalog.yaml",
        "catalog/devops_operations_catalog.json",
        "profiles/ci_triage.profile.yaml",
        "profiles/release_manager.profile.yaml",
        "profiles/rollback_planner.profile.yaml",
        "prompts/ci_triage.system.md",
        "prompts/release_evidence.system.md",
        "prompts/rollback_runbook.system.md",
        "presets/github_actions_triage.preset.yaml",
        "presets/cloudflare_workers_release.preset.yaml",
        "presets/local_first_release_gate.preset.yaml",
        "presets/incident_rollback.preset.yaml",
        "examples/ci_failure_triage.example.yaml",
        "examples/release_notes_evidence.example.yaml",
        "examples/rollback_plan.example.yaml",
        "metadata/overlap_promotion.yaml",
    ]

    missing = [path for path in required if not (PACK_ROOT / path).is_file()]
    assert missing == []

    ecosystem = _read_json(PACK_ROOT / "ecosystem.json")
    assert ecosystem["pack_id"] == "rumi_devops_release_pack"
    assert ecosystem["pack_identity"] == "rumi:ecosystem/rumi_devops_release_pack"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []
    assert ecosystem["metadata"]["legacy_annotations"]["network_policy"]["default"] == "none"

    manifest = _read_json(PACK_ROOT / "pack.v4.json")
    setup = _read_json(SETUP_PACK_JSON)
    setup_dependencies = {
        item["pack_id"]: item["version"] for item in setup["depends_on"]
    }
    assert setup_dependencies == {
        "defaultspack": ">=2.0.0",
        "rumi_default_tools_pack": ">=1.0.0",
    }
    assert manifest["requirements"]["pack_dependencies"] == {
        "rumi_default_tools_pack": ">=1.0.0",
    }
    assert manifest["requirements"]["network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert manifest["requirements"]["secrets"] == []


def test_rumi_devops_release_pack_json_and_yaml_assets_parse():
    json_files = [PACK_ROOT / "ecosystem.json", PACK_ROOT / "catalog" / "devops_operations_catalog.json", SETUP_PACK_JSON]
    yaml_files = sorted(PACK_ROOT.rglob("*.yaml"))

    for path in json_files:
        assert _read_json(path), path

    for path in yaml_files:
        assert yaml.safe_load(_read(path)), path


def test_rumi_devops_release_pack_setup_selector_discovers_policy_metadata():
    candidates = PackSelector(SETUP_PACK_ROOT).scan_candidates()
    candidate = next(item for item in candidates if item.pack_id == "rumi_devops_release_pack")

    assert candidate.pack_identity == "rumi:ecosystem/rumi_devops_release_pack"
    assert candidate.all_ok_eligible is False
    assert candidate.recommended is False
    assert candidate.risk_level == "medium"
    assert {dep["pack_id"]: dep["version"] for dep in candidate.depends_on} == {
        "defaultspack": ">=2.0.0",
        "rumi_default_tools_pack": ">=1.0.0",
    }
    assert candidate.overlap_policy["default"] == "prefer_explicit_pack_namespace"
    assert "rumi_code_ide_pack" in candidate.overlap_policy["complements"]
    assert "rumi_agent_services_pack" in candidate.overlap_policy["complements"]
    assert candidate.defaultspack_promotion["eligible"] is False

    issues = PackSelector(SETUP_PACK_ROOT).validate_candidates(
        installed_packs={
            "defaultspack": {"version": "2.0.0"},
            "rumi_default_tools_pack": {"version": "1.0.0"},
        },
        platform_name="macos",
        python_version="3.13.0",
    )
    assert [issue for issue in issues if issue.get("pack_id") == "rumi_devops_release_pack"] == []


def test_rumi_devops_release_pack_no_secrets_and_local_first_contract():
    text = "\n".join(_read(path) for path in _pack_files())
    setup_text = _read(SETUP_PACK_JSON)
    combined = text + "\n" + setup_text

    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|secret|token|password|credential)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    )
    assert not secret_assignment.search(combined)

    for expected in [
        "Required Secrets",
        "None",
        "network is none by default",
        "No executable code",
        "rumi_code_ide_pack",
        "rumi_agent_services_pack",
        "defaultspack_promotion",
        "eligible: false",
    ]:
        assert expected in combined
