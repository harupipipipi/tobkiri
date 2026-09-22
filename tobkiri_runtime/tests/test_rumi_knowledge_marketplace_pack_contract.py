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
PACK_ID = "rumi_knowledge_marketplace_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"
GENERIC_PLACEHOLDERS = (
    "Example workflow",
    "sample user request",
    "reviewer_ready_plan",
    "TODO",
)
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def asset_index_paths(ecosystem: dict) -> set[str]:
    indexed: set[str] = set()
    for paths in ecosystem["metadata"]["asset_index"].values():
        assert isinstance(paths, list)
        indexed.update(paths)
    return indexed


def test_pack_manifest_schema_valid_and_asset_index_complete() -> None:
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["vocabulary"]["types"]
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["owner_surfaces"]
    shipped = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file() and path.name != "executables.v4.json"
    }
    shipped -= V4_AUTHORITY_ARTIFACTS
    assert asset_index_paths(ecosystem) == shipped


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(read_yaml(path), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_validated_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]
    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="macos",
        python_version="3.13.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["bundle_catalog"] == "handoff_to_rumi_pack_suite_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert candidate.defaultspack_promotion["reason"].startswith("Marketplace curation stays optional")
    assert "would_auto_install_unreviewed_content" in candidate.defaultspack_promotion["promotion_blockers"]
    assert "install_review_workflow_review" in candidate.defaultspack_promotion["promotion_evidence_required"]
    assert candidate.marketplace["registry"] == "rumi_local_pack_registry"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "knowledge-marketplace"
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


def test_pack_marketplace_assets_have_semantic_contracts() -> None:
    card_schema = read_json(PACK_DIR / "schemas/marketplace_card.schema.json")
    assert {"card_id", "capability_type", "source", "publisher", "trust_status", "permission_summary", "provenance", "install_review"} <= set(card_schema["required"])
    assert {"skill", "template", "playbook", "connector_card", "pack_bundle"} <= set(card_schema["properties"]["capability_type"]["enum"])
    assert {"unreviewed", "repository_reviewed", "trusted", "blocked"} <= set(card_schema["properties"]["trust_status"]["enum"])
    assert card_schema["network_default"] == "none"

    card_catalog = read_yaml(PACK_DIR / "catalog/marketplace_card_schema.yaml")
    assert {"permission_summary", "provenance", "install_review"} <= set(card_catalog["required_fields"])
    assert "explicit_user_approval_required" not in card_catalog["required_fields"]
    assert "needs_security_review" in card_catalog["install_review_decisions"]

    rubric = read_yaml(PACK_DIR / "catalog/trust_rubric.yaml")
    rubric_levels = {level["id"] for level in rubric["rubric_levels"]}
    assert {"unreviewed", "repository_reviewed", "trusted", "blocked"} <= rubric_levels
    assert "permission_minimality" in rubric["score_dimensions"]
    assert rubric["handoff_policy"]["connector_card_execution"] == "rumi_connector_gateway_pack"

    workflow = read_yaml(PACK_DIR / "workflows/install_review_workflow.yaml")
    assert workflow["completion_gate"]["auto_install_allowed"] is False
    assert workflow["completion_gate"]["explicit_user_approval_required"] is True
    assert workflow["completion_gate"]["provenance_ledger_entry_required"] is True
    assert {"identify_candidate", "permission_review", "provenance_review", "trust_decision"} <= {phase["id"] for phase in workflow["phases"]}

    ledger = read_yaml(PACK_DIR / "ledgers/provenance_ledger.yaml")
    assert {"content_digest", "permission_summary", "trust_status", "blacklist_status"} <= set(ledger["ledger_fields"])
    assert {"content_digest_recorded", "blocked_cards_have_reason"} <= set(ledger["quality_gates"])

    policy = read_yaml(PACK_DIR / "policies/promotion_blacklist.policy.yaml")
    assert "excessive_permissions_without_justification" in policy["promotion_blockers"]
    assert "secret_exfiltration_risk" in policy["blacklist_reasons"]
    assert policy["recheck_policy"]["security_review_handoff"] == "rumi_security_review_pack"


def test_pack_examples_and_presets_are_specific() -> None:
    skill = read_yaml(PACK_DIR / "examples/skill_review_card.example.yaml")
    assert skill["marketplace_card"]["trust_status"] == "repository_reviewed"
    assert skill["install_review"]["explicit_user_approval_required"] is True
    assert skill["expected_ledger_entry"]["handoff"]["target_pack"] == "rumi_memory_knowledge_pack"

    connector = read_yaml(PACK_DIR / "examples/connector_card_review.example.yaml")
    assert connector["marketplace_card"]["capability_type"] == "connector_card"
    assert connector["install_review"]["decision"] == "needs_security_review"
    assert connector["expected_handoff"]["target_pack"] == "rumi_connector_gateway_pack"

    template = read_yaml(PACK_DIR / "examples/template_catalog_entry.example.yaml")
    assert template["marketplace_card"]["trust_status"] == "trusted"
    assert template["promotion_review"]["eligible_for_defaultspack"] is False
    assert template["expected_handoff"]["target_pack"] == "rumi_pack_suite_pack"

    quality_gate = read_yaml(PACK_DIR / "presets/quality_gate.preset.yaml")
    gate_ids = {gate["gate_id"] for gate in quality_gate["quality_gates"]}
    assert {"card_schema_complete", "provenance_recorded", "permission_scope_reviewed", "promotion_or_blacklist_decided"} <= gate_ids

    text = "\n".join(path.read_text(encoding="utf-8") for path in PACK_DIR.rglob("*") if path.is_file())
    assert not any(placeholder in text for placeholder in GENERIC_PLACEHOLDERS)


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence", "provenance", "trust rubric"]:
        assert expected in docs
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if SECRET_PATTERN.search(p.read_text(encoding="utf-8"))] == []
