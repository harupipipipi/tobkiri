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
PACK_ID = "rumi_agentic_qa_pack"
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
        "catalog/agentic_qa_scenarios.yaml",
        "catalog/qa_routing_matrix.yaml",
        "catalog/acceptance_rubric.yaml",
        "catalog/scenario_catalog.yaml",
        "policies/agentic_qa_gate.policy.yaml",
        "ledgers/qa_evidence_ledger.schema.yaml",
        "checklists/qa_replay.checklist.yaml",
        "templates/regression_triage_report.template.yaml",
        "profiles/agentic_qa_reviewer.profile.yaml",
        "prompts/agentic_qa_reviewer.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/agent_regression_case.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert set(ecosystem["vocabulary"]["types"]) >= {"qa", "catalog", "policy", "checklist", "ledger", "template"}
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "acceptance_matrix",
        "scenario_replay",
        "qa_routing_matrix",
        "acceptance_rubric",
        "qa_evidence_ledger",
        "scenario_catalog",
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
        "catalog/agentic_qa_scenarios.yaml",
        "catalog/qa_routing_matrix.yaml",
        "catalog/acceptance_rubric.yaml",
        "catalog/scenario_catalog.yaml",
        "policies/agentic_qa_gate.policy.yaml",
        "checklists/qa_replay.checklist.yaml",
        "ledgers/qa_evidence_ledger.schema.yaml",
        "templates/regression_triage_report.template.yaml",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "profiles/agentic_qa_reviewer.profile.yaml",
        "prompts/agentic_qa_reviewer.system.md",
        "examples/agent_regression_case.example.yaml",
        "examples/cross_pack_acceptance.example.yaml",
        "examples/failure_triage.example.yaml"
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
    assert candidate.overlap_policy["model_scoring"] == "handoff_to_rumi_model_evals_pack"
    assert candidate.overlap_policy["qa_routing_matrix"] == "owned_by_rumi_agentic_qa_pack"
    assert candidate.overlap_policy["acceptance_rubric"] == "owned_by_rumi_agentic_qa_pack"
    assert candidate.base_pack_promotion["eligible"] is False
    assert "acceptance and triage contract pack" in candidate.base_pack_promotion["reason"]
    assert {
        "no_executable_runtime",
        "qa_contract_only",
        "requires_external_owner_for_browser_replay",
        "requires_external_owner_for_model_scoring",
    } <= set(candidate.base_pack_promotion["promotion_blockers"])
    assert {
        "stable_replay_integration_surface",
        "observability_evidence_for_scenario_runs",
    } <= set(candidate.base_pack_promotion["promotion_evidence_required"])
    assert candidate.marketplace["id"].startswith("rumi.")
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "quality"
    assert candidate.signing["verified"] is True


def test_pack_quality_assets_have_replay_and_subagent_semantics() -> None:
    routing = yaml.safe_load((PACK_DIR / "catalog/qa_routing_matrix.yaml").read_text(encoding="utf-8"))[
        "qa_routing_matrix"
    ]
    assert routing["default_policy"]["repeated_subagent_use_required"] is True
    assert len(routing["default_policy"]["specialist_subagents_required"]) >= 5
    assert len(routing["lanes"]) >= 4
    for lane in routing["lanes"].values():
        assert {
            "scenario_runner",
            "adversarial_tester",
            "regression_analyst",
            "evidence_reviewer",
            "pack_handoff_coordinator",
            "owner_pack",
            "required_evidence",
        } <= set(lane)
        assert len(lane["required_evidence"]) >= 4
    owner_packs = {lane["owner_pack"] for lane in routing["lanes"].values()}
    assert {
        "rumi_model_evals_pack",
        "rumi_browser_automation_pack",
        "rumi_security_review_pack",
        "rumi_observability_pack",
    } <= owner_packs

    rubric = yaml.safe_load((PACK_DIR / "catalog/acceptance_rubric.yaml").read_text(encoding="utf-8"))[
        "acceptance_rubric"
    ]
    assert rubric["scoring"]["minimum_acceptance"] == "pass_with_notes"
    assert {"scenario_reproducibility", "evidence_quality", "subagent_coverage", "owner_handoff"} <= set(
        rubric["criteria"]
    )
    assert "single_agent_review_only" in rubric["criteria"]["subagent_coverage"]["blocking_failures"]

    scenarios = yaml.safe_load((PACK_DIR / "catalog/scenario_catalog.yaml").read_text(encoding="utf-8"))[
        "scenario_catalog"
    ]
    assert scenarios["scenario_defaults"]["network_default"] == "none"
    assert len(scenarios["scenarios"]) >= 4
    for scenario in scenarios["scenarios"].values():
        assert {"goal", "owner_pack", "expected_observation", "required_subagents"} <= set(scenario)
        assert len(scenario["required_subagents"]) >= 3

    ledger = yaml.safe_load((PACK_DIR / "ledgers/qa_evidence_ledger.schema.yaml").read_text(encoding="utf-8"))[
        "qa_evidence_ledger_schema"
    ]
    assert ledger["completion_rules"]["at_least_four_subagent_roles"] is True
    assert {"expected_observation", "actual_observation", "triage_owner_pack"} <= set(ledger["required_records"])

    checklist = yaml.safe_load((PACK_DIR / "checklists/qa_replay.checklist.yaml").read_text(encoding="utf-8"))[
        "qa_replay_checklist"
    ]
    assert {item["id"] for item in checklist["required_checks"]} >= {
        "scenario_defined",
        "subagent_coverage",
        "expected_actual_pair",
        "owner_handoff",
    }

    template = yaml.safe_load(
        (PACK_DIR / "templates/regression_triage_report.template.yaml").read_text(encoding="utf-8")
    )["regression_triage_report_template"]
    assert {"scenario_id", "triage_owner_pack", "expected_observation", "actual_observation"} <= set(
        template["required_fields"]
    )
    assert {"model_behavior", "browser_e2e", "unsafe_behavior", "pack_contract"} <= set(
        template["suspected_regression_types"]
    )

    examples = [
        yaml.safe_load(path.read_text(encoding="utf-8"))["example"]
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert len(examples) == 3
    assert all("sample user request" not in item["intent"] for item in examples)
    assert all(item["expected_result"] != "reviewer_ready_plan" for item in examples)
    assert any("expected observation" in item["intent"] for item in examples)
    assert any("acceptance_matrix_row" in item["expected_result"] for item in examples)

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
