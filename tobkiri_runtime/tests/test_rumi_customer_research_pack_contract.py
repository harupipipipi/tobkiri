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
PACK_ID = 'rumi_customer_research_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"
PACK_METADATA_FILES = {
    "ecosystem.json",
    "rumi.pack.v3.json",
    "artifact-manifest.json",
    "executables.v4.json",
    "frontend/contributions/customer-research.json",
}

REQUIRED_ASSETS = ['README.md', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'examples/feedback_opportunity_card.example.yaml', 'examples/interview_synthesis.example.yaml', 'examples/revoked_consent_filter.example.yaml', 'examples/survey_theme_map.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/customer_researcher.profile.yaml', 'prompts/insight_synthesizer.system.md', 'schemas/consent_record.schema.json', 'schemas/insight_card.schema.json', 'schemas/interview_evidence.schema.json', 'schemas/opportunity_map.schema.json', 'schemas/participant.schema.json', 'schemas/survey_response.schema.json', 'templates/artifact_card.template.md', 'templates/handoff.template.md', 'templates/review_report.template.md']
SCHEMA_EXPECTATIONS = {'schemas/participant.schema.json': ['participant_id', 'segment', 'consent_state', 'redaction_state', 'allowed_use', 'source_system'], 'schemas/consent_record.schema.json': ['consent_id', 'participant_id', 'consent_state', 'allowed_use', 'expires_at_state', 'evidence_ids', 'review_owner'], 'schemas/interview_evidence.schema.json': ['interview_id', 'participant_id', 'quotes', 'source_span_ids', 'consent_state', 'redaction_state', 'themes'], 'schemas/survey_response.schema.json': ['response_id', 'survey_id', 'question_id', 'answer', 'consent_state', 'redaction_state', 'weighting_note'], 'schemas/insight_card.schema.json': ['insight_id', 'claim', 'opportunity_area', 'confidence', 'supporting_evidence_ids', 'source_quote_ids', 'counter_evidence_ids', 'redaction_state', 'recommended_handoff'], 'schemas/opportunity_map.schema.json': ['map_id', 'insight_ids', 'opportunity_areas', 'assumptions', 'decision_boundary', 'handoff_owner']}
WORKFLOW_IDS = set(['consent_redaction_review', 'interview_synthesis', 'survey_synthesis', 'feedback_to_opportunity_mapping', 'research_brief_handoff'])
QUALITY_CHECK_IDS = set(['consent_state_present', 'redaction_state', 'source_quote_ids', 'source_quote_coverage', 'revoked_consent_excluded', 'counter_evidence_checked', 'no_live_recruiting', 'sample_limit_noted', 'decision_boundary_present', 'handoff_owner_named'])
OWNER_EXPECTED = set(['participant_consent_record', 'research_redaction_policy', 'interview_evidence', 'survey_synthesis', 'feedback_to_insight_card', 'opportunity_map', 'evidence_linked_research_brief'])
NON_OWNER_EXPECTED = set(['live recruiting', 'email or CRM writes', 'generic web research', 'product roadmap decisions', 'analytics query execution', 'contact enrichment', 'message sending'])
OVERLAP_EXPECTED = {'live_recruiting': 'handoff_to_rumi_connector_gateway_pack', 'email_or_crm_writes': 'handoff_to_rumi_business_ops_pack', 'generic_web_research': 'handoff_to_rumi_research_pack', 'analytics_query_execution': 'handoff_to_rumi_data_analysis_pack', 'transcript_normalization': 'handoff_to_rumi_meeting_intelligence_pack', 'customer_insight_cards': 'owned_by_rumi_customer_research_pack', 'consent_redaction_contract': 'owned_by_rumi_customer_research_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['requires_participant_consent_model', 'requires_redaction_review', 'connector_delivery_owned_elsewhere', 'does_not_recruit_or_write_crm', 'must_validate_source_quote_coverage'])
PROMOTION_EVIDENCE = set(['participant_consent_fixture_cases', 'insight_card_source_quote_cases', 'redacted_feedback_cases', 'survey_theme_supporting_response_cases', 'handoff_acceptance_by_connector_and_business_ops'])
BLOCKED_BY_DEFAULT = set(['use participant data without consent', 'store raw personal identifiers in insight cards', 'send recruiting emails', 'write to CRM records', 'perform generic web research', 'claim statistical significance without supplied analysis'])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_ecosystem_contract() -> None:
    missing = [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()]
    assert missing == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["connectivity"] == {
        "requires": [],
        "provides": [],
    }
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
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

    metadata_indexed = {item for values in ecosystem["metadata"]["asset_index"].values() for item in values}
    actual = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.relative_to(PACK_DIR).as_posix() not in PACK_METADATA_FILES
    }
    actual -= V4_AUTHORITY_ARTIFACTS
    assert metadata_indexed == actual
    assert set(REQUIRED_ASSETS) == actual

    asset_index = read_yaml(PACK_DIR / "asset_index.yaml")["asset_index"]
    indexed_file_assets = {item for values in asset_index["categories"].values() for item in values}
    assert indexed_file_assets == actual
    assert asset_index["invariants"] == {
        "required_secrets": [],
        "required_network": {
            "allowed_domains": [],
            "allowed_ports": [],
        },
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
    assert candidate.marketplace["category"] == 'customer-research'
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


def test_fixture_blocks_revoked_consent_before_synthesis() -> None:
    fixture = read_yaml(PACK_DIR / "fixtures/contract_fixture.yaml")["contract_fixture"]
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    participants = {item["participant_id"]: item for item in fixture["participants"]}
    consent_records = {item["participant_id"]: item for item in fixture["consent_records"]}
    evidence_by_participant = {item["participant_id"]: item for item in fixture["interview_evidence"]}
    blocked_by_participant = {item["participant_id"]: item for item in fixture["blocked_synthesis"]}
    revoked_participant_ids = {
        participant_id
        for participant_id, participant in participants.items()
        if participant["consent_state"] == "revoked" or "do_not_use" in participant["allowed_use"]
    }

    assert revoked_participant_ids
    assert set(consent_records) == set(participants)

    insight_cards = fixture["insight_cards"]
    for participant_id in revoked_participant_ids:
        participant = participants[participant_id]
        consent_record = consent_records[participant_id]
        evidence = evidence_by_participant[participant_id]
        revoked_quote_ids = {quote["quote_id"] for quote in evidence["quotes"]}

        assert participant["consent_state"] == "revoked"
        assert participant["redaction_state"] == "raw_blocked"
        assert participant["allowed_use"] == ["do_not_use"]
        assert consent_record["consent_state"] == "revoked"
        assert consent_record["allowed_use"] == ["do_not_use"]
        assert evidence["consent_state"] == "revoked"
        assert evidence["redaction_state"] == "raw_blocked"
        assert revoked_quote_ids
        assert all(quote["allowed_for_insight"] is False for quote in evidence["quotes"])

        for card in insight_cards:
            assert revoked_quote_ids.isdisjoint(card["supporting_evidence_ids"])
            assert revoked_quote_ids.isdisjoint(card["source_quote_ids"])

        blocked = blocked_by_participant[participant_id]
        assert blocked["required_decision"] == "do_not_use"
        assert set(blocked["prohibited_outputs"]) >= {"insight_card", "source_quote_reuse", "external_action"}

    cases = {item["id"]: item for item in negative["cases"]}
    revoked_case = cases["revoked_consent_negative"]
    assert set(revoked_case["must_block"]) >= {"insight_card_creation", "source_quote_reuse", "external_action"}
    assert revoked_case["required_output"] == {
        "decision": "blocked",
        "reason": "revoked_consent",
        "handoff_owner": "rumi_customer_research_pack",
    }


def test_fixture_insight_cards_have_source_quote_coverage() -> None:
    fixture = read_yaml(PACK_DIR / "fixtures/contract_fixture.yaml")["contract_fixture"]
    quote_index = {}
    revoked_quote_ids = set()

    for evidence in fixture["interview_evidence"]:
        source_spans = set(evidence["source_span_ids"])
        for quote in evidence["quotes"]:
            assert quote["source_span_id"] in source_spans
            quote_index[quote["quote_id"]] = (evidence, quote)
            if evidence["consent_state"] == "revoked" or quote["allowed_for_insight"] is False:
                revoked_quote_ids.add(quote["quote_id"])

    assert quote_index
    assert revoked_quote_ids

    for card in fixture["insight_cards"]:
        source_quote_ids = set(card["source_quote_ids"])
        supporting_evidence_ids = set(card["supporting_evidence_ids"])
        counter_evidence_ids = set(card["counter_evidence_ids"])

        assert card["claim"].strip()
        assert source_quote_ids
        assert source_quote_ids <= supporting_evidence_ids
        assert source_quote_ids <= set(quote_index)
        assert source_quote_ids.isdisjoint(revoked_quote_ids)
        assert counter_evidence_ids <= set(quote_index)
        assert card["redaction_state"] in {"redacted", "aggregate_only"}

        for quote_id in source_quote_ids:
            evidence, quote = quote_index[quote_id]
            assert evidence["consent_state"] in {"granted", "limited"}
            assert evidence["redaction_state"] == "redacted"
            assert quote["allowed_for_insight"] is True
            assert quote["redacted_quote"].strip()

        handoff = card["recommended_handoff"]
        assert handoff["owner_pack"]
        assert handoff["reason"]
        assert handoff["artifact_path"]


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
