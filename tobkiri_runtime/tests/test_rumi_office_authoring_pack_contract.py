from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from ecosystem.setup_pack.pack_selector import PackSelector

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_office_authoring_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = [
    "README.md",
    "asset_index.json",
    "asset_index.yaml",
    "catalog/handoff_matrix.yaml",
    "catalog/office_authoring_patterns.yaml",
    "catalog/quality_matrix.yaml",
    "catalog/taxonomy.yaml",
    "catalog/workflows.yaml",
    "checklists/review.checklist.yaml",
    "docs/README.md",
    "docs/architecture.md",
    "docs/interfaces.md",
    "docs/operations.md",
    "docs/overlap_policy.md",
    "examples/board_update_deck.example.yaml",
    "examples/cited_report_to_pdf.example.yaml",
    "examples/financial_model_workbook.example.yaml",
    "examples/office_suite_handoff_packet.example.yaml",
    "examples/policy_brief_doc.example.yaml",
    "fixtures/contract_fixture.yaml",
    "fixtures/negative_cases.yaml",
    "ledgers/authoring_decision_ledger.schema.yaml",
    "policies/handoff.policy.yaml",
    "policies/safety.policy.yaml",
    "presets/handoff_review.preset.yaml",
    "presets/quality_gate.preset.yaml",
    "presets/safe_default.preset.yaml",
    "profiles/office_authoring_reviewer.profile.yaml",
    "prompts/office_authoring_reviewer.system.md",
    "schemas/chart_embedding_contract.schema.json",
    "schemas/citation_insertion_plan.schema.json",
    "schemas/doc_authoring_contract.schema.json",
    "schemas/formula_map.schema.json",
    "schemas/office_handoff_packet.schema.json",
    "schemas/office_review_gate.schema.json",
    "schemas/pdf_export_plan.schema.json",
    "schemas/slide_deck_contract.schema.json",
    "schemas/slide_record.schema.json",
    "schemas/workbook_contract.schema.json",
    "templates/handoff.template.md",
    "templates/review_report.template.md",
    "templates/ui_contract.template.md",
]

SCHEMA_EXPECTATIONS = {
    "schemas/slide_deck_contract.schema.json": [
        "deck_id",
        "audience",
        "goal",
        "story_arc",
        "slides",
        "speaker_notes_policy",
        "visual_asset_refs",
        "review_gate",
        "export_plan_id",
        "render_owner",
        "file_creation_owner",
        "layout_review_required",
    ],
    "schemas/slide_record.schema.json": [
        "slide_id",
        "title",
        "layout_intent",
        "message",
        "content_blocks",
        "speaker_notes",
        "source_refs",
        "accessibility_notes",
    ],
    "schemas/workbook_contract.schema.json": [
        "workbook_id",
        "tabs",
        "tables",
        "formula_map_id",
        "validation_plan_id",
        "charts",
        "review_gate",
        "export_plan_id",
        "file_creation_owner",
        "analysis_owner",
        "formula_review_required",
    ],
    "schemas/formula_map.schema.json": [
        "formula_map_id",
        "cell_refs",
        "formula_intents",
        "dependencies",
        "recalc_policy",
        "execution_owner",
        "calculation_allowed",
    ],
    "schemas/doc_authoring_contract.schema.json": [
        "doc_id",
        "audience",
        "purpose",
        "outline",
        "sections",
        "style_brief",
        "citation_plan_id",
        "review_gate",
        "export_plan_id",
        "document_understanding_owner",
        "citation_review_required",
    ],
    "schemas/citation_insertion_plan.schema.json": [
        "citation_plan_id",
        "citation_style",
        "source_refs",
        "insertion_targets",
        "missing_source_policy",
        "ledger_owner",
        "verified_export_allowed",
    ],
    "schemas/chart_embedding_contract.schema.json": [
        "chart_id",
        "chart_spec_ref",
        "target_artifact",
        "target_location",
        "alt_text",
        "analysis_owner",
        "render_owner",
    ],
    "schemas/pdf_export_plan.schema.json": [
        "export_plan_id",
        "source_artifact_ids",
        "format_targets",
        "pagination_checks",
        "accessibility_checks",
        "render_owner",
        "manifest_only",
        "file_creation_owner",
        "verified_export_requires_review_gate",
    ],
    "schemas/office_review_gate.schema.json": [
        "review_gate_id",
        "blocking_checks",
        "citation_required",
        "layout_required",
        "formula_required",
        "accessibility_required",
        "export_allowed",
        "minimum_pass",
    ],
    "schemas/office_handoff_packet.schema.json": [
        "packet_id",
        "artifact_contract_ids",
        "owner_pack_handoffs",
        "review_gate_id",
        "export_plan_ids",
        "status",
        "external_action",
        "render_owner",
        "analysis_owner",
        "citation_owner",
        "document_understanding_owner",
    ],
}

WORKFLOW_IDS = {
    "deck_storyboard_authoring",
    "workbook_formula_planning",
    "doc_outline_citation_plan",
    "chart_embedding_review",
    "pdf_export_gate",
    "office_suite_handoff",
}
QUALITY_CHECK_IDS = {
    "citation_gate",
    "layout_gate",
    "formula_dependency_gate",
    "accessibility_gate",
    "export_manifest_only",
    "owner_handoff_named",
    "asset_index_complete",
}
OWNER_EXPECTED = {
    "slide_deck_authoring_contract",
    "slide_storyboard",
    "slide_layout_brief",
    "speaker_notes_contract",
    "spreadsheet_workbook_authoring_contract",
    "sheet_tab_plan",
    "formula_map",
    "data_validation_plan",
    "doc_authoring_contract",
    "document_outline",
    "style_and_tone_brief",
    "citation_insertion_plan",
    "chart_embedding_contract",
    "pdf_export_plan",
    "office_review_gate",
    "office_suite_handoff_packet",
}
NON_OWNER_EXPECTED = {
    "workspace broad artifact catalog and lifecycle",
    "document parsing and redline understanding",
    "data cleaning and statistical analysis",
    "claim graph and citation ledger credibility",
    "PPTX DOCX XLSX PDF rendering",
    "artifact app runtime approval",
    "external retrieval and connectors",
}
OVERLAP_EXPECTED = {
    "workspace_artifact_catalog": "handoff_to_rumi_workspace_pack",
    "office_file_rendering": "handoff_to_rumi_workspace_pack",
    "pdf_rendering": "handoff_to_rumi_workspace_pack",
    "document_understanding": "handoff_to_rumi_document_intelligence_pack",
    "data_analysis": "handoff_to_rumi_data_analysis_pack",
    "citation_ledger": "handoff_to_rumi_evidence_dossier_pack",
    "artifact_app_runtime": "handoff_to_rumi_artifact_app_runtime_pack",
    "external_retrieval": "handoff_to_rumi_research_pack",
    "connector_access": "handoff_to_rumi_connector_gateway_pack",
    "office_authoring_contract": "owned_by_rumi_office_authoring_pack",
    "tool_aliases": "prefer_explicit_pack_namespace",
}
PROMOTION_BLOCKERS = {
    "schema_contracts_only",
    "runtime_rendering_owned_elsewhere",
    "workspace_lifecycle_owned_elsewhere",
    "document_understanding_owned_elsewhere",
    "data_analysis_owned_elsewhere",
    "citation_ledger_owned_elsewhere",
    "requires_real_world_office_authoring_cases",
    "must_prove_review_gate_blocking_cases",
    "supports_all_ok_false_required",
}
PROMOTION_EVIDENCE = {
    "deck_authoring_cases",
    "workbook_formula_review_cases",
    "doc_citation_insertion_cases",
    "pdf_export_plan_cases",
    "office_suite_handoff_packet_cases",
    "negative_blocking_cases",
}
BLOCKED_BY_DEFAULT = {
    "direct PPTX DOCX XLSX PDF render",
    "direct file persistence",
    "direct ZIP export bundle creation",
    "direct connector source retrieval",
    "direct web research retrieval",
    "direct statistical data analysis",
    "direct workbook recalculation",
    "direct document parsing redline extraction",
    "direct artifact app runtime approval",
    "verified export with missing citations",
    "verified export with unresolved layout formula accessibility review failures",
}
FORBIDDEN_DIRS = {
    "api",
    "backend",
    "blocks",
    "domain",
    "functions",
    "routes",
    "scripts",
    "static",
    "stores",
    "tools",
    "transport",
    "ui",
    "webapp",
}
FORBIDDEN_EXTENSIONS = {".py", ".sh", ".js", ".ts", ".tsx", ".ipynb", ".sql"}
PACK_METADATA_FILES = {
    "ecosystem.json",
    "rumi.pack.v3.json",
    "artifact-manifest.json",
    "executables.v4.json",
    "frontend/contributions/office-authoring.json",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_required_assets_and_ecosystem_contract() -> None:
    assert [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["runtime"]["type"] == "declarative_pack"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert ecosystem["host_execution"] is False
    metadata = ecosystem["metadata"]
    assert metadata["runtime_type"] in {"declarative_pack", "declarative_setup_pack"}
    assert metadata["network_policy"] == "none_by_default"
    assert metadata["executable_code"] is False
    assert metadata["declarative_only"] is True
    assert metadata["output_effect"] == "draft_and_handoff_only"
    assert metadata["defaultspack_promotion_eligible"] is False
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED

    actual = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.relative_to(PACK_DIR).as_posix() not in PACK_METADATA_FILES
    }
    actual -= V4_AUTHORITY_ARTIFACTS
    indexed = {item for values in metadata["asset_index"].values() for item in values}
    assert actual == indexed == set(REQUIRED_ASSETS)
    asset_index = read_yaml(PACK_DIR / "asset_index.yaml")["asset_index"]
    indexed_file_assets = {item for values in asset_index["categories"].values() for item in values}
    assert indexed_file_assets == actual
    assert asset_index["invariants"]["external_actions_are_handoffs"] is True
    assert asset_index["invariants"]["defaultspack_promotion_eligible"] is False
    assert asset_index["invariants"]["declarative_only"] is True


def test_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(read_yaml(path), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(read_json(path), dict), path


def test_setup_pack_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert setup["compatibility"]["python"] == ">=3.9"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []
    for key, value in OVERLAP_EXPECTED.items():
        assert candidate.overlap_policy[key] == value
    assert any(value.startswith("owned_by_") for value in candidate.overlap_policy.values())
    assert any("handoff" in value for value in candidate.overlap_policy.values())
    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "office-authoring"
    assert candidate.signing["verified"] is True


def test_pack_v4_contract_carries_setup_dependencies() -> None:
    setup = read_json(SETUP_PACK_JSON)
    manifest = read_json(PACK_DIR / "pack.v4.json")
    setup_dependencies = {
        item["pack_id"]: item["version"] for item in setup["depends_on"]
    }

    assert manifest["pack"]["id"] == PACK_ID
    # Setup selection depends on Defaults, while the generated v4 runtime
    # manifest is declarative and carries no runtime Pack dependency.  These
    # are separate authorities and must not be conflated.
    assert setup_dependencies == {"defaultspack": ">=2.0.0"}
    assert manifest["requirements"]["pack_dependencies"] == {}
    assert manifest["requirements"]["network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert manifest["requirements"]["secrets"] == []


def test_schema_workflow_quality_policy_contracts() -> None:
    for rel_path, required in SCHEMA_EXPECTATIONS.items():
        schema = read_json(PACK_DIR / rel_path)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) >= set(required)
        assert set(required) <= set(schema["properties"])

    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    ledger = read_yaml(PACK_DIR / "ledgers/authoring_decision_ledger.schema.yaml")["evidence_ledger_schema"]

    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["default_mode"] == "draft_and_handoff_only"
    assert policy["external_effect"] == "handoff_packet_only"
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    for key, expected in OVERLAP_EXPECTED.items():
        assert handoff_policy["overlap_policy"][key] == expected
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert ledger["completion_rules"]["render_and_file_outputs_are_manifest_handoffs"] is True
    assert ledger["completion_rules"]["verified_export_requires_complete_review_gate"] is True
    assert ledger["completion_rules"]["missing_citations_block_verified_export"] is True
    assert ledger["completion_rules"]["unresolved_formula_dependencies_block_verified_export"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"


def test_office_authoring_owner_consts_and_review_gate() -> None:
    deck = read_json(PACK_DIR / "schemas/slide_deck_contract.schema.json")
    workbook = read_json(PACK_DIR / "schemas/workbook_contract.schema.json")
    formula = read_json(PACK_DIR / "schemas/formula_map.schema.json")
    document = read_json(PACK_DIR / "schemas/doc_authoring_contract.schema.json")
    citation = read_json(PACK_DIR / "schemas/citation_insertion_plan.schema.json")
    chart = read_json(PACK_DIR / "schemas/chart_embedding_contract.schema.json")
    pdf = read_json(PACK_DIR / "schemas/pdf_export_plan.schema.json")
    gate = read_json(PACK_DIR / "schemas/office_review_gate.schema.json")
    packet = read_json(PACK_DIR / "schemas/office_handoff_packet.schema.json")
    patterns = read_yaml(PACK_DIR / "catalog/office_authoring_patterns.yaml")["office_authoring_patterns"]

    assert deck["properties"]["render_owner"]["const"] == "rumi_workspace_pack"
    assert deck["properties"]["file_creation_owner"]["const"] == "rumi_workspace_pack"
    assert deck["properties"]["layout_review_required"]["const"] is True
    assert workbook["properties"]["file_creation_owner"]["const"] == "rumi_workspace_pack"
    assert workbook["properties"]["analysis_owner"]["const"] == "rumi_data_analysis_pack"
    assert workbook["properties"]["formula_review_required"]["const"] is True
    assert formula["properties"]["execution_owner"]["const"] == "rumi_workspace_pack"
    assert formula["properties"]["calculation_allowed"]["const"] is False
    assert document["properties"]["document_understanding_owner"]["const"] == "rumi_document_intelligence_pack"
    assert document["properties"]["citation_review_required"]["const"] is True
    assert citation["properties"]["ledger_owner"]["const"] == "rumi_evidence_dossier_pack"
    assert citation["properties"]["missing_source_policy"]["const"] == "block_verified_export_when_missing_sources"
    assert citation["properties"]["verified_export_allowed"]["const"] is False
    assert chart["properties"]["analysis_owner"]["const"] == "rumi_data_analysis_pack"
    assert chart["properties"]["render_owner"]["const"] == "rumi_workspace_pack"
    assert pdf["properties"]["manifest_only"]["const"] is True
    assert pdf["properties"]["render_owner"]["const"] == "rumi_workspace_pack"
    assert pdf["properties"]["file_creation_owner"]["const"] == "rumi_workspace_pack"
    assert pdf["properties"]["verified_export_requires_review_gate"]["const"] is True
    assert gate["properties"]["citation_required"]["const"] is True
    assert gate["properties"]["layout_required"]["const"] is True
    assert gate["properties"]["formula_required"]["const"] is True
    assert gate["properties"]["accessibility_required"]["const"] is True
    assert gate["properties"]["export_allowed"]["const"] is False
    assert gate["properties"]["minimum_pass"]["const"] == "all_blocking_checks"
    assert packet["properties"]["external_action"]["const"] == "handoff_only"
    assert packet["properties"]["render_owner"]["const"] == "rumi_workspace_pack"
    assert packet["properties"]["analysis_owner"]["const"] == "rumi_data_analysis_pack"
    assert packet["properties"]["citation_owner"]["const"] == "rumi_evidence_dossier_pack"
    assert packet["properties"]["document_understanding_owner"]["const"] == "rumi_document_intelligence_pack"
    assert {"citation", "layout", "formula", "accessibility", "export"} <= set(patterns["review_gates"])
    assert patterns["owner_handoffs"]["rendering"] == "rumi_workspace_pack"
    assert patterns["owner_handoffs"]["data_analysis"] == "rumi_data_analysis_pack"


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) >= 5
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)
    assert all(item["handoff_owner"] for item in examples)
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"
    assert {
        "missing_citation_export",
        "unresolved_formula_dependency",
        "overlapping_slide_layout",
        "missing_chart_alt_text",
        "attempted_direct_render",
    } <= set(negative["cases"])
    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    assert all(item["external_action"] == "handoff_only" for item in presets)
    profile = read_yaml(next((PACK_DIR / "profiles").glob("*.profile.yaml")))["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"

    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md", "docs/overlap_policy.md"]
    )
    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "Handoff",
        "Does Not Provide",
        "rumi_workspace_pack",
        "rumi_document_intelligence_pack",
        "rumi_data_analysis_pack",
        "rumi_evidence_dossier_pack",
        "rumi_artifact_app_runtime_pack",
    ]:
        assert expected in docs


def test_pack_body_has_no_credentials_or_runtime_surfaces() -> None:
    assert {path.name for path in PACK_DIR.iterdir() if path.is_dir()} & FORBIDDEN_DIRS == set()
    assert [path for path in PACK_DIR.rglob("*") if path.is_file() and path.suffix in FORBIDDEN_EXTENSIONS] == []
    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()] + [SETUP_PACK_JSON]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    generated_key_prefix = "s" + "k-"
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    for phrase in [
        generated_key_prefix,
        private_key_marker,
        "password=",
        "sample user request",
        "reviewer_ready_plan",
        "TODO",
    ]:
        assert phrase not in combined
    generated_key_pattern = r"s" + r"k-[A-Za-z0-9_-]{20,}"
    assert re.search(generated_key_pattern, combined) is None
    actual_secret_patterns = [
        r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{12,}['\"]",
        r"ghp_[A-Za-z0-9]{20,}",
        r"xox[baprs]-[A-Za-z0-9-]{20,}",
        r"AIza[0-9A-Za-z_-]{20,}",
        r"ya29\.[0-9A-Za-z_-]+",
    ]
    for pattern in actual_secret_patterns:
        assert re.search(pattern, combined) is None
