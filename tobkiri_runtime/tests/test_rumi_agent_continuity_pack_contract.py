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
PACK_ID = "rumi_agent_continuity_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = [
    "README.md",
    "asset_index.json",
    "asset_index.yaml",
    "catalog/handoff_matrix.yaml",
    "catalog/quality_matrix.yaml",
    "catalog/taxonomy.yaml",
    "catalog/workflows.yaml",
    "checklists/review.checklist.yaml",
    "docs/README.md",
    "docs/architecture.md",
    "docs/interfaces.md",
    "docs/operations.md",
    "docs/overlap_policy.md",
    "examples/attention_drift_recovery.example.yaml",
    "examples/branch_resume_packet.example.yaml",
    "examples/compaction_handoff.example.yaml",
    "examples/health_note.example.yaml",
    "examples/long_running_agent_resume.example.yaml",
    "fixtures/contract_fixture.yaml",
    "fixtures/negative_cases.yaml",
    "ledgers/continuity_evidence_ledger.schema.yaml",
    "policies/compaction_handoff.policy.yaml",
    "policies/continuity_safety.policy.yaml",
    "policies/handoff.policy.yaml",
    "policies/safety.policy.yaml",
    "presets/handoff_review.preset.yaml",
    "presets/quality_gate.preset.yaml",
    "presets/safe_default.preset.yaml",
    "profiles/continuity_reviewer.profile.yaml",
    "prompts/continuity_reviewer.system.md",
    "schemas/attention_drift_recovery.schema.json",
    "schemas/branch_resume_packet.schema.json",
    "schemas/compaction_handoff.schema.json",
    "schemas/continuity_artifact_manifest.schema.json",
    "schemas/continuity_packet.schema.json",
    "schemas/health_note.schema.json",
    "schemas/restart_evidence.schema.json",
    "schemas/run_summary.schema.json",
    "templates/handoff.template.md",
    "templates/review_report.template.md",
    "templates/ui_contract.template.md",
]

SCHEMA_EXPECTATIONS = {
    "schemas/continuity_packet.schema.json": [
        "packet_id",
        "source_pack_id",
        "run_ref",
        "created_at",
        "continuity_state",
        "resume_goal",
        "current_focus",
        "completed_work",
        "remaining_work",
        "open_questions",
        "evidence_refs",
        "handoff_targets",
        "review_state",
    ],
    "schemas/restart_evidence.schema.json": [
        "evidence_id",
        "packet_id",
        "human_summary",
        "last_known_good_state",
        "safe_next_action",
        "blocked_or_risky_actions",
        "verification_refs",
        "redaction_state",
    ],
    "schemas/compaction_handoff.schema.json": [
        "handoff_id",
        "conversation_ref",
        "pre_compaction_anchor",
        "post_compaction_summary",
        "lost_context_risks",
        "must_preserve",
        "resume_instructions",
        "owner_boundary_review",
    ],
    "schemas/attention_drift_recovery.schema.json": [
        "drift_id",
        "detected_signal",
        "expected_focus",
        "observed_drift",
        "recovery_prompt",
        "discarded_threads",
        "next_check",
    ],
    "schemas/run_summary.schema.json": [
        "summary_id",
        "run_ref",
        "objective",
        "status_snapshot",
        "decision_log",
        "files_or_artifacts_touched_refs",
        "tests_or_checks_refs",
        "known_gaps",
        "next_actions",
    ],
    "schemas/continuity_artifact_manifest.schema.json": [
        "manifest_id",
        "packet_id",
        "artifact_refs",
        "artifact_roles",
        "path_or_uri_redaction_state",
        "workspace_owner",
        "persistence_owner",
        "reference_only",
    ],
    "schemas/branch_resume_packet.schema.json": [
        "packet_id",
        "branch_ref",
        "base_ref",
        "worktree_ref",
        "resume_command_or_instruction",
        "diff_summary_ref",
        "uncommitted_state_note",
        "handoff_owner",
        "branch_mutation_allowed",
    ],
    "schemas/health_note.schema.json": [
        "note_id",
        "run_ref",
        "health_status",
        "attention_state",
        "context_budget_state",
        "staleness_risk",
        "handoff_readiness",
        "human_readable_note",
    ],
}

WORKFLOW_IDS = {
    "attention_drift_recovery",
    "branch_resume_packet",
    "compaction_handoff",
    "health_note_review",
    "long_running_resume",
    "run_summary_refresh",
}
QUALITY_CHECK_IDS = {
    "artifact_manifest_reference_only",
    "asset_index_complete",
    "branch_mutation_handoff_only",
    "lost_context_risks_named",
    "owner_boundaries_reviewed",
    "restart_evidence_human_readable",
    "safe_next_action_present",
}
OWNER_EXPECTED = {
    "agent_health_note",
    "attention_drift_recovery_note",
    "branch_resume_packet",
    "compaction_handoff_packet",
    "continuity_artifact_manifest",
    "continuity_packet_contract",
    "restart_evidence_packet",
    "run_summary_for_resume",
}
NON_OWNER_EXPECTED = {
    "file artifact persistence and export",
    "git branch creation switching push",
    "memory objects recall storage knowledge updates skill learning",
    "metrics telemetry run ledgers cost latency postmortems",
    "run boards live run events checkpoints interventions replay indexes",
    "schedules monitors wakeups retries recurring follow-ups",
}
OVERLAP_EXPECTED = {
    "run_board_events_checkpoints": "handoff_to_rumi_agent_workroom_pack",
    "memory_storage_recall": "handoff_to_rumi_memory_knowledge_pack",
    "metrics_telemetry_ledgers": "handoff_to_rumi_observability_pack",
    "schedules_wakeups_retries": "handoff_to_rumi_workflow_scheduler_pack",
    "artifact_persistence_export": "handoff_to_rumi_workspace_pack",
    "git_branch_mutation": "handoff_to_defaultspack_or_coding_owner",
    "continuity_packets": "owned_by_rumi_agent_continuity_pack",
    "restart_evidence": "owned_by_rumi_agent_continuity_pack",
    "tool_aliases": "prefer_explicit_pack_namespace",
}
PROMOTION_BLOCKERS = {
    "branch_mutation_owned_elsewhere",
    "declarative_only_no_runtime_resume_engine",
    "memory_pack_owns_persistent_knowledge",
    "no_compaction_runtime_authority",
    "no_durable_continuity_store",
    "observability_pack_owns_metrics_and_ledgers",
    "requires_real_restart_success_cases",
    "scheduler_pack_owns_wakeups_and_recurring_runs",
    "workroom_owns_run_events_and_checkpoints",
    "workspace_pack_owns_artifact_persistence",
}
PROMOTION_EVIDENCE = {
    "attention_drift_recovery_cases",
    "branch_resume_packet_cases",
    "compaction_handoff_cases",
    "health_note_review_cases",
    "long_running_resume_cases",
    "real_restart_success_cases",
}
BLOCKED_BY_DEFAULT = {
    "artifact persistence",
    "branch mutation",
    "checkpoint ownership",
    "file mutation",
    "memory writes",
    "metrics collection",
    "replay index ownership",
    "runtime action",
    "schedule creation",
}
FORBIDDEN_OWNER_SURFACES = {
    "run_event",
    "checkpoint",
    "run_board_view",
    "cost_latency",
    "cron_like",
    "runtime_memory_writes",
}
FORBIDDEN_SCHEMA_PROPERTIES = {
    "event_log_id",
    "checkpoint_ids",
    "replay_index_refs",
    "memory_object_id",
    "metric_id",
    "schedule_id",
    "cron_expression",
    "checksum",
    "export_targets",
    "branch_create",
    "branch_switch",
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


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def test_required_assets_and_ecosystem_contract() -> None:
    assert [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["host_execution"] is False
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []

    metadata = ecosystem["metadata"]
    assert metadata["runtime_type"] == "declarative_setup_pack"
    assert ecosystem["runtime"]["type"] == "declarative_pack"
    assert metadata["network_policy"] == "none_by_default"
    assert metadata["executable_code"] is False
    assert metadata["declarative_only"] is True
    assert metadata["output_effect"] == "restart_evidence_and_handoff_only"
    assert metadata["base_pack_promotion_eligible"] is False
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    assert not (FORBIDDEN_OWNER_SURFACES & set(metadata["owner_surfaces"]))

    actual = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name not in {"ecosystem.json", "executables.v4.json"}
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
        read_yaml(path)
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
    assert candidate.base_pack_promotion["eligible"] is False
    assert set(candidate.base_pack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.base_pack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "agent-continuity"
    assert candidate.signing["verified"] is True


def test_pack_v4_contract_carries_setup_dependencies() -> None:
    setup = read_json(SETUP_PACK_JSON)
    manifest = read_json(PACK_DIR / "pack.v4.json")
    setup_dependencies = {
        item["pack_id"]: item["version"] for item in setup["depends_on"]
    }

    assert manifest["pack"]["id"] == PACK_ID
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
        assert not (FORBIDDEN_SCHEMA_PROPERTIES & set(schema["properties"])), rel_path

    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    continuity_policy = read_yaml(PACK_DIR / "policies/continuity_safety.policy.yaml")["policy"]
    compaction_policy = read_yaml(PACK_DIR / "policies/compaction_handoff.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    ledger = read_yaml(next((PACK_DIR / "ledgers").glob("*.yaml")))["evidence_ledger_schema"]

    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"

    for policy_doc in (policy, continuity_policy):
        assert set(policy_doc["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
        assert policy_doc["external_effect"] == "handoff_packet_only"
    assert compaction_policy["requires_before_after_evidence"] is True
    assert compaction_policy["requires_human_readable_restart_note"] is True
    assert compaction_policy["lost_context_risks_required"] is True

    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    for key, expected in OVERLAP_EXPECTED.items():
        assert handoff_policy["overlap_policy"][key] == expected
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    assert {
        item["surface"]: item["resolution"] for item in handoff_matrix["items"]
    } == OVERLAP_EXPECTED
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"


def test_reference_only_artifact_manifest_and_branch_resume_boundaries() -> None:
    artifact = read_json(PACK_DIR / "schemas/continuity_artifact_manifest.schema.json")
    branch = read_json(PACK_DIR / "schemas/branch_resume_packet.schema.json")
    assert artifact["properties"]["reference_only"]["const"] is True
    assert artifact["properties"]["workspace_owner"]["const"] == "rumi_workspace_pack"
    assert artifact["properties"]["persistence_owner"]["const"] == "rumi_workspace_pack"
    assert "checksum" not in artifact["properties"]
    assert "export_targets" not in artifact["properties"]
    assert branch["properties"]["branch_mutation_allowed"]["const"] is False
    assert set(branch["properties"]["handoff_owner"]["enum"]) == {
        "defaultspack",
        "rumi_code_ide_pack",
    }


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) == 5
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)
    assert all(item["handoff_owner"] for item in examples)
    assert {
        "rumi_agent_workroom_pack",
        "defaultspack",
        "rumi_code_ide_pack",
        "rumi_observability_pack",
    } <= {item["handoff_owner"] for item in examples}

    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"
    assert {
        "runtime_resume_engine_claim",
        "memory_write_claim",
        "metrics_collection_claim",
        "schedule_creation_claim",
        "branch_mutation_claim",
    } <= set(negative["cases"])

    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    profile = read_yaml(next((PACK_DIR / "profiles").glob("*.profile.yaml")))["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"

    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md", "docs/overlap_policy.md"]
    )
    for expected in ["Required Secrets", "None", "defaultspack", "Handoff", "Does Not Provide"]:
        assert expected in docs
    for expected in [
        "rumi_agent_workroom_pack",
        "rumi_memory_knowledge_pack",
        "rumi_observability_pack",
        "rumi_workflow_scheduler_pack",
        "rumi_workspace_pack",
    ]:
        assert expected in docs


def test_pack_body_has_no_credentials_or_runtime_surfaces() -> None:
    assert {path.name for path in PACK_DIR.iterdir() if path.is_dir()} & FORBIDDEN_DIRS == set()
    assert [
        path
        for path in PACK_DIR.rglob("*")
        if path.is_file() and path.suffix in FORBIDDEN_EXTENSIONS
    ] == []
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
