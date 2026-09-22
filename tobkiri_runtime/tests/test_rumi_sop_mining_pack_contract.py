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
PACK_ID = "rumi_sop_mining_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = [
    "ecosystem.json",
    "README.md",
    "docs/README.md",
    "docs/architecture.md",
    "docs/interfaces.md",
    "docs/operations.md",
    "catalog/approval_state_machine.yaml",
    "catalog/sop_mining_workflows.yaml",
    "catalog/sop_mining_quality_matrix.yaml",
    "catalog/sop_step_taxonomy.yaml",
    "schemas/trace_evidence_record.schema.json",
    "schemas/sop_mining_record.schema.json",
    "schemas/workflow_recipe.schema.json",
    "schemas/sop_trace.schema.json",
    "policies/sop_mining_safety.policy.yaml",
    "policies/trace_redaction.policy.yaml",
    "checklists/sop_mining_review.checklist.yaml",
    "ledgers/sop_mining_evidence_ledger.schema.yaml",
    "runbooks/sop_mining_runbook.template.yaml",
    "templates/assumption_log.template.md",
    "templates/runbook.template.md",
    "templates/sop_mining_handoff.template.md",
    "profiles/process_cartographer.profile.yaml",
    "prompts/sop_extractor.system.md",
    "presets/safe_default.preset.yaml",
    "presets/handoff_review.preset.yaml",
    "presets/quality_gate.preset.yaml",
    "examples/bug_triage_trace.example.yaml",
    "examples/support_macro_trace.example.yaml",
    "examples/release_checklist_trace.example.yaml",
]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
    assert {
        item["pack_id"] for item in ecosystem["metadata"]["optional_integrations"]
    } >= {
        "rumi_observability_pack",
        "rumi_agentic_qa_pack",
        "rumi_security_review_pack",
        "rumi_browser_automation_pack",
        "rumi_workflow_scheduler_pack",
        "rumi_computer_control_pack",
        "rumi_default_tools_pack",
    }

    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "trace_redaction_contract",
        "trace_schema",
        "sop_extraction",
        "assumption_log",
        "runbook_template",
        "human_approval_gate",
        "source_consent_review",
        "non_execution_boundary_review",
    }
    assert set(ecosystem["metadata"]["excluded_surfaces"]) >= {
        "automation_execution",
        "browser_control",
        "computer_control",
        "schedule_creation",
        "scheduled_jobs",
        "tool_creation_or_invocation",
        "live_trace_capture",
        "long_term_run_ledger_storage",
    }

    indexed = {
        item
        for values in ecosystem["metadata"]["asset_index"].values()
        for item in values
    }
    all_pack_files = {
        str(path.relative_to(PACK_DIR)).replace("\\", "/")
        for path in PACK_DIR.rglob("*")
        if path.is_file() and path.name != "executables.v4.json"
    }
    all_pack_files -= V4_AUTHORITY_ARTIFACTS
    assert all_pack_files == set(REQUIRED_ASSETS)
    assert set(REQUIRED_ASSETS) - {"ecosystem.json"} <= indexed
    assert [asset for asset in sorted(indexed) if not (PACK_DIR / asset).is_file()] == []


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(load_yaml(path), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(read_json(path), dict), path


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

    assert candidate.overlap_policy["automation_execution"].startswith("blocked_handoff")
    assert candidate.overlap_policy["browser_control"] == "blocked_handoff_to_rumi_browser_automation_pack"
    assert candidate.overlap_policy["computer_control"] == "blocked_handoff_to_rumi_computer_control_pack"
    assert candidate.overlap_policy["schedule_creation"] == "blocked_handoff_to_rumi_workflow_scheduler_pack"
    assert candidate.overlap_policy["live_trace_capture"] == "blocked_handoff_to_rumi_observability_pack"
    assert candidate.overlap_policy["run_ledger_storage"] == "blocked_handoff_to_rumi_observability_pack"
    assert candidate.overlap_policy["tool_creation_or_invocation"] == "blocked_handoff_to_rumi_default_tools_pack"
    assert candidate.overlap_policy["sop_extraction"] == "owned_by_rumi_sop_mining_pack"
    assert candidate.overlap_policy["trace_schema"] == "owned_by_rumi_sop_mining_pack"

    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= {
        "no_automation_execution_runtime",
        "does_not_control_browsers",
        "does_not_control_computers",
        "does_not_create_schedules_or_jobs",
        "does_not_store_long_term_observability_ledgers",
        "does_not_create_or_invoke_tools",
        "requires_trace_redaction_review",
        "requires_human_approval_before_recipe_use",
    }
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "process-mining"
    assert candidate.signing["mode"] == "repository_reviewed"
    assert candidate.signing["verified"] is True


def test_pack_semantic_contract_assets() -> None:
    trace_schema = read_json(PACK_DIR / "schemas/trace_evidence_record.schema.json")
    record_schema = read_json(PACK_DIR / "schemas/sop_mining_record.schema.json")
    workflows = load_yaml(PACK_DIR / "catalog/sop_mining_workflows.yaml")["workflows"]
    matrix = load_yaml(PACK_DIR / "catalog/sop_mining_quality_matrix.yaml")["quality_matrix"]
    policy = load_yaml(PACK_DIR / "policies/sop_mining_safety.policy.yaml")["policy"]
    checklist = load_yaml(PACK_DIR / "checklists/sop_mining_review.checklist.yaml")["review_checklist"]
    ledger = load_yaml(PACK_DIR / "ledgers/sop_mining_evidence_ledger.schema.yaml")["evidence_ledger_schema"]
    runbook = load_yaml(PACK_DIR / "runbooks/sop_mining_runbook.template.yaml")["runbook_template"]
    template = (PACK_DIR / "templates/sop_mining_handoff.template.md").read_text(encoding="utf-8")

    assert trace_schema["additionalProperties"] is False
    assert set(trace_schema["required"]) >= {
        "trace_id",
        "source_type",
        "evidence_ref",
        "content_summary",
        "redaction_state",
        "redaction_actions",
        "sensitive_data_classes_found",
        "consent_basis",
        "scope_boundary",
        "review_status",
        "raw_payload_included",
    }
    assert trace_schema["properties"]["raw_payload_included"]["const"] is False
    assert {"chat_message", "tool_call", "tool_result", "audit_log", "test_output"} <= set(
        trace_schema["properties"]["source_type"]["enum"]
    )
    assert {"redacted", "no_sensitive_data", "blocked_sensitive_data"} <= set(
        trace_schema["properties"]["redaction_state"]["enum"]
    )

    assert set(record_schema["required"]) >= {
        "approval_state",
        "human_approval",
        "non_execution_boundary",
        "redaction_state",
        "events",
        "source_evidence",
        "trace_id",
        "assumptions",
        "sop_output",
    }
    approval = record_schema["properties"]["human_approval"]
    assert approval["properties"]["required"]["const"] is True
    assert {"approver_role", "approved_at", "approval_record_ref"} <= set(approval["required"])
    boundary = record_schema["properties"]["non_execution_boundary"]
    assert set(boundary["required"]) >= {
        "automation_execution",
        "browser_control",
        "computer_control",
        "schedule_creation",
        "run_ledger_storage",
        "tool_creation_or_invocation",
    }
    assert all(boundary["properties"][key]["const"] == "handoff_required" for key in boundary["required"])

    assert workflows["default_execution"] == "no_runtime_action"
    assert {"automation_execution", "browser_control", "computer_control", "schedule_creation", "run_ledger_storage"} <= set(
        workflows["blocked_actions"]
    )
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert all(item["promotion_requires_human_approval"] is True for item in workflows["items"])

    matrix_checks = {item["id"]: item for item in matrix["checks"]}
    assert {"trace_schema_valid", "source_consent_recorded", "human_owner_approved"} <= set(matrix_checks)
    assert all(matrix_checks[item]["blocking"] is True for item in ["trace_schema_valid", "human_owner_approved"])

    blocked = set(policy["blocked_by_default"])
    assert {
        "raw secrets in trace evidence",
        "unreviewed personal data",
        "automation execution",
        "browser control",
        "computer control",
        "schedule creation",
        "live trace capture",
        "long-term run ledger storage",
        "tool creation or invocation",
    } <= blocked
    assert {
        "redaction_complete",
        "human_owner_approved",
        "approver_identity_recorded",
        "approved_at_recorded",
        "non_execution_boundary_confirmed",
    } <= set(policy["approval_gate"]["promotion_requires"])

    checklist_ids = {item["id"] for item in checklist["required_checks"] if item["blocking"]}
    assert {"trace_schema_valid", "source_consent_recorded", "approved_at_recorded", "observability_handoff_named"} <= checklist_ids

    assert ledger["completion_rules"]["every_record_has_evidence"] is True
    assert ledger["completion_rules"]["every_trace_has_redaction_state"] is True
    assert ledger["completion_rules"]["every_promotion_has_human_approval"] is True
    assert ledger["completion_rules"]["observability_storage_requires_handoff"] is True
    assert ledger["completion_rules"]["no_runtime_grant_created"] is True
    assert ledger["completion_rules"]["no_raw_payload_storage"] is True

    assert runbook["mode"] == "declarative_only"
    assert {"human_approval_gate", "ledger_update", "trace_schema_review"} <= set(runbook["required_sections"])
    assert "human_owner_approved" in runbook["promotion_gate"]["blocking_requirements"]
    assert {"automation_execution", "browser_control", "schedule_creation", "long_term_run_ledger_storage"} <= set(
        runbook["blocked_actions"]
    )
    assert "Human Approval" in template
    assert "Non-Execution Boundary" in template


def test_examples_are_redacted_human_approved_handoff_packets() -> None:
    examples = [
        load_yaml(path)["example"]
        for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))
    ]
    assert len(examples) == 3
    for example in examples:
        assert example["expected_result"] == "sop_mining_handoff_packet"
        assert example["trace_record"]["raw_payload_included"] is False
        assert example["trace_record"]["redaction_state"] in {"redacted", "no_sensitive_data"}
        assert example["trace_record"]["consent_basis"]
        assert example["approval_state"] == "approved"
        assert example["human_approval"]["approver_role"]
        assert example["human_approval"]["approval_record_ref"]
        assert {"automation_execution", "browser_control", "schedule_creation", "run_ledger_storage"} <= set(
            example["non_execution_boundary"]
        )
        assert "rumi_observability_pack" in example["handoffs"]


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
        "browser control",
        "schedule creation",
        "observability",
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
