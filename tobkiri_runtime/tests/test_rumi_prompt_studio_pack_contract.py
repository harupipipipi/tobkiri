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
PACK_ID = "rumi_prompt_studio_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_metadata() -> None:
    required = [
        "ecosystem.json",
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "catalog/prompt_studio_prompt_library.yaml",
        "catalog/prompt_studio_workflows.yaml",
        "catalog/prompt_studio_quality_matrix.yaml",
        "catalog/custom_instruction_migration_map.yaml",
        "catalog/provider_migration_matrix.yaml",
        "schemas/prompt_studio_record.schema.json",
        "policies/prompt_studio_safety.policy.yaml",
        "policies/provider_style_boundary.policy.yaml",
        "checklists/prompt_studio_review.checklist.yaml",
        "ledgers/prompt_studio_evidence_ledger.schema.yaml",
        "ledgers/prompt_version_ledger.yaml",
        "templates/prompt_studio_handoff.template.md",
        "templates/custom_instruction_migration.template.md",
        "profiles/prompt_editor.profile.yaml",
        "prompts/prompt_linter.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/claude_style_instruction_migration.example.yaml",
        "examples/chatgpt_custom_instruction_lint.example.yaml",
        "examples/gemini_gem_prompt_normalization.example.yaml",
        "fixtures/prompt_regression_cases.yaml",
        "rumi.pack.v3.json",
        "artifact-manifest.json",
        "runtime/process.py",
        "runtime/service.py",
        "runtime/store.py",
        "frontend/contributions/prompt-studio.json",
        "ui/index.html",
        "ui/app.js",
        "ui/style.css",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    manifest = read_json(PACK_DIR / "pack.v4.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert set(ecosystem["connectivity"]["requires"]) == {
        "rumi.event.audit.recorded.v1",
        "rumi.resource.profile.workspace.v1",
    }
    assert manifest["requirements"]["pack_dependencies"] == {}
    contract_dependencies = manifest["requirements"]["contract_dependencies"]
    assert {
        item["contract_id"] for item in contract_dependencies
    } == {
        "tobkiri.event.audit.recorded.v1",
        "tobkiri.resource.profile.workspace.v1",
    }
    assert all(item["optional"] is True for item in contract_dependencies)
    assert "rumi.resource.prompt.studio.v1" in ecosystem["connectivity"]["provides"]
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is True
    assert ecosystem["host_execution"] is True
    assert ecosystem["metadata"]["integrity"]["artifact_manifest"] == "artifact-manifest.json"
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "prompt_artifact_catalog",
        "prompt_lint_rubric",
        "custom_instruction_migration",
        "fixture_dry_run_contract",
        "prompt_version_ledger",
    }
    indexed = {
        item
        for values in ecosystem["metadata"]["asset_index"].values()
        for item in values
    }
    runtime_assets = {
        "rumi.pack.v3.json",
        "artifact-manifest.json",
        "runtime/process.py",
        "runtime/service.py",
        "runtime/store.py",
        "frontend/contributions/prompt-studio.json",
        "ui/index.html",
        "ui/app.js",
        "ui/style.css",
    }
    assert set(required) - {"ecosystem.json"} - runtime_assets <= indexed


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
    assert candidate.depends_on == []
    issues = selector.validate_candidates(
        installed_packs={},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []

    assert candidate.overlap_policy["model_benchmarking"] == "handoff_to_rumi_model_evals_pack"
    assert candidate.overlap_policy["model_routing"] == "handoff_to_defaultspack_or_model_catalog_owner"
    assert candidate.overlap_policy["long_term_memory_storage"] == "handoff_to_rumi_memory_knowledge_pack"
    assert candidate.overlap_policy["tool_or_api_creation"] == "handoff_to_rumi_api_toolsmith_pack"
    assert candidate.overlap_policy["code_edits"] == "handoff_to_rumi_code_ide_pack"
    assert candidate.overlap_policy["prompt_artifact_catalog"] == "owned_by_rumi_prompt_studio_pack"
    assert candidate.overlap_policy["custom_instruction_migration"] == "owned_by_rumi_prompt_studio_pack"
    assert candidate.overlap_policy["prompt_lint_rubric"] == "owned_by_rumi_prompt_studio_pack"
    assert candidate.overlap_policy["fixture_dry_run_contract"] == "owned_by_rumi_prompt_studio_pack"
    assert candidate.overlap_policy["prompt_version_ledger"] == "owned_by_rumi_prompt_studio_pack"
    assert candidate.base_pack_promotion["eligible"] is False
    assert set(candidate.base_pack_promotion["promotion_blockers"]) >= {
        "prompt_preferences_are_user_specific",
        "no_model_router_owner",
        "no_memory_store_owner",
        "no_tool_api_or_code_owner",
    }
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "prompt-customization"
    assert candidate.signing["verified"] is True


def test_pack_semantic_contract_assets() -> None:
    record_schema = read_json(PACK_DIR / "schemas/prompt_studio_record.schema.json")
    prompt_library = yaml.safe_load(
        (PACK_DIR / "catalog/prompt_studio_prompt_library.yaml").read_text(encoding="utf-8")
    )["prompt_library"]
    migration_map = yaml.safe_load(
        (PACK_DIR / "catalog/custom_instruction_migration_map.yaml").read_text(encoding="utf-8")
    )["custom_instruction_migration_map"]
    provider_migration = yaml.safe_load(
        (PACK_DIR / "catalog/provider_migration_matrix.yaml").read_text(encoding="utf-8")
    )["provider_migration_matrix"]
    workflows = yaml.safe_load(
        (PACK_DIR / "catalog/prompt_studio_workflows.yaml").read_text(encoding="utf-8")
    )["workflows"]
    matrix = yaml.safe_load(
        (PACK_DIR / "catalog/prompt_studio_quality_matrix.yaml").read_text(encoding="utf-8")
    )["quality_matrix"]
    policy = yaml.safe_load(
        (PACK_DIR / "policies/prompt_studio_safety.policy.yaml").read_text(encoding="utf-8")
    )["policy"]
    provider_boundary = yaml.safe_load(
        (PACK_DIR / "policies/provider_style_boundary.policy.yaml").read_text(encoding="utf-8")
    )["provider_style_boundary_policy"]
    checklist = yaml.safe_load(
        (PACK_DIR / "checklists/prompt_studio_review.checklist.yaml").read_text(encoding="utf-8")
    )["review_checklist"]
    ledger = yaml.safe_load(
        (PACK_DIR / "ledgers/prompt_studio_evidence_ledger.schema.yaml").read_text(encoding="utf-8")
    )["evidence_ledger_schema"]
    version_ledger = yaml.safe_load(
        (PACK_DIR / "ledgers/prompt_version_ledger.yaml").read_text(encoding="utf-8")
    )["prompt_version_ledger"]
    template = (PACK_DIR / "templates/prompt_studio_handoff.template.md").read_text(encoding="utf-8")
    migration_template = (PACK_DIR / "templates/custom_instruction_migration.template.md").read_text(
        encoding="utf-8"
    )

    assert set(record_schema["required"]) >= {
        "prompt_id",
        "version",
        "purpose",
        "customization_knobs",
        "evidence_requirement",
        "risk_notes",
        "fixture_ids",
        "lint_dimensions",
        "boundary_handoffs",
        "review_status",
    }
    assert record_schema["properties"]["fixture_ids"]["type"] == "array"
    assert record_schema["properties"]["customization_knobs"]["type"] == "array"

    prompt_records = prompt_library["records"]
    assert len(prompt_records) >= 3
    assert {record["prompt_id"] for record in prompt_records} >= {
        "prompt_evidence_first",
        "prompt_handoff_boundary",
        "prompt_custom_instruction_migration",
    }
    assert all(record["version"] == "0.1.0" for record in prompt_records)
    assert all(record["fixture_ids"] for record in prompt_records)
    non_overlap_keys = {key for record in prompt_records for key in record["non_overlap"]}
    assert {
        "model_benchmarking",
        "long_term_memory_storage",
        "tool_or_api_creation",
        "code_edits",
    } <= non_overlap_keys

    assert {source["platform"] for source in migration_map["sources"]} == {"claude", "chatgpt", "gemini"}
    assert all(source["blocked_imports"] for source in migration_map["sources"])
    assert {item["id"] for item in provider_migration["sources"]} >= {
        "claude_project_instructions",
        "chatgpt_custom_instructions",
        "gemini_gem_prompt",
    }
    assert {
        "deterministic_prompt_id",
        "migration_notes",
        "fixture_ids",
        "blocked_imports",
        "handoff_owner",
        "review_state",
    } <= set(provider_migration["required_outputs"])
    assert all(source["blocked_imports"] and source["handoff_triggers"] for source in provider_migration["sources"])
    assert {item["id"] for item in workflows["items"]} == {
        "prompt_lint_review",
        "custom_instruction_migration",
        "fixture_dry_run_release",
    }

    check_ids = {item["id"] for item in matrix["checks"]}
    assert check_ids >= {
        "deterministic_prompt_id",
        "risk_notes",
        "evidence_requirement",
        "instruction_hierarchy",
        "scope_boundary",
        "custom_instruction_migration_fidelity",
        "style_persona_consistency",
        "privacy_secret_boundary",
        "owner_handoff",
    }
    assert {item["dimension"] for item in matrix["checks"]} >= {
        "identity",
        "hierarchy",
        "non_overlap",
        "safety",
        "reviewability",
        "migration",
        "style",
        "privacy",
    }

    assert set(policy["blocked_by_default"]) >= {
        "request silent credential discovery",
        "hide missing evidence",
        "claim model benchmark result",
        "route model automatically",
        "write persistent memory",
        "define or invoke tool API",
        "perform code edit",
    }
    assert policy["owner_handoff_required"]["model_benchmarking"] == "rumi_model_evals_pack"
    assert policy["owner_handoff_required"]["long_term_memory_storage"] == "rumi_memory_knowledge_pack"
    assert set(provider_boundary["blocked_by_default"]) >= {
        "claim provider runtime compatibility",
        "copy hidden system instructions",
        "import persistent user memory",
        "claim model benchmark superiority",
    }
    assert provider_boundary["owner_handoff_required"]["tool_or_api_creation"] == "rumi_api_toolsmith_pack"
    assert {item["id"] for item in checklist["required_checks"]} >= {
        "version_ledger_entry",
        "lint_dimension_coverage",
        "custom_instruction_source_review",
        "non_overlap_handoffs",
    }
    assert all(item["blocking"] for item in checklist["required_checks"])

    assert ledger["completion_rules"]["every_record_has_evidence"] is True
    assert ledger["completion_rules"]["version_ledger_entry_required"] is True
    assert ledger["completion_rules"]["no_model_eval_memory_api_or_code_ownership"] is True
    assert {
        "prompt_id",
        "prompt_version",
        "lint_dimensions_checked",
        "fixture_ids",
        "blocked_actions",
    } <= set(ledger["required_records"])
    assert len(version_ledger["entries"]) >= 3
    assert {entry["prompt_id"] for entry in version_ledger["entries"]} == {
        record["prompt_id"] for record in prompt_records
    }
    assert all(entry["compatibility_notes"] for entry in version_ledger["entries"])
    assert "Evidence" in template and "Handoff" in template
    assert "Customization Knobs" in migration_template
    assert "Blocked Imports" in migration_template
    assert "Handoff" in migration_template

    fixtures = yaml.safe_load(
        (PACK_DIR / "fixtures/prompt_regression_cases.yaml").read_text(encoding="utf-8")
    )["prompt_regression_cases"]
    assert fixtures["network_default"] == "none"
    assert len(fixtures["cases"]) >= 5
    assert all(case["prompt_id"].startswith("prompt_") for case in fixtures["cases"])
    assert all(case["expected_lint_dimensions"] for case in fixtures["cases"])
    assert all(case["must_not_emit"] for case in fixtures["cases"])
    assert {"risk", "evidence_requirement", "customization_knobs"} <= set(fixtures["cases"][0]["must_emit"])
    assert {case["source_kind"] for case in fixtures["cases"]} >= {
        "claude_project_instructions",
        "chatgpt_custom_instructions",
        "gemini_gem_instruction",
    }


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
        "version ledger",
        "model benchmarking",
        "persistent memory storage",
        "tool/API creation",
        "code edits",
    ]:
        assert expected in docs
    pattern = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}")
    checked = [
        p
        for p in PACK_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
    combined = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    for phrase in ["sample user request", "reviewer_ready_plan", "Complementary owner surface"]:
        assert phrase not in combined
