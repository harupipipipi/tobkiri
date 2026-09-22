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
PACK_ID = 'rumi_study_coach_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = ['README.md', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'examples/biology_uncertainty_report.example.yaml', 'examples/history_exam_plan.example.yaml', 'examples/language_vocab_review.example.yaml', 'examples/math_notes_quiz.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/study_coach.profile.yaml', 'prompts/socratic_tutor.system.md', 'schemas/diagnostic_assessment.schema.json', 'schemas/learner_profile.schema.json', 'schemas/practice_session.schema.json', 'schemas/progress_report.schema.json', 'schemas/review_queue.schema.json', 'schemas/study_goal.schema.json', 'schemas/study_plan.schema.json', 'templates/artifact_card.template.md', 'templates/handoff.template.md', 'templates/review_report.template.md']
SCHEMA_EXPECTATIONS = {'schemas/learner_profile.schema.json': ['learner_id', 'goals', 'constraints', 'preferred_explanation_style', 'source_scope', 'accommodations'], 'schemas/study_goal.schema.json': ['study_goal_id', 'learner_id', 'target_outcome', 'success_criteria', 'deadline_state', 'evidence_basis', 'handoff_owner'], 'schemas/diagnostic_assessment.schema.json': ['assessment_id', 'study_goal_id', 'skill_map', 'question_set', 'uncertainty_notes', 'evidence_summary'], 'schemas/study_plan.schema.json': ['plan_id', 'study_goal_id', 'sessions', 'review_windows', 'source_note_ids', 'scheduler_handoff'], 'schemas/practice_session.schema.json': ['practice_session_id', 'study_goal_id', 'quiz_items', 'source_note_ids', 'feedback_contract', 'uncertainty'], 'schemas/review_queue.schema.json': ['queue_id', 'learner_id', 'items', 'next_review_reason', 'decay_model', 'handoff_owner'], 'schemas/progress_report.schema.json': ['report_id', 'study_goal_id', 'mastery_estimates', 'evidence_summary', 'uncertainty_notes', 'recommended_handoffs']}
WORKFLOW_IDS = set(['local_note_intake_contract', 'skill_gap_diagnosis', 'study_plan_generation', 'practice_session_quiz_builder', 'spaced_review_practice_loop', 'progress_report_handoff'])
QUALITY_CHECK_IDS = set(['learner_goal_alignment', 'source_note_citation', 'uncertainty_when_notes_insufficient', 'no_external_fact_invention', 'spacing_reason_present', 'difficulty_calibration', 'accessibility_constraint_respected', 'handoff_owner_named'])
OWNER_EXPECTED = set(['learner_profile', 'study_goal_contract', 'diagnostic_assessment', 'study_plan', 'practice_session', 'quiz_item_contract', 'spaced_review_queue', 'progress_report', 'evidence_bound_explanation'])
NON_OWNER_EXPECTED = set(['document parsing', 'web research', 'long term memory storage', 'calendar scheduling', 'workspace export', 'medical or therapeutic advice', 'graded credential issuance'])
OVERLAP_EXPECTED = {'document_parsing': 'handoff_to_rumi_document_intelligence_pack', 'web_research': 'handoff_to_rumi_research_pack', 'long_term_memory': 'handoff_to_rumi_memory_knowledge_pack', 'review_scheduling': 'handoff_to_rumi_workflow_scheduler_pack', 'study_artifact_export': 'handoff_to_rumi_workspace_pack', 'diagnostic_assessment': 'owned_by_rumi_study_coach_pack', 'quiz_item_contract': 'owned_by_rumi_study_coach_pack', 'spaced_review_queue': 'owned_by_rumi_study_coach_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['requires_user_learning_goals', 'requires_local_note_evidence', 'scheduling_owned_by_workflow_scheduler_pack', 'memory_storage_owned_by_rumi_memory_knowledge_pack', 'must_prove_uncertainty_when_notes_are_insufficient'])
PROMOTION_EVIDENCE = set(['quiz_item_note_citation_cases', 'insufficient_note_uncertainty_cases', 'review_queue_decay_cases', 'learner_constraint_accessibility_cases', 'study_plan_to_scheduler_handoff_cases'])
BLOCKED_BY_DEFAULT = set(['claim mastery without local note evidence', 'invent facts absent from notes', 'schedule reminders without workflow scheduler handoff', 'store durable learner memory inside this pack', 'present medical or therapeutic advice as tutoring'])


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
        "requires": ["defaultspack"],
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
    assert ecosystem["metadata"]["defaultspack_promotion_eligible"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(ecosystem["metadata"]["non_owner_surfaces"]) >= NON_OWNER_EXPECTED

    metadata_indexed = {item for values in ecosystem["metadata"]["asset_index"].values() for item in values}
    actual = {
        str(path.relative_to(PACK_DIR)).replace("\\", "/")
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
        "required_network": {
            "allowed_domains": [],
            "allowed_ports": [],
        },
        "executable_code": False,
        "supports_all_ok": False,
        "external_actions_are_handoffs": True,
        "defaultspack_promotion_eligible": False,
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
    assert setup["risk_level"] == 'low'
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

    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'learning'
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


def test_fixtures_and_examples_preserve_local_note_citation_semantics() -> None:
    fixture = read_yaml(PACK_DIR / "fixtures/contract_fixture.yaml")["contract_fixture"]
    positive = {case["id"]: case for case in fixture["positive_cases"]}["math_notes_quiz_positive"]
    source_note_ids = {note["note_id"] for note in positive["given"]["source_notes"]}
    expected = positive["expected"]

    assert expected["artifact_type"] == "practice_session"
    assert set(expected["source_note_ids"]) <= source_note_ids
    assert expected["uncertainty"] == "low"
    assert expected["quiz_items"]
    for item in expected["quiz_items"]:
        assert set(item["source_note_ids"]) <= source_note_ids
        assert item["source_span_ids"]
        assert item["uncertainty"] == "low"
    assert "all_answers_cite_local_note_ids" in positive["semantic_checks"]
    assert "uncertainty_recorded_when_notes_are_insufficient" in fixture["minimum_expected"]

    negative_cases = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]["cases"]
    negative = {case["id"]: case for case in negative_cases}
    missing_note = negative["missing_note_negative"]["expected"]
    assert missing_note["decision"] == "blocked_packet"
    assert missing_note["uncertainty"] == "high_due_to_missing_notes"
    assert {"uncertainty_notes", "safe_next_step", "requested_local_note_evidence"} <= set(missing_note["required_fields"])
    assert {"fabricated_answer", "uncited_claim", "external_research"} <= set(missing_note["forbidden"])

    scheduler = negative["scheduler_boundary_negative"]["expected"]
    assert scheduler["decision"] == "handoff_packet"
    assert scheduler["owner_pack"] == "rumi_workflow_scheduler_pack"
    assert {"calendar_write", "reminder_creation", "durable_memory_write"} <= set(scheduler["forbidden"])

    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert {example["id"] for example in examples} == {
        "biology_uncertainty_report",
        "history_exam_plan",
        "language_vocab_review",
        "math_notes_quiz",
    }
    for example in examples:
        local_note_ids = {note["note_id"] for note in example["local_notes"]}
        packet = example["expected_packet"]
        assert set(packet["source_note_ids"]) <= local_note_ids
        assert packet["handoff_owner"]
        assert packet["uncertainty_or_limitations"]
        assert any("note_" in note_id for note_id in packet["source_note_ids"])

    biology_packet = {example["id"]: example for example in examples}["biology_uncertainty_report"]["expected_packet"]
    assert biology_packet["uncertainty_level"] == "high_due_to_missing_notes"
    assert biology_packet["uncertainty_notes"]
    assert all("safe_next_step" in note for note in biology_packet["uncertainty_notes"])
    assert any("insufficient" in note["reason"] or "missing" in note["reason"] for note in biology_packet["uncertainty_notes"])


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
