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
PACK_ID = 'rumi_experiment_design_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = ['README.md', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'examples/ab_test_checkout_copy.example.yaml', 'examples/instrumentation_request.example.yaml', 'examples/quasi_experiment_pricing.example.yaml', 'examples/rollout_guardrail_plan.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/experiment_designer.profile.yaml', 'prompts/experiment_planner.system.md', 'schemas/assignment_plan.schema.json', 'schemas/decision_record.schema.json', 'schemas/guardrail_plan.schema.json', 'schemas/hypothesis.schema.json', 'schemas/instrumentation_request.schema.json', 'schemas/metric_plan.schema.json', 'schemas/sample_size_plan.schema.json', 'templates/artifact_card.template.md', 'templates/handoff.template.md', 'templates/review_report.template.md']
SCHEMA_EXPECTATIONS = {'schemas/hypothesis.schema.json': ['hypothesis_id', 'population', 'intervention', 'expected_effect', 'evidence_basis', 'falsification_rule'], 'schemas/metric_plan.schema.json': ['metric_id', 'primary_metric', 'guardrail_metrics', 'instrumentation_owner', 'data_availability_state', 'analysis_handoff'], 'schemas/sample_size_plan.schema.json': ['plan_id', 'sample_source', 'minimum_detectable_effect', 'power_assumption', 'allocation_ratio', 'limitations'], 'schemas/assignment_plan.schema.json': ['assignment_id', 'unit_of_assignment', 'randomization_method', 'bias_risks', 'exposure_rules', 'owner_pack'], 'schemas/guardrail_plan.schema.json': ['guardrail_plan_id', 'metrics', 'rollback_thresholds', 'monitoring_owner', 'stop_rules', 'human_review_required'], 'schemas/instrumentation_request.schema.json': ['request_id', 'metric_ids', 'event_contracts', 'observability_owner', 'data_analysis_owner', 'privacy_review_state'], 'schemas/decision_record.schema.json': ['decision_id', 'hypothesis_id', 'available_data_state', 'decision', 'result_claim', 'analysis_boundary', 'limitations', 'source_evidence_ids', 'next_owner_pack']}
WORKFLOW_IDS = set(['ab_test_design', 'quasi_experiment_design', 'rollout_guardrail_plan', 'instrumentation_requirements', 'decision_ready_packet'])
QUALITY_CHECK_IDS = set(['hypothesis_metric_alignment', 'guardrail_metric_present', 'sample_size_assumptions', 'assignment_bias_risk', 'no_result_claim_without_data', 'no_analytics_query_execution', 'data_source_state_declared', 'rollback_owner_named', 'privacy_review_state_declared'])
OWNER_EXPECTED = set(['hypothesis_contract', 'metric_plan', 'sample_size_plan', 'assignment_plan', 'guardrail_plan', 'instrumentation_requirements', 'decision_record', 'experiment_readiness_packet'])
NON_OWNER_EXPECTED = set(['analytics query execution', 'production rollout', 'runtime telemetry collection', 'model benchmark execution', 'business decision execution', 'feature flag mutation'])
OVERLAP_EXPECTED = {'analytics_query_execution': 'handoff_to_defaultspack_tool_runtime', 'runtime_telemetry': 'handoff_to_defaultspack_tool_runtime', 'production_rollout': 'handoff_to_defaultspack_tool_runtime', 'model_benchmarking': 'handoff_to_defaultspack_tool_runtime', 'business_decision_execution': 'handoff_to_rumi_operations_company_pack', 'feature_flag_mutation': 'handoff_to_defaultspack_tool_runtime', 'experiment_design_contract': 'owned_by_rumi_experiment_design_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['does_not_run_analytics_queries', 'does_not_claim_results_without_supplied_data', 'rollout_handoff_owned_by_defaultspack', 'telemetry_handoff_owned_by_defaultspack', 'must_validate_guardrail_and_rollback_assumptions'])
PROMOTION_EVIDENCE = set(['hypothesis_metric_alignment_cases', 'sample_size_assumption_cases', 'guardrail_decision_cases', 'quasi_experiment_bias_cases', 'handoff_acceptance_by_defaultspack_and_operations_company'])
BLOCKED_BY_DEFAULT = set(['declare a winner without supplied data', 'claim statistical significance or lift without supplied results', 'ignore guardrail metrics', 'run analytics queries', 'mutate feature flags or production rollout', 'hide sample-size assumptions'])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def decision_rule_for(schema: dict, data_state: str) -> dict:
    for rule in schema.get("allOf", []):
        state = rule.get("if", {}).get("properties", {}).get("available_data_state", {})
        if state.get("const") == data_state:
            return rule.get("then", {})
    raise AssertionError(f"missing decision rule for {data_state}")


def test_pack_required_assets_and_ecosystem_contract() -> None:
    missing = [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()]
    assert missing == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["host_execution"] is False
    assert ecosystem["metadata"]["runtime_type"] == "declarative_setup_pack"
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["declarative_only"] is True
    assert ecosystem["metadata"]["consumes_existing_sources_only"] is True
    assert ecosystem["metadata"]["output_effect"] == "draft_and_handoff_only"
    assert ecosystem["metadata"]["base_pack_promotion_eligible"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(ecosystem["metadata"]["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    available = {item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()}
    assert {item["pack_id"] for item in ecosystem["metadata"]["optional_integrations"]} <= available

    metadata_indexed = {item for values in ecosystem["metadata"]["asset_index"].values() for item in values}
    actual = {
        str(path.relative_to(PACK_DIR))
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name not in {"ecosystem.json", "executables.v4.json"}
    }
    actual -= V4_AUTHORITY_ARTIFACTS
    assert metadata_indexed == actual
    assert set(REQUIRED_ASSETS) == actual

    asset_index = read_yaml(PACK_DIR / "asset_index.yaml")["asset_index"]
    indexed_file_assets = {item for values in asset_index["categories"].values() for item in values}
    assert indexed_file_assets == actual
    assert asset_index["invariants"] == {
        "required_secrets": [],
        "required_network": [],
        "executable_code": False,
        "supports_all_ok": False,
        "external_actions_are_handoffs": True,
        "base_pack_promotion_eligible": False,
    }


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path
    for path in PACK_DIR.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]

    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == 'medium'
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
    owned = {key: value for key, value in candidate.overlap_policy.items() if value.startswith("owned_by_")}
    handed = {key: value for key, value in candidate.overlap_policy.items() if "handoff" in value}
    assert owned
    assert handed
    assert candidate.overlap_policy["tool_aliases"] == "prefer_explicit_pack_namespace"

    assert candidate.base_pack_promotion["eligible"] is False
    assert set(candidate.base_pack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.base_pack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'experiment-design'
    assert candidate.signing["mode"] == "repository_reviewed"
    assert candidate.signing["verified"] is True


def test_schema_required_fields_are_domain_specific() -> None:
    for rel_path, expected_required in SCHEMA_EXPECTATIONS.items():
        schema = read_json(PACK_DIR / rel_path)
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) >= set(expected_required)
        assert set(expected_required) <= set(schema["properties"])
    assert len(SCHEMA_EXPECTATIONS) >= 6


def test_decision_contract_blocks_design_only_result_claims() -> None:
    schema = read_json(PACK_DIR / "schemas/decision_record.schema.json")
    result_claim = schema["properties"]["result_claim"]
    analysis_boundary = schema["properties"]["analysis_boundary"]

    assert set(result_claim["required"]) == {"status", "statement", "requires_supplied_results"}
    assert result_claim["properties"]["status"]["enum"] == ["not_claimed", "supplied_results_claim"]
    assert result_claim["properties"]["requires_supplied_results"]["const"] is True
    assert analysis_boundary["properties"]["queries_executed_by_pack"]["const"] is False
    assert analysis_boundary["properties"]["handoff_owner_pack"]["const"] == "defaultspack"
    next_owners = set(schema["properties"]["next_owner_pack"]["enum"]) - {"none"}
    available = {item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()}
    assert next_owners <= available

    design_only = decision_rule_for(schema, "design_only")
    insufficient = decision_rule_for(schema, "insufficient_data")
    supplied = decision_rule_for(schema, "supplied_results")
    assert design_only["properties"]["result_claim"]["properties"]["status"]["const"] == "not_claimed"
    assert design_only["properties"]["analysis_boundary"]["properties"]["allowed_source_state"]["const"] == "design_artifacts_only"
    assert insufficient["properties"]["result_claim"]["properties"]["status"]["const"] == "not_claimed"
    assert insufficient["properties"]["analysis_boundary"]["properties"]["allowed_source_state"]["const"] == "insufficient_data"
    assert supplied["properties"]["analysis_boundary"]["properties"]["allowed_source_state"]["const"] == "user_supplied_results_only"

    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    cases = {item["id"]: item for item in negative["cases"]}
    winner_case = cases["design_only_winner_claim_negative"]
    expected = winner_case["expected_decision_record"]
    assert winner_case["supplied_data_state"] == "design_only"
    assert set(winner_case["forbidden_result_claims"]) >= {
        "variant_won",
        "statistically_significant",
        "observed_lift",
        "conversion_rate_changed",
    }
    assert expected["available_data_state"] == "design_only"
    assert expected["decision"] == "do_not_decide"
    assert expected["result_claim"]["status"] == "not_claimed"
    assert expected["analysis_boundary"]["queries_executed_by_pack"] is False
    assert expected["analysis_boundary"]["allowed_source_state"] == "design_artifacts_only"
    for forbidden in winner_case["forbidden_result_claims"]:
        assert forbidden not in json.dumps(expected).lower()

    analytics_case = cases["analytics_query_execution_negative"]
    assert analytics_case["expected_handoff"]["owner_pack"] == "defaultspack"
    assert analytics_case["expected_handoff"]["human_review_required"] is True


def test_workflows_quality_policy_and_handoffs_are_scoped() -> None:
    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    ledger = read_yaml(PACK_DIR / "ledgers/evidence_ledger.schema.yaml")["evidence_ledger_schema"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    template = (PACK_DIR / "templates/handoff.template.md").read_text(encoding="utf-8")

    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert all("blocking_gates" in item and item["blocking_gates"] for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED

    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert "boundary_violation" in quality["fail_states"]
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["default_mode"] == "draft_and_handoff_only"
    assert any("result_claim.status" in rule for rule in policy["claim_rules"])
    assert any("analytics queries" in rule for rule in policy["claim_rules"])
    assert "external_connector_action" in policy["requires_human_review_before"]
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    assert len(handoff_matrix["items"]) >= 3
    assert ledger["completion_rules"]["every_record_has_evidence"] is True
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"
    assert all(item["blocking"] is True for item in checklist["items"])
    assert "Evidence" in template and "Handoff" in template and "Human review required" in template


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) >= 4
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)
    assert all("analytics_query_execution" in item["must_not"] for item in examples)
    assert all("result_claim_without_supplied_data" in item["must_not"] for item in examples)

    fixture = read_yaml(PACK_DIR / "fixtures/contract_fixture.yaml")["contract_fixture"]
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert len(fixture["positive_cases"]) >= 1
    assert len(negative["cases"]) >= 1
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"

    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    profile_path = next((PACK_DIR / "profiles").glob("*.profile.yaml"))
    profile = read_yaml(profile_path)["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"

    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "Handoff", "evidence", "Does Not Provide"]:
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
    assert '"host_execution": true' not in combined
