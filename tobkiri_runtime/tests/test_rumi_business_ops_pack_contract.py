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
PACK_ID = "rumi_business_ops_pack"
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
        "catalog/business_ops_workflows.yaml",
        "catalog/workflow_taxonomy.yaml",
        "policies/business_ops_safety.policy.yaml",
        "policies/approval_risk_matrix.policy.yaml",
        "ledgers/business_ops_handoff_ledger.schema.yaml",
        "checklists/operator_handoff.checklist.yaml",
        "profiles/business_ops_operator.profile.yaml",
        "prompts/business_ops_operator.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/support_triage.example.yaml",
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
        "business_ops",
        "catalog",
        "policy",
        "checklist",
        "ledger",
    }
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "sales_support_workflows",
        "marketing_briefs",
        "procurement_decision_memos",
        "crm_hygiene_contracts",
        "workflow_taxonomy",
        "approval_risk_matrix",
        "business_ops_handoff_ledger",
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
        "ledger",
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
    assert candidate.overlap_policy["connectors"] == "handoff_to_rumi_connector_gateway_pack"
    assert candidate.overlap_policy["workflow_taxonomy"] == "owned_by_rumi_business_ops_pack"
    assert candidate.overlap_policy["approval_risk_matrix"] == "owned_by_rumi_business_ops_pack"
    assert candidate.overlap_policy["handoff_ledger"] == "owned_by_rumi_business_ops_pack"
    assert candidate.base_pack_promotion["eligible"] is False
    assert "declarative planning, approval, and handoff pack" in candidate.base_pack_promotion["reason"]
    assert {
        "no_executable_runtime",
        "business_contract_only",
        "requires_connector_pack_for_external_actions",
        "requires_scheduler_pack_for_followups",
    } <= set(candidate.base_pack_promotion["promotion_blockers"])
    assert {
        "successful_approval_gated_business_workflows",
        "connector_handoff_audit_evidence",
    } <= set(candidate.base_pack_promotion["promotion_evidence_required"])
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "business-ops"
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


def test_pack_specific_business_ops_assets_are_semantic() -> None:
    taxonomy = yaml.safe_load((PACK_DIR / "catalog/workflow_taxonomy.yaml").read_text(encoding="utf-8"))
    classes = taxonomy["workflow_classes"]
    assert {"support_triage", "sales_followup", "marketing_campaign_brief", "procurement_comparison", "crm_hygiene"} <= set(
        classes
    )
    assert classes["procurement_comparison"]["risk_default"] == "high"
    assert classes["sales_followup"]["schedule_handoff"] == "rumi_workflow_scheduler_pack"
    assert taxonomy["classification_rules"]["external_system_mutation_handoff_required"] is True

    risk = yaml.safe_load((PACK_DIR / "policies/approval_risk_matrix.policy.yaml").read_text(encoding="utf-8"))
    matrix = risk["approval_matrix"]
    assert {"external_message_send", "crm_field_update", "scheduled_followup", "procurement_recommendation"} <= set(
        matrix
    )
    assert matrix["crm_field_update"]["approval_required"] is True
    assert "contract_signature" in risk["blocked_without_explicit_user_instruction"]
    assert risk["review_rules"]["connector_actions_never_executed_by_this_pack"] is True

    ledger = yaml.safe_load((PACK_DIR / "ledgers/business_ops_handoff_ledger.schema.yaml").read_text(encoding="utf-8"))
    assert {"workflow_id", "risk_level", "approval_required", "owner_pack", "blocked_actions"} <= set(
        ledger["required_records"]
    )
    assert ledger["completion_rules"]["owner_pack_required_for_execution"] is True

    checklist = yaml.safe_load((PACK_DIR / "checklists/operator_handoff.checklist.yaml").read_text(encoding="utf-8"))
    assert len(checklist["blocking_checks"]) >= 5
    assert {item["id"] for item in checklist["blocking_checks"]} >= {
        "workflow_classified",
        "evidence_present",
        "approval_risk_scored",
        "execution_owner_named",
        "blocked_actions_checked",
    }

    examples = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert all("Example workflow" not in item["request"] for item in examples)
    assert any("procurement decision memo" in item["request"] for item in examples)
    assert any("connector_gateway_handoff" in item["expected_outputs"] for item in examples)

    presets = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))
    ]
    assert all("purpose" in item and item["required_assets"] for item in presets)
    assert any("approval risk gates" in item["purpose"] for item in presets)


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
