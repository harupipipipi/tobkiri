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
PACK_ID = "rumi_api_toolsmith_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_metadata() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/api_toolsmith_workflows.yaml",
        "specs/openapi_schema_review.yaml",
        "specs/graphql_operation_review.yaml",
        "specs/webhook_contract_review.yaml",
        "evidence/mock_test_evidence.schema.yaml",
        "policies/api_tool_safety.policy.yaml",
        "profiles/api_toolsmith.profile.yaml",
        "prompts/api_toolsmith.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/openapi_to_tool.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert "vocabulary" in ecosystem
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["registers_tools"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "graphql_operation_mapping",
        "openapi_tool_generation",
        "mock_test_evidence",
        "webhook_signature_policy_review",
    }
    asset_index = ecosystem["metadata"]["asset_index"]
    assert "catalog/api_toolsmith_workflows.yaml" in asset_index["catalogs"]
    assert set(asset_index["specs"]) == {
        "specs/openapi_schema_review.yaml",
        "specs/graphql_operation_review.yaml",
        "specs/webhook_contract_review.yaml",
    }
    assert "policies/api_tool_safety.policy.yaml" in asset_index["policies"]
    assert "evidence/mock_test_evidence.schema.yaml" in asset_index["evidence_ledgers"]
    indexed_assets = {asset for assets in asset_index.values() for asset in assets}
    assert {'README.md', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'profiles/api_toolsmith.profile.yaml', 'prompts/api_toolsmith.system.md'} <= indexed_assets
    assert all((PACK_DIR / asset).is_file() for asset in indexed_assets)
    assert set(asset_index["examples"]) == {
        "examples/openapi_to_tool.example.yaml",
        "examples/graphql_action_map.example.yaml",
        "examples/webhook_contract_test.example.yaml",
    }


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["mcp_server_registration"] == "handoff_to_rumi_mcp_gateway_pack"
    assert candidate.base_pack_promotion["eligible"] is False
    assert "API Toolsmith" in candidate.base_pack_promotion["reason"]
    assert "no_executable_runtime_tools" in candidate.base_pack_promotion["promotion_blockers"]
    assert "webhook_signature_policy_secret_free_review" in candidate.base_pack_promotion["promotion_evidence_required"]
    assert candidate.marketplace["id"].startswith("rumi.")
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "api-tooling"
    assert candidate.signing["verified"] is True


def test_api_toolsmith_review_assets_are_substantial() -> None:
    openapi = yaml.safe_load((PACK_DIR / "specs/openapi_schema_review.yaml").read_text(encoding="utf-8"))
    graphql = yaml.safe_load((PACK_DIR / "specs/graphql_operation_review.yaml").read_text(encoding="utf-8"))
    webhook = yaml.safe_load((PACK_DIR / "specs/webhook_contract_review.yaml").read_text(encoding="utf-8"))
    evidence = yaml.safe_load((PACK_DIR / "evidence/mock_test_evidence.schema.yaml").read_text(encoding="utf-8"))
    workflows = yaml.safe_load((PACK_DIR / "catalog/api_toolsmith_workflows.yaml").read_text(encoding="utf-8"))

    assert "operation_selection" in openapi["review_dimensions"]
    assert "request_body" in openapi["review_dimensions"]
    assert "destructive_methods_require_handoff" in openapi["review_dimensions"]["operation_selection"]["checks"]
    mutation_checks = graphql["review_dimensions"]["operation_kind"]["mutation"]["required_checks"]
    assert "explicit_user_confirmation" in mutation_checks
    assert "idempotency_policy" in mutation_checks
    assert "partial_error_policy_recorded" in graphql["review_dimensions"]["response_mapping"]["checks"]
    assert "signature_header_names_recorded_without_secrets" in webhook["review_dimensions"]["security_boundary"]["checks"]
    security_checks = "\n".join(webhook["review_dimensions"]["security_boundary"]["checks"])
    assert "signature_header_names_recorded_without_secrets" in security_checks
    assert "duplicate_delivery_case_included" in webhook["review_dimensions"]["mock_delivery"]["checks"]
    assert {"happy_path_mock", "auth_boundary_no_secret", "malformed_payload", "partial_error", "rate_limit_or_retry"} <= set(evidence["test_case_types"])
    assert "map_graphql_operation_to_tool" in workflows["workflows"]
    assert "review_webhook_contract" in workflows["workflows"]


def test_api_toolsmith_pack_does_not_register_tools_or_placeholder_examples() -> None:
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["components"] == {}
    assert "functions" not in ecosystem
    assert not (PACK_DIR / "tools").exists()
    assert ecosystem["metadata"]["registers_tools"] is False

    checked_text = "\n".join(path.read_text(encoding="utf-8") for path in (PACK_DIR / "examples").glob("*.yaml"))
    assert "sample user request" not in checked_text
    assert "reviewer_ready_plan" not in checked_text


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence"]:
        assert expected in docs
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
