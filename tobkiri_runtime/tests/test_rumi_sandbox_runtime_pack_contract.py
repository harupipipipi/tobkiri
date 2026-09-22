from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from core_runtime.setup_pack import SetupPackManager
from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_sandbox_runtime_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _asset_index_paths(ecosystem: dict) -> set[str]:
    index = ecosystem["metadata"]["asset_index"]
    result: set[str] = set()
    for value in index.values():
        result.update(value)
    return result


def _meaningful_pack_assets() -> set[str]:
    return {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name not in {"ecosystem.json", "executables.v4.json"}
    }


def test_pack_required_assets_metadata_and_schema_validity() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/runtime_matrix.yaml",
        "policies/sandbox_execution.policy.yaml",
        "policies/secret_mount.policy.yaml",
        "specs/execution_boundary_matrix.yaml",
        "specs/runtime_receipt.schema.yaml",
        "checklists/reproducibility_checklist.yaml",
        "evidence/runtime_receipt_ledger.template.yaml",
        "profiles/sandbox_runtime_reviewer.profile.yaml",
        "prompts/sandbox_runtime_reviewer.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/container_test_run.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["vocabulary"]["types"]
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["registers_tools"] is False
    assert _asset_index_paths(ecosystem) == (
        _meaningful_pack_assets() - V4_AUTHORITY_ARTIFACTS
    )


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_validates_dependencies() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]

    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "high"
    assert candidate.depends_on == []
    assert candidate.overlap_policy["code_execution"] == "requires_defaultspack_tool_grants"
    assert candidate.base_pack_promotion["eligible"] is False
    assert "Sandbox Runtime" in candidate.base_pack_promotion["reason"]
    assert "secret_mounts_require_security_review" in candidate.base_pack_promotion["promotion_blockers"]
    assert "runtime_receipt_schema_cases" in candidate.base_pack_promotion["promotion_evidence_required"]
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "runtime-safety"
    assert candidate.signing["verified"] is True

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="macos",
        python_version="3.13.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_setup_pack_manager_installs_pack_without_selection_dependencies(tmp_path: Path) -> None:
    manager = SetupPackManager(
        root=ROOT / "ecosystem" / "setup_pack",
        selection_file=tmp_path / "setup_pack_selection.json",
        ecosystem_dir=ROOT / "ecosystem",
    )
    result = manager.install(PACK_ID)
    assert result["success"] is True
    assert result["installed_setup_pack_ids"] == [PACK_ID]
    assert result["installed_target_pack_ids"] == [PACK_ID]
    assert result["active_setup_pack_id"] == PACK_ID
    assert result["active_target_pack_id"] == PACK_ID
    assert result["skipped_all_ok_setup_pack_ids"] == [PACK_ID]


def test_sandbox_runtime_assets_have_real_semantics() -> None:
    boundary = yaml.safe_load((PACK_DIR / "specs/execution_boundary_matrix.yaml").read_text(encoding="utf-8"))
    execution_policy = yaml.safe_load((PACK_DIR / "policies/sandbox_execution.policy.yaml").read_text(encoding="utf-8"))
    secret_mount = yaml.safe_load((PACK_DIR / "policies/secret_mount.policy.yaml").read_text(encoding="utf-8"))
    receipt = yaml.safe_load((PACK_DIR / "specs/runtime_receipt.schema.yaml").read_text(encoding="utf-8"))
    repro = yaml.safe_load((PACK_DIR / "checklists/reproducibility_checklist.yaml").read_text(encoding="utf-8"))
    ledger = yaml.safe_load((PACK_DIR / "evidence/runtime_receipt_ledger.template.yaml").read_text(encoding="utf-8"))

    assert {"local_read_only", "local_host_mutating", "container_ephemeral", "remote_ssh"} <= set(boundary["boundaries"])
    assert boundary["boundaries"]["local_host_mutating"]["approval"] == "explicit_user_confirmation_required"
    assert boundary["boundaries"]["remote_ssh"]["handoff"] == "rumi_devops_release_pack"
    assert any(
        rule["id"] == "destructive_lifecycle_requires_confirmation" and rule["decision"] == "require"
        for rule in execution_policy["rules"]
    )
    assert secret_mount["default_decision"] == "deny_secret_mount"
    assert secret_mount["secret_mount_classes"]["raw_secret_value"]["allowed"] is False
    assert receipt["redaction_policy"]["redact_secret_values"] is True
    assert "boundary_classification_matches_matrix" in receipt["receipt_quality_gates"]
    assert "environment_variables_redacted" in repro["required_items"]
    assert "reject_secret_or_boundary_violation" in ledger["reviewer_decisions"]


def test_pack_docs_no_placeholders_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md"]
    )
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence", "runtime receipt"]:
        assert expected in docs

    all_text = "\n".join(p.read_text(encoding="utf-8") for p in PACK_DIR.rglob("*") if p.is_file())
    for forbidden in ["Example workflow", "sample user request", "reviewer_ready_plan", "placeholder"]:
        assert forbidden not in all_text

    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
