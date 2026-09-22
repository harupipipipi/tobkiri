from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_subagent_pr_manager_pack"
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
        "catalog/subagent_pr_workflows.yaml",
        "catalog/subagent_routing_matrix.yaml",
        "catalog/pr_acceptance_rubric.yaml",
        "policies/subagent_pr_governance.policy.yaml",
        "templates/subagent_assignment_brief.template.yaml",
        "ledgers/pr_evidence_ledger.schema.yaml",
        "checklists/reviewer_handoff.checklist.yaml",
        "profiles/subagent_pr_manager.profile.yaml",
        "prompts/subagent_pr_manager.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/one_pr_per_subagent.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert set(ecosystem["vocabulary"]["types"]) >= {"governance", "catalog", "policy", "checklist", "ledger", "template"}
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "subagent_assignment",
        "branch_pr_ledger",
        "subagent_routing_matrix",
        "pr_acceptance_rubric",
        "evidence_ledger",
    }
    asset_index = ecosystem["metadata"]["asset_index"]
    assert set(asset_index) == {"readme", "docs", "catalog", "policy", "spec", "checklist", "ledger", "template", "presets", "profiles", "prompts", "example"}
    indexed_assets = {asset for assets in asset_index.values() for asset in assets}
    assert indexed_assets == {
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "catalog/subagent_pr_workflows.yaml",
        "catalog/subagent_routing_matrix.yaml",
        "catalog/pr_acceptance_rubric.yaml",
        "policies/subagent_pr_governance.policy.yaml",
        "checklists/reviewer_handoff.checklist.yaml",
        "ledgers/pr_evidence_ledger.schema.yaml",
        "templates/subagent_assignment_brief.template.yaml",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "profiles/subagent_pr_manager.profile.yaml",
        "prompts/subagent_pr_manager.system.md",
        "examples/one_pr_per_subagent.example.yaml",
        "examples/merge_readiness_board.example.yaml",
        "examples/review_handoff.example.yaml"
    }
    assert all((PACK_DIR / asset).is_file() for asset in indexed_assets)


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
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []
    assert candidate.overlap_policy["code_edits"] == "handoff_to_rumi_code_ide_pack"
    assert candidate.overlap_policy["subagent_routing_matrix"] == "owned_by_rumi_subagent_pr_manager_pack"
    assert candidate.overlap_policy["pr_acceptance_rubric"] == "owned_by_rumi_subagent_pr_manager_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert "governance overlay" in candidate.defaultspack_promotion["reason"]
    assert {
        "no_executable_runtime",
        "governance_only_pack",
        "requires_external_owner_for_code_edits",
    } <= set(candidate.defaultspack_promotion["promotion_blockers"])
    assert {
        "multiple_successful_pack_pr_cycles",
        "observability_evidence_for_review_outcomes",
    } <= set(candidate.defaultspack_promotion["promotion_evidence_required"])
    assert candidate.marketplace["id"].startswith("rumi.")
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "governance"
    assert candidate.signing["verified"] is True


def test_pack_quality_assets_have_subagent_and_merge_semantics() -> None:
    routing = yaml.safe_load((PACK_DIR / "catalog/subagent_routing_matrix.yaml").read_text(encoding="utf-8"))[
        "routing_matrix"
    ]
    assert routing["default_policy"]["repeated_subagent_use_required"] is True
    assert routing["default_policy"]["owner_reviewer_fallback_required"] is True
    assert len(routing["lanes"]) >= 3
    for lane in routing["lanes"].values():
        assert {"owner_subagent", "reviewer_subagent", "fallback_subagent", "required_evidence"} <= set(lane)
        assert len(lane["required_evidence"]) >= 3
    role_names = {
        lane[key]
        for lane in routing["lanes"].values()
        for key in ("owner_subagent", "reviewer_subagent", "fallback_subagent")
    }
    assert len(role_names) >= 5

    rubric = yaml.safe_load((PACK_DIR / "catalog/pr_acceptance_rubric.yaml").read_text(encoding="utf-8"))[
        "acceptance_rubric"
    ]
    assert rubric["scoring"]["minimum_merge_readiness"] == "strong"
    assert {"scope_control", "declarative_depth", "subagent_repetition", "setup_policy", "verification"} <= set(
        rubric["criteria"]
    )
    assert "defaultspack_promotion_eligible" in rubric["criteria"]["setup_policy"]["blocking_failures"]

    ledger = yaml.safe_load((PACK_DIR / "ledgers/pr_evidence_ledger.schema.yaml").read_text(encoding="utf-8"))[
        "pr_evidence_ledger_schema"
    ]
    assert ledger["completion_rules"]["at_least_three_subagent_roles"] is True
    assert {"validation_commands", "validation_results", "handoff_decisions"} <= set(ledger["required_records"])

    checklist = yaml.safe_load((PACK_DIR / "checklists/reviewer_handoff.checklist.yaml").read_text(encoding="utf-8"))[
        "reviewer_handoff_checklist"
    ]
    blocking_checks = [item for item in checklist["required_checks"] if item["blocking"]]
    assert len(blocking_checks) >= 5
    assert {item["id"] for item in blocking_checks} >= {"scope_guard", "subagent_repetition", "verification"}

    template = yaml.safe_load(
        (PACK_DIR / "templates/subagent_assignment_brief.template.yaml").read_text(encoding="utf-8")
    )["assignment_brief_template"]
    assert {"owner_subagent", "reviewer_subagent", "fallback_subagent"} <= set(template["required_fields"])
    assert "Contract tests assert semantic quality, not only file existence." in template["quality_bar"]

    examples = [
        yaml.safe_load(path.read_text(encoding="utf-8"))["example"]
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert len(examples) == 3
    assert all("sample user request" not in item["intent"] for item in examples)
    assert all(item["expected_result"] != "reviewer_ready_plan" for item in examples)
    assert any("merge readiness board" in item["intent"] for item in examples)
    assert any("assignment_brief" in item["expected_result"] for item in examples)

    presets = [
        yaml.safe_load(path.read_text(encoding="utf-8"))["preset"]
        for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))
    ]
    assert all("purpose" in item and item["required_assets"] for item in presets)
    assert any("acceptance rubric" in item["purpose"] for item in presets)


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence"]:
        assert expected in docs
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
    skeleton_phrases = ["sample user request", "reviewer_ready_plan", "Complementary owner surface"]
    all_text = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    assert [phrase for phrase in skeleton_phrases if phrase in all_text] == []
