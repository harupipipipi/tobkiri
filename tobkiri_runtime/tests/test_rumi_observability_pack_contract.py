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
PACK_ID = "rumi_observability_pack"
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
    assert candidate.overlap_policy["model_metrics"] == "handoff_to_rumi_model_evals_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert candidate.defaultspack_promotion["reason"].startswith("Observability belongs")
    assert "requires_network_or_secret_access" in candidate.defaultspack_promotion["promotion_blockers"]
    assert "run_ledger_contract_review" in candidate.defaultspack_promotion["promotion_evidence_required"]
    assert candidate.marketplace["registry"] == "rumi_local_pack_registry"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "observability"
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


def test_pack_observability_assets_have_semantic_contracts() -> None:
    event_schema = read_json(PACK_DIR / "schemas/observability_event.schema.json")
    required = set(event_schema["required"])
    assert {"event_id", "event_class", "privacy_class", "redaction_state", "evidence_refs"} <= required
    event_classes = set(event_schema["properties"]["event_class"]["enum"])
    assert {"agent_run", "tool_call", "model_call", "cost_latency", "incident", "handoff"} <= event_classes

    event_catalog = read_yaml(PACK_DIR / "catalog/observability_events.yaml")
    catalog_classes = {item["class_id"] for item in event_catalog["event_classes"]}
    assert {"cost_latency", "handoff", "incident"} <= catalog_classes

    ledger = read_yaml(PACK_DIR / "catalog/run_ledger_contract.yaml")
    assert {"cost_summary", "latency_summary", "redaction_summary"} <= set(ledger["ledger_required_fields"])
    assert {"all_sensitive_fields_redacted", "cost_units_declared", "latency_units_declared"} <= set(ledger["ledger_quality_gates"])
    assert ledger["handoff_contract"]["release_incidents"] == "rumi_devops_release_pack"

    redaction = read_yaml(PACK_DIR / "policies/privacy_cost_redaction.policy.yaml")
    assert {"secret_value", "account_identifier", "private_prompt_text"} <= set(redaction["redaction_classes"])
    assert "pricing_source_required_for_estimated_usd" in redaction["cost_redaction_rules"]

    checklist = read_yaml(PACK_DIR / "checklists/incident_review_checklist.yaml")
    checklist_ids = {item["id"] for item in checklist["checklist"]}
    assert {"redaction_review", "cost_latency_review", "handoff"} <= checklist_ids
    assert "owner_pack_boundary_confirmed" in checklist["completion_gate"]["required_items"]

    template = (PACK_DIR / "templates/postmortem_template.md").read_text(encoding="utf-8")
    for expected in ["Redaction State", "Cost And Latency", "Owner Pack Handoff"]:
        assert expected in template


def test_pack_examples_and_presets_are_specific() -> None:
    agent_run = read_yaml(PACK_DIR / "examples/agent_run_summary.example.yaml")
    assert agent_run["expected_run_ledger"]["redaction_state"] == "redacted"
    assert agent_run["expected_run_ledger"]["cost_summary"]["unit"] == "estimated_usd"
    assert agent_run["expected_run_ledger"]["latency_summary"]["unit"] == "ms"
    assert agent_run["expected_run_ledger"]["handoff"]["target_pack"] == "rumi_model_evals_pack"

    snapshot = read_yaml(PACK_DIR / "examples/cost_latency_snapshot.example.yaml")
    assert snapshot["summary_contract"]["sample_count"] == 2
    assert snapshot["summary_contract"]["latency_unit"] == "ms"
    assert "raw_prompt_text" in snapshot["summary_contract"]["disallowed_fields"]

    postmortem = read_yaml(PACK_DIR / "examples/tool_call_postmortem.example.yaml")
    assert postmortem["expected_handoff"]["target_pack"] == "rumi_devops_release_pack"
    assert "redaction_state" in postmortem["required_postmortem_sections"]

    quality_gate = read_yaml(PACK_DIR / "presets/quality_gate.preset.yaml")
    gate_ids = {gate["gate_id"] for gate in quality_gate["quality_gates"]}
    assert {"schema_valid_event", "ledger_complete", "redaction_complete", "incident_ready"} <= gate_ids

    text = "\n".join(path.read_text(encoding="utf-8") for path in PACK_DIR.rglob("*") if path.is_file())
    assert not any(placeholder in text for placeholder in GENERIC_PLACEHOLDERS)


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence", "event schema", "run ledger"]:
        assert expected in docs
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if SECRET_PATTERN.search(p.read_text(encoding="utf-8"))] == []
