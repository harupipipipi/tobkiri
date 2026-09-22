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
PACK_ID = "rumi_meeting_intelligence_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


REQUIRED_ASSETS = [
    "ecosystem.json",
    "asset_index.yaml",
    "README.md",
    "docs/README.md",
    "docs/architecture.md",
    "docs/interfaces.md",
    "docs/operations.md",
    "catalog/meeting_intelligence_workflows.yaml",
    "catalog/meeting_intelligence_quality_matrix.yaml",
    "catalog/participant_consent_matrix.yaml",
    "schemas/action_item.schema.json",
    "schemas/meeting_intelligence_record.schema.json",
    "policies/meeting_intelligence_safety.policy.yaml",
    "checklists/meeting_intelligence_review.checklist.yaml",
    "ledgers/meeting_intelligence_evidence_ledger.schema.yaml",
    "templates/followup_email.template.md",
    "templates/meeting_intelligence_handoff.template.md",
    "profiles/meeting_chief.profile.yaml",
    "prompts/meeting_recap_scribe.system.md",
    "presets/safe_default.preset.yaml",
    "presets/handoff_review.preset.yaml",
    "presets/quality_gate.preset.yaml",
    "examples/weekly_sync_recap.example.yaml",
    "examples/sales_call_followup.example.yaml",
    "examples/incident_review_decisions.example.yaml",
]


def test_pack_required_assets_and_metadata() -> None:
    assert [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()] == []

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
    assert ecosystem["metadata"]["declarative_only"] is True
    assert ecosystem["metadata"]["consumes_existing_sources_only"] is True
    assert ecosystem["metadata"]["output_effect"] == "draft_and_handoff_only"

    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "meeting_prebrief",
        "transcript_to_decisions",
        "decision_log",
        "action_item_extraction",
        "evidence_linked_recap",
        "followup_draft_contract",
        "evidence_linked_recap_bundle",
        "meeting_artifact_quality_gate",
    }
    assert set(ecosystem["metadata"]["non_owner_surfaces"]) >= {
        "connectors",
        "scheduler",
        "voice_capture",
        "business_ops_workflows",
        "document_parsing",
        "message_sending",
        "calendar_booking",
    }

    indexed = {item for values in ecosystem["metadata"]["asset_index"].values() for item in values}
    assert set(REQUIRED_ASSETS) - {"ecosystem.json"} <= indexed

    asset_index = yaml.safe_load((PACK_DIR / "asset_index.yaml").read_text(encoding="utf-8"))[
        "asset_index"
    ]
    indexed_file_assets = {
        item for values in asset_index["categories"].values() for item in values
    }
    assert set(REQUIRED_ASSETS) - {"ecosystem.json"} <= indexed_file_assets
    assert asset_index["invariants"] == {
        "required_secrets": [],
        "required_network": {
            "allowed_domains": [],
            "allowed_ports": [],
        },
        "executable_code": False,
        "supports_all_ok": False,
        "external_actions_are_handoffs": True,
    }


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
    assert setup["compatibility"]["python"] == ">=3.9"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []

    assert candidate.overlap_policy["connector_delivery"] == "handoff_to_rumi_connector_gateway_pack"
    assert (
        candidate.overlap_policy["calendar_or_reminder_scheduling"]
        == "handoff_to_rumi_workflow_scheduler_pack"
    )
    assert candidate.overlap_policy["voice_capture"] == "handoff_to_rumi_voice_mobile_pack"
    assert candidate.overlap_policy["document_parsing"] == "handoff_to_rumi_document_intelligence_pack"
    assert candidate.overlap_policy["business_workflow_execution"] == "handoff_to_rumi_business_ops_pack"
    assert candidate.overlap_policy["transcript_to_decisions"] == f"owned_by_{PACK_ID}"
    assert candidate.overlap_policy["action_extraction"] == f"owned_by_{PACK_ID}"
    assert candidate.overlap_policy["evidence_linked_recap_bundle"] == f"owned_by_{PACK_ID}"

    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= {
        "no_connector_delivery_runtime",
        "no_calendar_write_runtime",
        "no_business_ops_execution",
        "no_document_parser_ownership",
        "no_message_send_runtime",
        "no_voice_capture_runtime",
        "human_review_required_before_external_action",
    }
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= {
        "participant_consent_review_cases",
        "decision_log_source_span_cases",
        "followup_delivery_handoff_cases",
        "local_first_privacy_review",
        "evidence_linked_recap_examples_reviewed",
        "handoff_surface_acceptance_by_owner_packs",
    }
    assert candidate.marketplace["id"] == "rumi.meeting_intelligence_pack"
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "meeting-intelligence"
    assert candidate.signing["verified"] is True


def test_pack_semantic_contract_assets() -> None:
    record_schema = read_json(PACK_DIR / "schemas/meeting_intelligence_record.schema.json")
    workflows = yaml.safe_load(
        (PACK_DIR / "catalog/meeting_intelligence_workflows.yaml").read_text(encoding="utf-8")
    )["workflows"]
    matrix = yaml.safe_load(
        (PACK_DIR / "catalog/meeting_intelligence_quality_matrix.yaml").read_text(encoding="utf-8")
    )["quality_matrix"]
    policy = yaml.safe_load(
        (PACK_DIR / "policies/meeting_intelligence_safety.policy.yaml").read_text(encoding="utf-8")
    )["policy"]
    checklist = yaml.safe_load(
        (PACK_DIR / "checklists/meeting_intelligence_review.checklist.yaml").read_text(
            encoding="utf-8"
        )
    )["review_checklist"]
    ledger = yaml.safe_load(
        (PACK_DIR / "ledgers/meeting_intelligence_evidence_ledger.schema.yaml").read_text(
            encoding="utf-8"
        )
    )["evidence_ledger_schema"]
    template = (PACK_DIR / "templates/meeting_intelligence_handoff.template.md").read_text(
        encoding="utf-8"
    )

    assert set(record_schema["required"]) >= {
        "record_id",
        "meeting",
        "source_evidence",
        "decision_records",
        "action_items",
        "follow_up_drafts",
        "recap_bundle",
        "handoff_owner",
        "review_state",
    }
    assert record_schema["properties"]["meeting"]["properties"]["local_only"]["const"] is True
    assert record_schema["properties"]["source_evidence"]["type"] == "array"
    assert record_schema["properties"]["source_evidence"]["items"]["required"] == [
        "source_id",
        "source_type",
        "source_span",
        "excerpt_summary",
    ]
    decision = record_schema["properties"]["decision_records"]["items"]
    assert "source_evidence_ids" in decision["required"]
    action = record_schema["properties"]["action_items"]["items"]
    assert {"owner", "due_date_confidence", "handoff_owner", "source_evidence_ids"} <= set(
        action["required"]
    )
    assert action["properties"]["due_date_confidence"]["enum"] == [
        "explicit",
        "inferred",
        "unknown",
    ]
    followup = record_schema["properties"]["follow_up_drafts"]["items"]
    assert followup["properties"]["draft_only"]["const"] is True
    assert followup["properties"]["human_review_required"]["const"] is True
    recap = record_schema["properties"]["recap_bundle"]
    assert {"decision_ids", "action_ids", "evidence_ids", "open_questions"} <= set(
        recap["required"]
    )

    workflow_ids = {item["id"] for item in workflows["items"]}
    assert workflow_ids == {
        "prebrief_from_local_context",
        "transcript_to_decision_log",
        "action_extraction_register",
        "followup_packet",
        "evidence_recap_bundle",
    }
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert workflows["default_execution"] == "no_runtime_action"
    assert set(workflows["ownership"]["handoff"]) >= {
        "connectors",
        "scheduler",
        "voice_capture",
        "business_ops_workflows",
        "document_parsing",
    }

    check_ids = {item["id"] for item in matrix["checks"]}
    assert {
        "source_spans",
        "participant_consent",
        "owner_for_each_action",
        "draft_only_followups",
        "no_external_execution",
        "recap_bundle_integrity",
    } <= check_ids
    assert matrix["minimum_pass"] == "all_blocking_checks"
    assert "fail_boundary_violation" in matrix["fail_states"]

    assert set(policy["blocked_by_default"]) >= {
        "send follow-up without participant consent",
        "invent absent attendee decisions",
        "schedule calendar changes from inferred action items",
        "fetch remote calendar, mail, chat, or drive context directly",
        "record or transcribe live meeting audio",
        "parse attached documents directly",
        "execute CRM, ticket, project, or business workflow updates",
    }
    assert set(policy["handoff_required_for"]) >= {
        "connector_delivery",
        "calendar_or_reminder_scheduling",
        "voice_capture",
        "document_parsing",
        "business_workflow_execution",
    }

    required_check_ids = {item["id"] for item in checklist["required_checks"]}
    assert {
        "source_spans",
        "participant_consent",
        "owner_for_each_action",
        "draft_only_followups",
        "recap_bundle_evidence",
        "no_adjacent_execution",
    } <= required_check_ids
    assert any(item["blocking"] for item in checklist["required_checks"])

    assert ledger["completion_rules"]["every_record_has_evidence"] is True
    assert ledger["completion_rules"]["every_action_has_owner_or_unknown_marker"] is True
    assert ledger["completion_rules"]["every_followup_is_draft_only"] is True
    assert ledger["completion_rules"]["every_recap_claim_links_to_evidence"] is True
    assert ledger["completion_rules"]["adjacent_execution_requires_handoff"] is True
    assert set(ledger["allowed_claim_types"]) >= {
        "decision_record",
        "action_item",
        "follow_up_draft",
        "recap_claim",
    }

    for expected in [
        "Evidence",
        "Decision Records",
        "Action Register",
        "Follow-Up Drafts",
        "Recap Bundle",
        "Boundary Notes",
    ]:
        assert expected in template
    assert "Draft-only" in template


def test_pack_examples_are_concrete_and_draft_only() -> None:
    examples = [
        yaml.safe_load(path.read_text(encoding="utf-8"))["example"]
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert len(examples) == 3
    assert {item["expected_result"] for item in examples} == {
        "evidence_linked_recap_bundle",
        "follow_up_draft_handoff_packet",
        "transcript_to_decision_log",
    }
    for example in examples:
        assert example["inputs"]
        assert all({"source_type", "source_id", "source_span"} <= set(item) for item in example["inputs"])
        assert example["draft_only"] is True
        assert example["human_review_required"] is True
        assert {"decision_records", "action_items", "evidence_ledger_entries"} <= set(
            example["required_outputs"]
        )
        assert {"rumi_connector_gateway_pack", "rumi_workflow_scheduler_pack"} <= set(
            example["handoffs"]
        )


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md"]
    )
    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "Handoff",
        "evidence",
        "Does Not Provide",
        "No connector fetching or sending",
        "No calendar booking or reminders",
        "No voice capture or transcription",
        "No business workflow execution",
        "No document parsing",
    ]:
        assert expected in docs

    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
    combined = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    for phrase in ["sample user request", "reviewer_ready_plan", "Complementary owner surface"]:
        assert phrase not in combined
