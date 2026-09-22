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
PACK_ID = "rumi_code_migration_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = [
    "README.md",
    "asset_index.json",
    "asset_index.yaml",
    "catalog/compatibility_dimensions.yaml",
    "catalog/handoff_matrix.yaml",
    "catalog/migration_patterns.yaml",
    "catalog/quality_matrix.yaml",
    "catalog/taxonomy.yaml",
    "catalog/workflows.yaml",
    "checklists/review.checklist.yaml",
    "docs/README.md",
    "docs/architecture.md",
    "docs/interfaces.md",
    "docs/operations.md",
    "examples/api_deprecation_migration.example.yaml",
    "examples/framework_upgrade.example.yaml",
    "examples/monorepo_sharded_migration.example.yaml",
    "fixtures/contract_fixture.yaml",
    "fixtures/negative_cases.yaml",
    "ledgers/migration_risk_ledger.schema.yaml",
    "metadata/overlap_promotion.yaml",
    "policies/handoff.policy.yaml",
    "policies/migration_governance.policy.yaml",
    "policies/safety.policy.yaml",
    "presets/handoff_review.preset.yaml",
    "presets/quality_gate.preset.yaml",
    "presets/safe_default.preset.yaml",
    "profiles/migration_planner.profile.yaml",
    "prompts/migration_planner.system.md",
    "schemas/codemod_plan.schema.yaml",
    "schemas/compatibility_matrix.schema.yaml",
    "schemas/migration_handoff_packet.schema.yaml",
    "schemas/migration_plan.schema.yaml",
    "schemas/pr_shard_plan.schema.yaml",
    "schemas/repo_inventory.schema.yaml",
    "schemas/risk_ledger.schema.yaml",
    "schemas/rollback_plan.schema.yaml",
    "schemas/test_gate.schema.yaml",
    "templates/handoff.template.md",
    "templates/migration_handoff_packet.template.yaml",
    "templates/pr_shard_brief.template.yaml",
    "templates/review_report.template.md",
    "templates/ui_contract.template.md",
]

SCHEMA_EXPECTATIONS = {
    "schemas/repo_inventory.schema.yaml": {
        "inventory_id",
        "languages",
        "package_managers",
        "build_systems",
        "test_suites",
        "dependency_graph",
        "ownership_map",
        "generated_files",
        "deprecated_apis",
        "migration_hotspots",
    },
    "schemas/migration_plan.schema.yaml": {
        "plan_id",
        "current_state",
        "target_state",
        "non_goals",
        "phases",
        "prerequisites",
        "invariants",
        "compatibility_constraints",
        "acceptance_gates",
    },
    "schemas/codemod_plan.schema.yaml": {
        "codemod_plan_id",
        "transform_intent",
        "matcher_strategy",
        "dry_run_requirements",
        "manual_review_buckets",
        "generated_file_exclusions",
        "idempotency_checks",
        "validation_commands",
    },
    "schemas/pr_shard_plan.schema.yaml": {
        "shard_id",
        "scope",
        "dependencies",
        "max_blast_radius",
        "ordering",
        "owner_handoff",
        "reviewer_notes",
        "merge_sequencing",
        "conflict_risk",
    },
    "schemas/compatibility_matrix.schema.yaml": {
        "matrix_id",
        "runtime_versions",
        "framework_versions",
        "os_platform",
        "package_manager_constraints",
        "api_behavior_deltas",
        "supported_cells",
        "unsupported_cells",
    },
    "schemas/risk_ledger.schema.yaml": {
        "risk_id",
        "impact",
        "likelihood",
        "detection_signal",
        "mitigation",
        "owner",
        "escalation_path",
        "rollback_linkage",
    },
    "schemas/test_gate.schema.yaml": {
        "gate_id",
        "static_checks",
        "unit_tests",
        "integration_tests",
        "e2e_tests",
        "migration_smoke_tests",
        "fixture_coverage",
        "flake_policy",
        "pass_fail_evidence",
    },
    "schemas/rollback_plan.schema.yaml": {
        "rollback_id",
        "trigger",
        "revert_strategy",
        "data_config_considerations",
        "dependency_downgrade_path",
        "user_impact",
        "verification_after_rollback",
    },
    "schemas/migration_handoff_packet.schema.yaml": {
        "packet_id",
        "inventory_summary",
        "chosen_migration_strategy",
        "shard_briefs",
        "risks",
        "test_gates",
        "rollback_plan",
        "downstream_owner_pack",
        "external_action",
    },
}

OWNER_EXPECTED = {
    "repo_inventory",
    "migration_plan",
    "codemod_plan",
    "pr_shard_plan",
    "compatibility_matrix",
    "risk_ledger",
    "test_gate_plan",
    "rollback_plan",
    "migration_handoff_packet",
}
NON_OWNER_EXPECTED = {
    "CLI IDE command loops",
    "file editing and patch execution",
    "subagent assignment and PR execution",
    "release notes and deploy runbooks",
    "security findings",
    "model provider scoring",
    "runtime telemetry storage",
}
OVERLAP_EXPECTED = {
    "cli_ide_execution": "handoff_to_rumi_code_ide_pack",
    "file_editing_patch_execution": "handoff_to_rumi_code_ide_pack",
    "subagent_pr_execution": "handoff_to_rumi_subagent_pr_manager_pack",
    "release_deploy_runbooks": "handoff_to_rumi_devops_release_pack",
    "security_findings": "handoff_to_rumi_security_review_pack",
    "model_scoring": "handoff_to_rumi_model_evals_pack",
    "runtime_telemetry": "handoff_to_rumi_observability_pack",
    "migration_planning_contract": "owned_by_rumi_code_migration_pack",
    "tool_aliases": "prefer_explicit_pack_namespace",
}
PROMOTION_BLOCKERS = {
    "no_executable_runtime",
    "planning_only_pack",
    "requires_external_owner_for_code_edits",
    "requires_external_owner_for_pr_execution",
    "requires_maintainer_approved_migration_rollout",
}
PROMOTION_EVIDENCE = {
    "successful_large_repo_migration_plans",
    "observed_shard_handoff_quality",
    "rollback_plan_validation_evidence",
    "compatibility_matrix_maintainer_review",
    "zero_overlap_regressions_with_code_ide_pr_manager_release_packs",
}
BLOCKED_BY_DEFAULT = {
    "execute codemods",
    "edit files directly",
    "run terminal command loops",
    "create pull requests",
    "merge pull requests",
    "publish release notes",
    "deploy changes",
    "record telemetry",
    "claim migration success without checks",
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
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return loaded


def read_structured(path: Path) -> dict:
    return read_json(path) if path.suffix == ".json" else read_yaml(path)


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
    assert ecosystem["runtime"]["type"] == "declarative_pack"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []

    metadata = ecosystem["metadata"]
    assert metadata["network_policy"] == "none_by_default"
    assert metadata["executable_code"] is False
    assert metadata["declarative_only"] is True
    assert metadata["defaultspack_promotion_eligible"] is False
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    assert {item["pack_id"] for item in metadata["optional_integrations"]} >= {
        "rumi_code_ide_pack",
        "rumi_subagent_pr_manager_pack",
        "rumi_devops_release_pack",
        "rumi_security_review_pack",
        "rumi_model_evals_pack",
        "rumi_observability_pack",
    }

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
    assert asset_index["invariants"]["defaultspack_promotion_eligible"] is False


def test_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(read_yaml(path), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(read_json(path), dict), path


def test_setup_pack_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]

    assert setup["pack_id"] == PACK_ID
    assert setup["target_pack_id"] == PACK_ID
    assert setup["recommended"] is False
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
    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["id"] == "rumi.code_migration_pack"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "governance"
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
        schema = read_structured(PACK_DIR / rel_path)
        assert schema["additionalProperties"] is False
        assert required <= set(schema["required"])
        assert required <= set(schema["properties"])

    handoff_schema = read_yaml(PACK_DIR / "schemas/migration_handoff_packet.schema.yaml")
    assert handoff_schema["properties"]["external_action"]["const"] == "handoff_only"
    assert set(handoff_schema["properties"]["downstream_owner_pack"]["enum"]) >= {
        "rumi_code_ide_pack",
        "rumi_subagent_pr_manager_pack",
        "rumi_devops_release_pack",
        "rumi_security_review_pack",
        "rumi_model_evals_pack",
        "rumi_observability_pack",
    }

    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    governance = read_yaml(PACK_DIR / "policies/migration_governance.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    ledger = read_yaml(PACK_DIR / "ledgers/migration_risk_ledger.schema.yaml")["evidence_ledger_schema"]

    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert {item["id"] for item in workflows["items"]} == {
        "large_repo_inventory",
        "codemod_planning",
        "pr_shard_planning",
        "compatibility_gate",
        "risk_rollback_review",
        "migration_handoff",
    }
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert all(item["produces"].endswith("_handoff_packet") for item in workflows["items"])

    assert {item["id"] for item in quality["checks"]} >= {
        "inventory_complete",
        "codemod_dry_run_required",
        "generated_files_excluded",
        "pr_shards_blast_radius_limited",
        "compatibility_matrix_reviewed",
        "rollback_plan_linked",
        "owner_handoff_named",
    }
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["external_effect"] == "handoff_packet_only"
    assert governance["planning_only"] is True
    assert governance["requires_dry_run_before_execution_handoff"] is True

    for key, expected in OVERLAP_EXPECTED.items():
        assert handoff_policy["overlap_policy"][key] == expected
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"


def test_examples_are_realistic_migration_handoff_packets() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert {item["id"] for item in examples} == {
        "api_deprecation_migration",
        "framework_upgrade",
        "monorepo_sharded_migration",
    }

    for example in examples:
        assert example["expected_result"].endswith("handoff_packet")
        assert "external_action" in example["must_not"]
        assert example["handoff_owner"] in {
            "rumi_code_ide_pack",
            "rumi_subagent_pr_manager_pack",
            "rumi_devops_release_pack",
            "rumi_security_review_pack",
            "rumi_model_evals_pack",
            "rumi_observability_pack",
        }
        assert set(example["handoff_packet"]) >= {
            "repo_inventory",
            "migration_plan",
            "codemod_plan",
            "pr_shard_plan",
            "compatibility_matrix",
            "risk_ledger",
            "test_gates",
            "rollback_plan",
            "downstream_owner_pack",
            "external_action",
        }
        assert example["handoff_packet"]["external_action"] == "handoff_only"
        assert len(example["handoff_packet"]["repo_inventory"]["migration_hotspots"]) >= 2
        assert len(example["handoff_packet"]["pr_shard_plan"]["shards"]) >= 2
        assert len(example["handoff_packet"]["risk_ledger"]) >= 2
        assert len(example["handoff_packet"]["test_gates"]) >= 3
        assert "execute" not in " ".join(example["handoff_packet"]["codemod_plan"]["validation_commands"]).lower()

    assert any(item["handoff_owner"] == "rumi_subagent_pr_manager_pack" for item in examples)
    assert any("React" in item["scenario"] or "Next.js" in item["scenario"] for item in examples)
    assert any("deprecated API" in item["scenario"] for item in examples)


def test_presets_profile_templates_docs_and_negative_cases_keep_boundaries() -> None:
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"
    assert set(negative["blocked_requests"]) >= BLOCKED_BY_DEFAULT

    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    assert all(item["external_action"] == "handoff_only" for item in presets)

    profile = read_yaml(next((PACK_DIR / "profiles").glob("*.profile.yaml")))["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"

    packet_template = read_yaml(PACK_DIR / "templates/migration_handoff_packet.template.yaml")["template"]
    shard_template = read_yaml(PACK_DIR / "templates/pr_shard_brief.template.yaml")["template"]
    assert packet_template["external_action"] == "handoff_only"
    assert set(packet_template) >= {"packet_id", "downstream_owner_pack", "external_action"}
    assert shard_template["owner_handoff"] == "rumi_subagent_pr_manager_pack"

    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md"]
    )
    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "Handoff",
        "Does Not Provide",
        "No executable code",
        "network is none by default",
    ]:
        assert expected in docs


def test_pack_body_has_no_credentials_or_runtime_surfaces() -> None:
    assert {path.name for path in PACK_DIR.iterdir() if path.is_dir()} & FORBIDDEN_DIRS == set()
    assert [path for path in PACK_DIR.rglob("*") if path.is_file() and path.suffix in FORBIDDEN_EXTENSIONS] == []

    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()] + [SETUP_PACK_JSON]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    forbidden_phrases = [
        "BEGIN " + "PRIVATE KEY",
        "password=",
        "sample user request",
        "reviewer_ready_plan",
        "TODO",
    ]
    for phrase in forbidden_phrases:
        assert phrase not in combined

    secret_patterns = [
        r"s" + r"k-[A-Za-z0-9_-]{20,}",
        r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{12,}['\"]",
        r"ghp_[A-Za-z0-9]{20,}",
        r"xox[baprs]-[A-Za-z0-9-]{20,}",
        r"AIza[0-9A-Za-z_-]{20,}",
        r"ya29\.[0-9A-Za-z_-]+",
    ]
    for pattern in secret_patterns:
        assert re.search(pattern, combined) is None
