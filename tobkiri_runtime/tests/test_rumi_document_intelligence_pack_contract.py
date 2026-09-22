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
PACK_ID = "rumi_document_intelligence_pack"
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
        "catalog/document_intelligence_workflows.yaml",
        "catalog/citation_page_span_schema.json",
        "catalog/redline_review_matrix.yaml",
        "catalog/document_types.yaml",
        "catalog/redline_operation_tests.yaml",
        "schemas/citation_trace.schema.json",
        "schemas/redline_handoff.schema.json",
        "policies/document_privacy.policy.yaml",
        "policies/citation_privacy_review.policy.yaml",
        "coordination/subagent_review_roster.yaml",
        "profiles/document_intelligence_reviewer.profile.yaml",
        "prompts/document_intelligence_reviewer.system.md",
        "prompts/citation_redline_privacy_reviewer.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/contract_clause_review.example.yaml",
        "examples/page_span_citation_audit.example.yaml",
        "examples/redline_privacy_review.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["vocabulary"]["types"]
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "pdf_extraction",
        "contract_clause_review",
        "page_span_mapping",
        "privacy_review",
        "multi_pass_specialist_review",
    }
    indexed = {
        item
        for values in ecosystem["metadata"]["asset_index"].values()
        for item in values
    }
    for path in required:
        assert path in indexed or path in {"ecosystem.json"}


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["slide_sheet_doc_creation"] == "handoff_to_rumi_workspace_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert "reason" in candidate.defaultspack_promotion
    assert "promotion_blockers" in candidate.defaultspack_promotion
    assert "promotion_evidence_required" in candidate.defaultspack_promotion
    assert candidate.marketplace["id"].startswith("rumi.")
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "document-intelligence"
    assert candidate.signing["verified"] is True


def test_pack_thickened_document_review_contracts() -> None:
    citation_schema = read_json(PACK_DIR / "catalog" / "citation_page_span_schema.json")
    redline_matrix = yaml.safe_load((PACK_DIR / "catalog" / "redline_review_matrix.yaml").read_text(encoding="utf-8"))
    privacy_policy = yaml.safe_load((PACK_DIR / "policies" / "citation_privacy_review.policy.yaml").read_text(encoding="utf-8"))
    roster = yaml.safe_load((PACK_DIR / "coordination" / "subagent_review_roster.yaml").read_text(encoding="utf-8"))
    document_types = yaml.safe_load((PACK_DIR / "catalog" / "document_types.yaml").read_text(encoding="utf-8"))
    citation_trace = read_json(PACK_DIR / "schemas" / "citation_trace.schema.json")
    redline_handoff = read_json(PACK_DIR / "schemas" / "redline_handoff.schema.json")
    redline_tests = yaml.safe_load((PACK_DIR / "catalog" / "redline_operation_tests.yaml").read_text(encoding="utf-8"))

    assert "page_start" in citation_schema["citation_record"]["required_fields"]
    assert "page_end" in citation_schema["citation_record"]["required_fields"]
    assert "missing_evidence" in citation_schema["citation_record"]["evidence_type_allowed"]
    assert {"insertion", "deletion", "substitution", "formatting_only"} <= {
        item["id"] for item in redline_matrix["change_classes"]
    }
    assert "personal_data" in privacy_policy["privacy_classes"]
    assert "confidential_contract" in privacy_policy["privacy_classes"]
    assert "regulated_sensitive" in document_types["privacy_classes"]
    assert "page_span" in citation_trace["required"]
    assert "quote_digest" in citation_trace["required"]
    assert "operations" in redline_handoff["required"]
    operations = [item["operation"] for item in redline_tests["operation_tests"]]
    assert operations == ["insert", "delete", "replace", "format"]
    assert redline_tests["test_contract"]["all_operations_require_page_span"] is True
    assert redline_tests["test_contract"]["all_operations_require_quote_digest"] is True
    assert roster["governance"]["repeated_specialist_passes_required"] is True
    assert roster["governance"]["grant_effect"] == "none"
    assert {item["id"] for item in roster["review_passes"]} == {
        "citation_mapper",
        "redline_delta_reviewer",
        "privacy_reviewer",
        "final_evidence_integrator",
    }


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence", "page-span", "redline", "privacy"]:
        assert expected in docs
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
    combined = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    assert "sample user request" not in combined
    assert "reviewer_ready_plan" not in combined
