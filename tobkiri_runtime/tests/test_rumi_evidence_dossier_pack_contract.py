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
PACK_ID = 'rumi_evidence_dossier_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"
PACK_METADATA_FILES = {"executables.v4.json"}
REQUIRED_ASSETS = ['README.md', 'asset_index.json', 'asset_index.yaml', 'catalog/citation_styles.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/source_quality_labels.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'docs/overlap_policy.md', 'examples/cited_research_brief.example.yaml', 'examples/contradiction_review.example.yaml', 'examples/source_quality_queue.example.yaml', 'examples/uncited_claim_block.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/evidence_reviewer.profile.yaml', 'prompts/evidence_dossier_reviewer.system.md', 'schemas/citation_ledger.schema.json', 'schemas/claim_evidence_graph.schema.json', 'schemas/claim_record.schema.json', 'schemas/contradiction_detection_contract.schema.json', 'schemas/contradiction_record.schema.json', 'schemas/dossier_export_manifest.schema.json', 'schemas/evidence_link.schema.json', 'schemas/reviewer_queue.schema.json', 'schemas/source_quality_label.schema.json', 'schemas/source_registry.schema.json', 'templates/handoff.template.md', 'templates/review_report.template.md', 'templates/ui_contract.template.md']
SCHEMA_EXPECTATIONS = {'schemas/citation_ledger.schema.json': ['ledger_id', 'claim_ids', 'citation_style', 'missing_claim_ids', 'completion_state', 'gate_policy', 'verified_export_allowed', 'claim_citation_map'], 'schemas/claim_evidence_graph.schema.json': ['graph_id', 'claim_ids', 'evidence_ids', 'source_ids', 'contradiction_ids', 'citation_ledger_id', 'ready_for_export', 'export_gate'], 'schemas/claim_record.schema.json': ['claim_id', 'statement', 'claim_type', 'evidence_ids', 'confidence', 'status'], 'schemas/contradiction_detection_contract.schema.json': ['contract_id', 'candidate_claim_ids', 'candidate_evidence_ids', 'detector_kind', 'human_review_required', 'output_schema_ref', 'model_scoring_allowed', 'runtime_execution', 'detection_output_only'], 'schemas/contradiction_record.schema.json': ['contradiction_id', 'claim_ids', 'evidence_ids', 'severity', 'resolution_state', 'reviewer_notes'], 'schemas/dossier_export_manifest.schema.json': ['export_id', 'dossier_id', 'claim_ids', 'source_ids', 'citation_ledger_id', 'unresolved_contradictions', 'verified', 'manifest_only', 'render_owner', 'requires_citation_ledger_complete', 'requires_no_unresolved_contradictions'], 'schemas/evidence_link.schema.json': ['evidence_id', 'source_id', 'source_anchor', 'excerpt_summary', 'supports_claim_ids', 'contradicts_claim_ids'], 'schemas/reviewer_queue.schema.json': ['queue_id', 'items', 'priority_rule', 'handoff_owner', 'review_state'], 'schemas/source_quality_label.schema.json': ['quality_label_id', 'source_id', 'credibility', 'recency_state', 'bias_notes', 'review_state'], 'schemas/source_registry.schema.json': ['source_id', 'source_type', 'title', 'provenance', 'quality_label_id', 'access_owner', 'retrieval_mode', 'content_storage_policy', 'raw_secret_material_allowed']}
WORKFLOW_IDS = set(['source_intake_registry', 'claim_evidence_graph', 'contradiction_review', 'citation_completeness_gate', 'reviewer_queue_triage', 'dossier_export_manifest'])
QUALITY_CHECK_IDS = set(['source_provenance_present', 'claim_has_evidence', 'source_anchor_present', 'contradiction_blocks_export', 'citation_ledger_complete', 'no_retrieval_execution', 'reviewer_queue_blocking', 'quality_label_present'])
OWNER_EXPECTED = set(['source_registry', 'source_quality_label', 'claim_evidence_graph', 'evidence_link_contract', 'contradiction_review_contract', 'citation_ledger', 'reviewer_queue', 'dossier_export_manifest'])
NON_OWNER_EXPECTED = set(['source retrieval', 'connector access', 'data transformation', 'document rendering', 'workspace export', 'model eval scoring', 'web browsing'])
OVERLAP_EXPECTED = {'source_retrieval': 'handoff_to_defaultspack', 'connector_access': 'handoff_to_defaultspack', 'data_transformation': 'handoff_to_defaultspack', 'document_rendering': 'handoff_to_defaultspack', 'workspace_export': 'handoff_to_defaultspack', 'model_eval_scoring': 'handoff_to_defaultspack', 'claim_evidence_graph': 'owned_by_rumi_evidence_dossier_pack', 'citation_ledger': 'owned_by_rumi_evidence_dossier_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['requires_shared_source_provenance_object', 'requires_citation_required_response_mode', 'retrieval_owned_elsewhere', 'connector_access_owned_elsewhere', 'data_transforms_owned_elsewhere', 'document_rendering_owned_elsewhere', 'workspace_render_export_owned_elsewhere', 'model_eval_scoring_owned_elsewhere', 'must_prove_contradiction_blocking_cases'])
PROMOTION_EVIDENCE = set(['claim_with_source_anchor_cases', 'uncited_claim_block_cases', 'contradiction_review_cases', 'source_quality_label_cases', 'export_manifest_review_cases'])
BLOCKED_BY_DEFAULT = set(['export verified dossier with uncited claims', 'hide unresolved contradictions', 'retrieve sources directly', 'render final documents directly', 'score model outputs directly'])
HANDOFF_TARGETS = set(['defaultspack', 'rumi_default_tools_pack'])
WORKFLOW_HANDOFFS = set(['handoff_to_defaultspack', 'handoff_to_rumi_default_tools_pack'])
FORBIDDEN_EXECUTION_SURFACES = {
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
FORBIDDEN_RUNTIME_FILENAMES = {
    "manifest.json",
    "package.json",
    "permissions.json",
    "pyproject.toml",
    "requirements.lock",
    "requirements.txt",
    "routes.json",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def available_setup_pack_ids() -> set[str]:
    selector = PackSelector(ROOT / "ecosystem")
    return {item.pack_id for item in selector.scan_candidates()}


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
    assert ecosystem["required_network"] == []
    assert ecosystem["host_execution"] is False
    metadata = ecosystem["metadata"]
    assert metadata["runtime_type"] == "declarative_pack"
    assert metadata["network_policy"] == "none_by_default"
    assert metadata["executable_code"] is False
    assert metadata["declarative_only"] is True
    assert metadata["consumes_existing_sources_only"] is True
    assert metadata["output_effect"] == "draft_and_handoff_only"
    assert metadata["base_pack_promotion_eligible"] is False
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    available = available_setup_pack_ids()
    assert HANDOFF_TARGETS <= available
    optional_integrations = {item["pack_id"]: item["reason"] for item in metadata["optional_integrations"]}
    assert set(optional_integrations) == HANDOFF_TARGETS
    assert "connector access" in optional_integrations["defaultspack"]
    assert "browser" in optional_integrations["rumi_default_tools_pack"].lower()
    actual = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name != "ecosystem.json"
        and path.relative_to(PACK_DIR).as_posix() not in PACK_METADATA_FILES
    }
    actual -= V4_AUTHORITY_ARTIFACTS
    indexed = {item for values in metadata["asset_index"].values() for item in values}
    assert actual == indexed == set(REQUIRED_ASSETS)
    asset_index = read_yaml(PACK_DIR / "asset_index.yaml")["asset_index"]
    indexed_file_assets = {item for values in asset_index["categories"].values() for item in values}
    assert indexed_file_assets == actual
    assert asset_index["invariants"]["external_actions_are_handoffs"] is True
    assert asset_index["invariants"]["base_pack_promotion_eligible"] is False


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
    assert setup["risk_level"] == 'medium'
    assert setup["compatibility"]["python"] == ">=3.10"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    issues = selector.validate_candidates(installed_packs={"defaultspack": {"version": "2.0.0"}}, platform_name="linux", python_version="3.11.0")
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []
    for key, value in OVERLAP_EXPECTED.items():
        assert candidate.overlap_policy[key] == value
    assert any(value.startswith("owned_by_") for value in candidate.overlap_policy.values())
    assert any("handoff" in value for value in candidate.overlap_policy.values())
    assert candidate.base_pack_promotion["eligible"] is False
    assert set(candidate.base_pack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.base_pack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'evidence-dossier'
    assert candidate.signing["verified"] is True


def test_schema_workflow_quality_and_policy_contracts() -> None:
    for rel_path, required in SCHEMA_EXPECTATIONS.items():
        schema = read_json(PACK_DIR / rel_path)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) >= set(required)
        assert set(required) <= set(schema["properties"])
    assert len(SCHEMA_EXPECTATIONS) >= 7

    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    source_quality_labels = read_yaml(PACK_DIR / "catalog/source_quality_labels.yaml")["source_quality_labels"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    taxonomy = read_yaml(PACK_DIR / "catalog/taxonomy.yaml")["taxonomy"]
    ledger = read_yaml(PACK_DIR / "ledgers/evidence_ledger.schema.yaml")["evidence_ledger_schema"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    graph_schema = read_json(PACK_DIR / "schemas/claim_evidence_graph.schema.json")
    citation_schema = read_json(PACK_DIR / "schemas/citation_ledger.schema.json")
    contradiction_contract = read_json(PACK_DIR / "schemas/contradiction_detection_contract.schema.json")
    export_manifest = read_json(PACK_DIR / "schemas/dossier_export_manifest.schema.json")
    source_registry = read_json(PACK_DIR / "schemas/source_registry.schema.json")
    quality_label_schema = read_json(PACK_DIR / "schemas/source_quality_label.schema.json")
    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert all(set(item["handoffs"]) >= WORKFLOW_HANDOFFS for item in workflows["items"])
    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["default_mode"] == "draft_and_handoff_only"
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    for key, expected in OVERLAP_EXPECTED.items():
        assert handoff_policy["overlap_policy"][key] == expected
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    matrix_resolution = {item["surface"]: item["resolution"] for item in handoff_matrix["items"]}
    for key, expected in OVERLAP_EXPECTED.items():
        if key in {"claim_evidence_graph", "citation_ledger", "tool_aliases"}:
            continue
        assert matrix_resolution[key] == expected
    assert HANDOFF_TARGETS <= set(taxonomy["handoff_targets"])
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert ledger["completion_rules"]["verified_export_requires_complete_citation_ledger"] is True
    assert ledger["completion_rules"]["verified_export_requires_no_unresolved_contradictions"] is True
    assert ledger["completion_rules"]["render_and_file_outputs_are_manifest_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"
    assert graph_schema["properties"]["export_gate"]["properties"]["render_owner"]["const"] == "defaultspack"
    assert citation_schema["properties"]["gate_policy"]["const"] == "block_verified_export_when_missing_citations"
    assert contradiction_contract["properties"]["model_scoring_allowed"]["const"] is False
    assert contradiction_contract["properties"]["runtime_execution"]["const"] == "not_owned_by_this_pack"
    assert contradiction_contract["properties"]["detection_output_only"]["const"] is True
    assert export_manifest["properties"]["manifest_only"]["const"] is True
    assert export_manifest["properties"]["render_owner"]["const"] == "defaultspack"
    assert export_manifest["properties"]["requires_citation_ledger_complete"]["const"] is True
    assert export_manifest["properties"]["requires_no_unresolved_contradictions"]["const"] is True
    assert source_registry["properties"]["raw_secret_material_allowed"]["const"] is False
    assert source_registry["properties"]["retrieval_mode"]["enum"] == ["pre_supplied_reference", "owner_pack_handoff"]
    assert source_registry["properties"]["access_owner"]["enum"] == ["defaultspack", "user_supplied"]
    assert source_quality_labels["allowed_use"] == "review_label_only"
    assert source_quality_labels["numeric_score_allowed"] is False
    assert source_quality_labels["ranking_allowed"] is False
    assert {"score", "rank", "model_quality", "eval_metric"} <= set(source_quality_labels["prohibited_fields"])
    assert {"score", "rank", "model_score", "eval_score", "metric"} .isdisjoint(quality_label_schema["properties"])


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) >= 4
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"
    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    profile = read_yaml(next((PACK_DIR / "profiles").glob("*.profile.yaml")))["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "Handoff", "Does Not Provide"]:
        assert expected in docs


def test_pack_body_has_no_credentials_or_skeleton_phrases() -> None:
    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()] + [SETUP_PACK_JSON]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    generated_key_prefix = "s" + "k-"
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    for phrase in [generated_key_prefix, private_key_marker, "auth token", "password=", "sample user request", "reviewer_ready_plan", "Complementary owner surface"]:
        assert phrase not in combined
    generated_key_pattern = r"s" + r"k-[A-Za-z0-9_-]{20,}"
    assert re.search(generated_key_pattern, combined) is None
    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|bearer|client[_-]?secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9._/\-]{12,}"
    )
    assert secret_assignment.search(combined) is None


def _dossier_export_allowed(claims: list[dict], contradictions: list[dict]) -> tuple[bool, str]:
    uncited = [claim["claim_id"] for claim in claims if not claim.get("evidence_ids")]
    unresolved = [item["contradiction_id"] for item in contradictions if item["resolution_state"] == "unresolved"]
    if uncited:
        return False, "uncited_claims:" + ",".join(sorted(uncited))
    if unresolved:
        return False, "unresolved_contradictions:" + ",".join(sorted(unresolved))
    return True, "verified_export_manifest"


def test_evidence_dossier_blocks_uncited_and_unresolved_exports() -> None:
    claims = [
        {"claim_id": "claim_1", "evidence_ids": ["ev_1"]},
        {"claim_id": "claim_2", "evidence_ids": []},
    ]
    assert _dossier_export_allowed(claims, []) == (False, "uncited_claims:claim_2")
    resolved_claims = [{"claim_id": "claim_1", "evidence_ids": ["ev_1"]}]
    contradictions = [{"contradiction_id": "contra_1", "resolution_state": "unresolved"}]
    assert _dossier_export_allowed(resolved_claims, contradictions) == (False, "unresolved_contradictions:contra_1")
    contradictions[0]["resolution_state"] = "accepted_limitation"
    assert _dossier_export_allowed(resolved_claims, contradictions) == (True, "verified_export_manifest")
def test_evidence_dossier_subagent_acceptance_assets() -> None:
    graph = read_json(PACK_DIR / "schemas/claim_evidence_graph.schema.json")
    detection = read_json(PACK_DIR / "schemas/contradiction_detection_contract.schema.json")
    quality_labels = read_yaml(PACK_DIR / "catalog/source_quality_labels.yaml")["source_quality_labels"]
    citation_styles = read_yaml(PACK_DIR / "catalog/citation_styles.yaml")["citation_styles"]
    assert {"graph_id", "claim_ids", "evidence_ids", "source_ids", "contradiction_ids", "citation_ledger_id", "ready_for_export"} <= set(graph["required"])
    assert detection["properties"]["human_review_required"]["const"] is True
    assert detection["properties"]["detector_kind"]["enum"] == ["schema_rule", "human_review", "external_model_handoff"]
    assert quality_labels["not_eval_scores"] is True
    assert citation_styles["missing_citation_blocks_verified_export"] is True
    assert [name for name in FORBIDDEN_EXECUTION_SURFACES if (PACK_DIR / name).exists()] == []
    assert [path for path in PACK_DIR.rglob("*.py")] == []
    assert [path for path in PACK_DIR.rglob("*.sh")] == []
    assert [path for path in PACK_DIR.rglob("*") if path.name in FORBIDDEN_RUNTIME_FILENAMES] == []
    docs = (PACK_DIR / "docs/overlap_policy.md").read_text(encoding="utf-8")
    for owner in ["defaultspack", "rumi_default_tools_pack"]:
        assert owner in docs
