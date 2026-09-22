from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from core_runtime.setup_pack import SetupPackManager
from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_frontend_design_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_metadata() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/frontend_workflows.yaml",
        "catalog/design_system_fit_rubric.yaml",
        "catalog/responsive_qa_matrix.yaml",
        "policies/frontend_quality.policy.yaml",
        "schemas/component_acceptance.schema.yaml",
        "checklists/component_acceptance.checklist.yaml",
        "profiles/frontend_design_reviewer.profile.yaml",
        "prompts/frontend_design_reviewer.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/dashboard_review.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert "runtime" not in ecosystem
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert set(ecosystem["vocabulary"]["types"]) >= {
        "frontend_design",
        "catalog",
        "policy",
        "schema",
        "checklist",
    }
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "frontend_briefs",
        "design_system_fit",
        "responsive_visual_qa",
        "component_acceptance_criteria",
        "design_system_fit_rubric",
        "responsive_qa_matrix",
        "component_acceptance_schema",
    }
    asset_index = ecosystem["metadata"]["asset_index"]
    assert set(asset_index) == {
        "manifest",
        "readme",
        "docs",
        "catalog",
        "policy",
        "profile",
        "prompt",
        "preset",
        "example",
        "schema",
        "checklist",
        "template",
    }
    indexed_assets = {asset for assets in asset_index.values() for asset in assets}
    actual_assets = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file() and path.name != "executables.v4.json"
    }
    actual_assets -= V4_AUTHORITY_ARTIFACTS
    assert indexed_assets == actual_assets


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == []
    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []
    assert candidate.overlap_policy["code_edits"] == "handoff_to_rumi_code_ide_pack"
    assert candidate.overlap_policy["design_system_fit_rubric"] == "owned_by_rumi_frontend_design_pack"
    assert candidate.overlap_policy["responsive_qa_matrix"] == "owned_by_rumi_frontend_design_pack"
    assert candidate.overlap_policy["component_acceptance_schema"] == "owned_by_rumi_frontend_design_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert "declarative planning and QA contract pack" in candidate.defaultspack_promotion["reason"]
    assert {
        "no_executable_runtime",
        "design_contract_only",
        "requires_code_pack_for_implementation",
        "requires_browser_pack_for_rendered_evidence",
    } <= set(candidate.defaultspack_promotion["promotion_blockers"])
    assert {
        "successful_component_acceptance_cycles",
        "browser_qa_evidence_across_viewports",
    } <= set(candidate.defaultspack_promotion["promotion_evidence_required"])
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "frontend"
    assert candidate.signing["verified"] is True


def test_setup_pack_manager_installs_pack_without_selection_dependencies(tmp_path: Path) -> None:
    manager = SetupPackManager(
        root=ROOT / "ecosystem" / "setup_pack",
        selection_file=tmp_path / "setup_pack_selection.json",
        ecosystem_dir=ROOT / "ecosystem",
    )
    result = manager.install(PACK_ID)
    assert result["success"] is True
    assert result["installed_setup_pack_ids"] == [PACK_ID]
    assert result["installed_target_pack_ids"] == [PACK_ID]
    assert result["active_setup_pack_id"] == PACK_ID
    assert result["active_target_pack_id"] == PACK_ID
    assert result["skipped_all_ok_setup_pack_ids"] == [PACK_ID]


def test_pack_specific_quality_assets_are_semantic() -> None:
    rubric = yaml.safe_load((PACK_DIR / "catalog/design_system_fit_rubric.yaml").read_text(encoding="utf-8"))
    assert rubric["minimum_ready_score"] >= 16
    assert {"product_density", "component_idioms", "visual_language", "accessibility_and_states"} <= set(
        rubric["criteria"]
    )
    assert "implementation_handoff_owner_named" in rubric["readiness_rules"]["ready_requires"]

    matrix = yaml.safe_load((PACK_DIR / "catalog/responsive_qa_matrix.yaml").read_text(encoding="utf-8"))
    viewports = {row["id"]: row for row in matrix["viewport_rows"]}
    assert {"mobile_360", "mobile_390", "tablet_768", "desktop_1440"} <= set(viewports)
    assert matrix["evidence_requirements"]["screenshot_required"] is True
    assert matrix["defect_classes"]["layout_overlap"] == "handoff_to_rumi_code_ide_pack"

    schema = yaml.safe_load((PACK_DIR / "schemas/component_acceptance.schema.yaml").read_text(encoding="utf-8"))
    assert {"component_id", "state_matrix", "responsive_requirements", "owner_handoffs"} <= set(
        schema["required_fields"]
    )
    assert set(schema["state_matrix_required_states"]) >= {"loading", "empty", "error", "focused", "mobile"}
    assert schema["acceptance_rules"]["no_runtime_code_in_this_pack"] is True

    checklist = yaml.safe_load((PACK_DIR / "checklists/component_acceptance.checklist.yaml").read_text(encoding="utf-8"))
    assert len(checklist["blocking_checks"]) >= 5
    assert {item["id"] for item in checklist["blocking_checks"]} >= {
        "goal_and_surface",
        "design_system_fit",
        "state_matrix",
        "responsive_qa",
        "handoff_owner",
    }

    examples = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert all("Example workflow" not in item["request"] for item in examples)
    assert any("settings table" in item["request"] for item in examples)
    assert any("responsive_qa_matrix_rows" in item["expected_outputs"] for item in examples)

    presets = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))
    ]
    assert all("purpose" in item and item["required_assets"] for item in presets)
    assert any("design-system fit" in item["purpose"] for item in presets)


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence"]:
        assert expected in docs
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
    skeleton_phrases = ["Example workflow", "Complementary owner surface", "declarative_pack"]
    all_text = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    assert [phrase for phrase in skeleton_phrases if phrase in all_text] == []
