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
PACK_ID = "rumi_browser_form_operator_pack"
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
        "catalog/form_action_recipes.yaml",
        "specs/form_taxonomy.yaml",
        "specs/field_risk_classification.yaml",
        "specs/semantic_dom_dependency.yaml",
        "specs/submission_review_checklist.yaml",
        "evidence/submission_receipt_evidence.schema.yaml",
        "policies/form_action_safety.policy.yaml",
        "profiles/browser_form_operator.profile.yaml",
        "prompts/browser_form_operator.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/contact_form_fill.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert "vocabulary" in ecosystem
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["registers_tools"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "safe_field_filling",
        "semantic_form_fields",
        "field_risk_classification",
        "semantic_dom_dependency",
        "submission_receipt_evidence",
    }
    asset_index = ecosystem["metadata"]["asset_index"]
    assert "catalog/form_action_recipes.yaml" in asset_index["catalogs"]
    assert "specs/form_taxonomy.yaml" in asset_index["specs"]
    assert "specs/submission_review_checklist.yaml" in asset_index["checklists"]
    assert "policies/form_action_safety.policy.yaml" in asset_index["policies"]
    assert "evidence/submission_receipt_evidence.schema.yaml" in asset_index["evidence_ledgers"]
    indexed_assets = {asset for assets in asset_index.values() for asset in assets}
    assert {'README.md', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'profiles/browser_form_operator.profile.yaml', 'prompts/browser_form_operator.system.md'} <= indexed_assets
    assert all((PACK_DIR / asset).is_file() for asset in indexed_assets)
    assert set(asset_index["examples"]) == {
        "examples/contact_form_fill.example.yaml",
        "examples/checkout_review.example.yaml",
        "examples/multi_step_application.example.yaml",
    }


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "high"
    assert candidate.risk_level == "high"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["semantic_dom_collection"] == "handoff_to_rumi_browser_element_pack"
    assert candidate.base_pack_promotion["eligible"] is False
    assert "Browser Form Operator" in candidate.base_pack_promotion["reason"]
    assert "no_browser_execution_runtime" in candidate.base_pack_promotion["promotion_blockers"]
    assert "submission_receipt_evidence_cases" in candidate.base_pack_promotion["promotion_evidence_required"]
    assert candidate.marketplace["id"].startswith("rumi.")
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "browser-safety"
    assert candidate.signing["verified"] is True


def test_browser_form_review_assets_are_substantial() -> None:
    taxonomy = yaml.safe_load((PACK_DIR / "specs/form_taxonomy.yaml").read_text(encoding="utf-8"))
    risk = yaml.safe_load((PACK_DIR / "specs/field_risk_classification.yaml").read_text(encoding="utf-8"))
    semantic = yaml.safe_load((PACK_DIR / "specs/semantic_dom_dependency.yaml").read_text(encoding="utf-8"))
    submission = yaml.safe_load((PACK_DIR / "specs/submission_review_checklist.yaml").read_text(encoding="utf-8"))
    receipt = yaml.safe_load((PACK_DIR / "evidence/submission_receipt_evidence.schema.yaml").read_text(encoding="utf-8"))
    workflows = yaml.safe_load((PACK_DIR / "catalog/form_action_recipes.yaml").read_text(encoding="utf-8"))

    assert {"informational_contact", "account_profile", "commerce_checkout", "legal_or_government", "public_or_external_send"} <= set(taxonomy["form_categories"])
    assert taxonomy["form_categories"]["commerce_checkout"]["submit_policy"] == "security_review_required"
    assert {"low", "medium", "high", "irreversible"} <= set(risk["risk_levels"])
    assert risk["risk_levels"]["high"]["default_policy"] == "handoff_to_security_review_before_fill"
    assert "field_selector_map" in semantic["required_semantic_evidence"]
    assert "validation_message_map" in semantic["required_semantic_evidence"]
    assert semantic["fallback_policy"]["missing_semantic_dom"] == "handoff_to_rumi_browser_element_pack"
    assert "field_risk_map_reviewed" in submission["pre_submit_required"]
    assert submission["approval_gates"]["irreversible_submit"] == "explicit_confirmation_required_each_time"
    assert receipt["redaction_policy"]["redact_sensitive_values"] is True
    assert "block_high_risk_submission" in workflows["workflows"]


def test_browser_form_submit_paths_require_explicit_approval_and_receipt_evidence() -> None:
    submission = yaml.safe_load((PACK_DIR / "specs/submission_review_checklist.yaml").read_text(encoding="utf-8"))
    receipt = yaml.safe_load((PACK_DIR / "evidence/submission_receipt_evidence.schema.yaml").read_text(encoding="utf-8"))
    workflows = yaml.safe_load((PACK_DIR / "catalog/form_action_recipes.yaml").read_text(encoding="utf-8"))

    assert submission["submit_path_policy"]["rule"] == "no_submit_path_without_explicit_approval_and_receipt_evidence"
    assert {"user_confirmation_ref", "pre_submit_review", "receipt_evidence_plan"} <= set(submission["submit_path_policy"]["requires"])
    assert "user_confirmation_ref" in receipt["required_fields"]
    assert "submitted_with_confirmation" in receipt["submission_state_values"]

    submit_workflow = workflows["workflows"]["record_submission_receipt"]
    assert "user_confirmation_ref" in submit_workflow["requires_evidence"]
    assert "receipt_evidence_plan" in submit_workflow["requires_evidence"]
    assert "no_submit_without_explicit_approval_and_receipt_evidence" in submit_workflow["acceptance_gates"]


def test_browser_form_examples_are_specific_not_placeholders() -> None:
    checked_text = "\n".join(path.read_text(encoding="utf-8") for path in (PACK_DIR / "examples").glob("*.yaml"))
    assert "sample user request" not in checked_text
    assert "reviewer_ready_plan" not in checked_text


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence"]:
        assert expected in docs
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
