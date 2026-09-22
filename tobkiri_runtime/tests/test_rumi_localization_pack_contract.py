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
PACK_ID = "rumi_localization_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_metadata() -> None:
    required = [
        "ecosystem.json",
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "catalog/localization_workflows.yaml",
        "catalog/localization_quality_matrix.yaml",
        "schemas/locale_issue.schema.json",
        "schemas/localization_record.schema.json",
        "policies/protected_terms.policy.yaml",
        "policies/localization_safety.policy.yaml",
        "checklists/localization_review.checklist.yaml",
        "ledgers/localization_evidence_ledger.schema.yaml",
        "templates/localization_handoff.template.md",
        "templates/locale_review_report.template.md",
        "profiles/localization_reviewer.profile.yaml",
        "prompts/translation_reviewer.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/ja_en_product_copy.example.yaml",
        "examples/support_macro_es.example.yaml",
        "examples/app_strings_locale_qa.example.yaml",
        "glossaries/example_glossary.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["connectivity"] == {
        "requires": ["defaultspack"],
        "provides": [],
    }
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert {
        item["pack_id"] for item in ecosystem["metadata"]["optional_integrations"]
    } >= {
        "rumi_document_intelligence_pack",
        "rumi_frontend_design_pack",
        "rumi_workspace_pack",
        "rumi_connector_gateway_pack",
        "rumi_agentic_qa_pack",
        "rumi_model_evals_pack",
    }
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "locale_qa_matrix",
        "terminology_glossary",
        "protected_terms_policy",
        "translation_review_checklist",
        "tone_preservation",
        "translation_issue_triage",
        "localization_handoff_packet",
    }

    indexed = {
        item
        for values in ecosystem["metadata"]["asset_index"].values()
        for item in values
    }
    assert set(required) - {"ecosystem.json"} <= indexed
    assert all((PACK_DIR / path).is_file() for path in indexed)
    pack_files = {
        str(path.relative_to(PACK_DIR)).replace("\\", "/")
        for path in PACK_DIR.rglob("*")
        if path.is_file() and path.name != "executables.v4.json"
    }
    pack_files -= V4_AUTHORITY_ARTIFACTS
    assert pack_files - {"ecosystem.json"} == indexed


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
    assert setup["risk_level"] == "low"
    assert setup["compatibility"]["python"] == ">=3.9"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []

    assert candidate.overlap_policy["document_parsing"] == "handoff_to_rumi_document_intelligence_pack"
    assert candidate.overlap_policy["document_mutation"] == "handoff_to_rumi_document_intelligence_pack"
    assert candidate.overlap_policy["ui_layout_design"] == "handoff_to_rumi_frontend_design_pack"
    assert candidate.overlap_policy["artifact_export"] == "handoff_to_rumi_workspace_pack"
    assert candidate.overlap_policy["external_publishing"] == "handoff_to_rumi_connector_gateway_pack"
    assert candidate.overlap_policy["connector_delivery"] == "handoff_to_rumi_connector_gateway_pack"
    assert candidate.overlap_policy["model_quality_evals"] == "handoff_to_rumi_model_evals_pack"
    assert candidate.overlap_policy["terminology_glossary"] == "owned_by_rumi_localization_pack"
    assert candidate.overlap_policy["protected_terms_policy"] == "owned_by_rumi_localization_pack"
    assert candidate.overlap_policy["tone_preservation"] == "owned_by_rumi_localization_pack"
    assert candidate.overlap_policy["locale_qa_matrix"] == "owned_by_rumi_localization_pack"
    assert candidate.overlap_policy["locale_issue_schema"] == "owned_by_rumi_localization_pack"

    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= {
        "no_executable_runtime",
        "localization_contract_only",
        "requires_locale_policy_selection",
        "requires_user_glossary",
        "requires_protected_term_owner",
        "requires_adjacent_owner_handoff_for_execution",
        "connector_delivery_owned_by_connector_gateway_pack",
    }
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "localization"
    assert candidate.signing["verified"] is True


def test_pack_semantic_contract_assets() -> None:
    issue_schema = read_json(PACK_DIR / "schemas/locale_issue.schema.json")
    record_schema = read_json(PACK_DIR / "schemas/localization_record.schema.json")
    workflows = yaml.safe_load(
        (PACK_DIR / "catalog/localization_workflows.yaml").read_text(encoding="utf-8")
    )["workflows"]
    matrix = yaml.safe_load(
        (PACK_DIR / "catalog/localization_quality_matrix.yaml").read_text(encoding="utf-8")
    )["quality_matrix"]
    policy = yaml.safe_load(
        (PACK_DIR / "policies/localization_safety.policy.yaml").read_text(encoding="utf-8")
    )["policy"]
    protected_policy = yaml.safe_load(
        (PACK_DIR / "policies/protected_terms.policy.yaml").read_text(encoding="utf-8")
    )["protected_terms_policy"]
    checklist = yaml.safe_load(
        (PACK_DIR / "checklists/localization_review.checklist.yaml").read_text(encoding="utf-8")
    )["review_checklist"]
    ledger = yaml.safe_load(
        (PACK_DIR / "ledgers/localization_evidence_ledger.schema.yaml").read_text(encoding="utf-8")
    )["evidence_ledger_schema"]
    template = (PACK_DIR / "templates/localization_handoff.template.md").read_text(encoding="utf-8")
    report_template = (PACK_DIR / "templates/locale_review_report.template.md").read_text(encoding="utf-8")

    assert issue_schema["additionalProperties"] is False
    assert set(issue_schema["required"]) >= {
        "issue_id",
        "locale_pair",
        "source_locale",
        "target_locale",
        "surface",
        "source_segment_id",
        "target_segment_id",
        "severity",
        "category",
        "evidence",
        "recommended_action",
        "handoff",
    }
    assert {"blocker", "major", "minor", "note"} <= set(
        issue_schema["properties"]["severity"]["enum"]
    )
    assert {
        "terminology",
        "protected_term",
        "tone_register",
        "placeholder_integrity",
        "handoff_boundary",
        "evidence_gap",
    } <= set(issue_schema["properties"]["category"]["enum"])
    assert {
        "source_excerpt",
        "target_excerpt",
        "artifact_ref",
        "reason",
        "reviewer",
        "confidence",
    } <= set(issue_schema["properties"]["evidence"]["required"])
    owner_enum = issue_schema["properties"]["handoff"]["properties"]["owner_pack"]["enum"]
    assert "rumi_connector_gateway_pack" in owner_enum

    assert set(record_schema["required"]) >= {
        "record_id",
        "locale_pair",
        "surface",
        "review_state",
        "issues",
        "evidence_refs",
        "handoff_owner",
    }
    assert record_schema["properties"]["evidence_refs"]["minItems"] == 1

    workflow_ids = {item["id"] for item in workflows["items"]}
    assert {
        "locale_issue_triage",
        "glossary_guarded_review",
        "app_string_locale_qa",
        "docs_localization_review",
        "support_copy_tone_review",
    } <= workflow_ids
    assert workflows["default_execution"] == "no_runtime_action"
    assert workflows["evidence_policy"]["adjacent_owner_actions_require_handoff"] is True
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])

    assert {item["id"] for item in matrix["surfaces"]} >= {
        "app_string",
        "documentation",
        "support_copy",
    }
    assert {item["id"] for item in matrix["checks"]} >= {
        "tone_register",
        "protected_terms",
        "terminology",
        "placeholder_integrity",
        "locale_issue_triage",
    }
    assert matrix["completion_rules"]["adjacent_owner_actions_require_handoff"] is True
    protected_check = next(item for item in matrix["checks"] if item["id"] == "protected_terms")
    assert protected_check["blocking"] is True
    assert protected_check["failure_action"] == "block_release"

    assert set(policy["blocked_by_default"]) >= {
        "dropping source segment IDs",
        "changing locked brand terms",
        "sending localized copy through connectors",
    }
    assert (
        policy["non_overlap_boundaries"]["document_intelligence"]
        == "handoff_for_source_extraction_or_document_mutation"
    )
    assert set(protected_policy["blocked_by_default"]) >= {
        "translating locked brand or code identifiers",
        "deleting placeholders or ICU variables",
        "resolving layout truncation by rewriting UI design",
    }
    assert {item["id"] for item in protected_policy["protected_categories"]} >= {
        "brand",
        "product",
        "code",
        "placeholder",
        "legal",
    }

    assert {item["id"] for item in checklist["required_checks"]} >= {
        "terminology",
        "protected_terms",
        "placeholder_integrity",
        "segment_evidence_map",
        "owner_boundary",
    }
    assert checklist["completion_rules"]["no_external_publish_from_this_pack"] is True
    assert ledger["completion_rules"]["every_record_has_evidence"] is True
    assert ledger["completion_rules"]["every_protected_term_has_decision"] is True
    assert {
        "locale_pair",
        "source_segment_id",
        "target_segment_id",
        "protected_terms_checked",
        "glossary_version",
        "issue_ids",
    } <= set(ledger["required_records"])
    assert "Evidence" in template and "Handoff" in template and "Boundary" in template
    assert "Segment Evidence" in report_template and "Protected Terms" in report_template

    glossary = yaml.safe_load((PACK_DIR / "glossaries/example_glossary.yaml").read_text(encoding="utf-8"))[
        "glossary"
    ]
    assert glossary["change_control"] == "issue_record_required"
    assert len(glossary["terms"]) >= 5
    locked = {term["source"] for term in glossary["terms"] if term["locked"]}
    assert {"Rumi", "defaultspack"} <= locked
    for term in glossary["terms"]:
        assert {"term_id", "source", "allowed_targets", "category", "locked", "notes"} <= set(term)


def test_pack_examples_cover_apps_docs_support_and_handoffs() -> None:
    examples = [
        yaml.safe_load(path.read_text(encoding="utf-8"))["example"]
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert {item["surface"] for item in examples} == {
        "app_string",
        "documentation",
        "support_copy",
    }
    assert {item["locale_pair"] for item in examples} >= {"ja-en", "en-es", "en-fr"}
    assert all(item["expected_result"] == "localization_handoff_packet" for item in examples)
    assert all("expected_issue_categories" in item for item in examples)
    assert any("rumi_frontend_design_pack" in item["handoffs"] for item in examples)
    assert any("rumi_document_intelligence_pack" in item["handoffs"] for item in examples)
    assert any("rumi_connector_gateway_pack" in item["handoffs"] for item in examples)
    assert all("sample user request" not in item["intent"] for item in examples)


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md"]
    )
    for expected in ["Required Secrets", "None", "defaultspack", "Handoff", "evidence", "Does Not Provide"]:
        assert expected in docs

    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []

    combined = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    for phrase in ["sample user request", "reviewer_ready_plan", "Complementary owner surface"]:
        assert phrase not in combined
    for boundary in [
        "document parsing",
        "frontend design",
        "workspace export",
        "connector delivery",
    ]:
        assert boundary in combined
