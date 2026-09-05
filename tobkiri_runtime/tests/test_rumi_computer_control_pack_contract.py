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
PACK_ROOT = ECOSYSTEM_ROOT / "rumi_computer_control_pack"
SETUP_PACK_ROOT = ECOSYSTEM_ROOT / "setup_pack"
SETUP_PACK_JSON = SETUP_PACK_ROOT / "rumi_computer_control_pack" / "pack.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(_read(path))


def _pack_files() -> list[Path]:
    return sorted(path for path in PACK_ROOT.rglob("*") if path.is_file())


def test_rumi_computer_control_pack_required_docs_and_assets_exist():
    required = [
        "ecosystem.json",
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "catalog/control_surface_catalog.yaml",
        "catalog/local_gateway_evidence.json",
        "specs/session_observation_spec.json",
        "policies/evidence_first_control.policy.yaml",
        "policies/sandbox_host_boundary.policy.yaml",
        "profiles/macos_desktop_operator.profile.yaml",
        "profiles/local_testing_observer.profile.yaml",
        "profiles/terminal_session_monitor.profile.yaml",
        "prompts/desktop_control.system.md",
        "prompts/evidence_before_action.system.md",
        "prompts/sandbox_host_boundary.system.md",
        "presets/macos_app_workflow.preset.yaml",
        "presets/foreground_app_context.preset.yaml",
        "presets/terminal_observation.preset.yaml",
        "presets/unrestricted_local_testing_contract.preset.yaml",
        "examples/macos_app_navigation.example.yaml",
        "examples/screenshot_keyboard_mouse.example.yaml",
        "examples/terminal_sandbox_observation.example.yaml",
        "metadata/overlap_promotion.yaml",
    ]

    missing = [path for path in required if not (PACK_ROOT / path).is_file()]
    assert missing == []

    ecosystem = _read_json(PACK_ROOT / "ecosystem.json")
    assert ecosystem["pack_id"] == "rumi_computer_control_pack"
    assert ecosystem["pack_identity"] == "rumi:ecosystem/rumi_computer_control_pack"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []
    assert ecosystem["metadata"]["network_policy_details"]["default"] == "none"
    assert ecosystem["required_secrets"] == []


def test_rumi_computer_control_pack_json_and_yaml_assets_parse():
    json_files = [
        PACK_ROOT / "ecosystem.json",
        PACK_ROOT / "catalog" / "local_gateway_evidence.json",
        PACK_ROOT / "specs" / "session_observation_spec.json",
        SETUP_PACK_JSON,
    ]
    yaml_files = sorted(PACK_ROOT.rglob("*.yaml"))

    for path in json_files:
        assert _read_json(path), path

    for path in yaml_files:
        assert yaml.safe_load(_read(path)), path


def test_rumi_computer_control_pack_setup_selector_discovers_overlap_metadata():
    candidates = PackSelector(SETUP_PACK_ROOT).scan_candidates()
    candidate = next(item for item in candidates if item.pack_id == "rumi_computer_control_pack")

    assert candidate.pack_identity == "rumi:ecosystem/rumi_computer_control_pack"
    assert candidate.all_ok_eligible is False
    assert candidate.recommended is False
    assert candidate.risk_level == "medium"
    assert {dep["pack_id"]: dep["version"] for dep in candidate.depends_on} == {
        "defaultspack": ">=2.0.0",
        "rumi_default_tools_pack": ">=1.0.0",
    }
    assert candidate.overlap_policy["defaultspack_grants"] == "do_not_override"
    assert candidate.overlap_policy["actual_computer_use_tool"] == "do_not_override"
    assert candidate.overlap_policy["rumi_browser_automation_pack"]
    assert candidate.overlap_policy["rumi_security_review_pack"]
    assert candidate.overlap_policy["rumi_agent_services_pack"]
    assert candidate.base_pack_promotion["eligible"] is False

    issues = PackSelector(SETUP_PACK_ROOT).validate_candidates(
        installed_packs={
            "defaultspack": {"version": "2.0.0"},
            "rumi_default_tools_pack": {"version": "1.0.0"},
        },
        platform_name="macos",
        python_version="3.13.0",
    )
    assert [issue for issue in issues if issue.get("pack_id") == "rumi_computer_control_pack"] == []


def test_rumi_computer_control_pack_no_secrets_and_contract_text():
    text = "\n".join(_read(path) for path in _pack_files())
    combined = text + "\n" + _read(SETUP_PACK_JSON)

    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|secret|token|password|credential)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    )
    assert not secret_assignment.search(combined)

    for expected in [
        "Required Secrets",
        "None",
        "network is none by default",
        "No executable code",
        "Computer Use",
        "Chrome",
        "rumi_browser_automation_pack",
        "rumi_security_review_pack",
        "rumi_agent_services_pack",
        "base_pack_promotion",
        "eligible: false",
        "unrestricted local testing",
        "not an unrestricted grant",
    ]:
        assert expected in combined
