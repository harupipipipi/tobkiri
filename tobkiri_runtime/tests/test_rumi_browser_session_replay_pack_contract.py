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
PACK_ID = "rumi_browser_session_replay_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = [
    "README.md",
    "asset_index.json",
    "asset_index.yaml",
    "catalog/evidence_bundle_contract.yaml",
    "catalog/handoff_matrix.yaml",
    "catalog/quality_matrix.yaml",
    "catalog/replay_event_taxonomy.yaml",
    "catalog/taxonomy.yaml",
    "catalog/workflows.yaml",
    "checklists/review.checklist.yaml",
    "docs/README.md",
    "docs/architecture.md",
    "docs/interfaces.md",
    "docs/operations.md",
    "docs/overlap_policy.md",
    "examples/checkout_selector_drift.example.yaml",
    "examples/login_failure_trace.example.yaml",
    "examples/private_dashboard_replay_review.example.yaml",
    "examples/redacted_bug_replay_manifest.example.yaml",
    "fixtures/contract_fixture.yaml",
    "fixtures/negative_cases.yaml",
    "ledgers/replay_evidence_ledger.schema.yaml",
    "policies/handoff.policy.yaml",
    "policies/replay_safety_boundary.policy.yaml",
    "policies/safety.policy.yaml",
    "policies/session_replay_redaction.policy.yaml",
    "presets/handoff_review.preset.yaml",
    "presets/quality_gate.preset.yaml",
    "presets/safe_default.preset.yaml",
    "profiles/browser_session_replay.profile.yaml",
    "prompts/browser_session_replay.system.md",
    "schemas/browser_event_evidence.schema.json",
    "schemas/browser_session_trace.schema.json",
    "schemas/dom_snapshot_evidence.schema.json",
    "schemas/redaction_review_receipt.schema.json",
    "schemas/replay_manifest.schema.json",
    "schemas/screenshot_evidence.schema.json",
    "schemas/selector_drift_report.schema.json",
    "templates/handoff.template.md",
    "templates/review_report.template.md",
    "templates/ui_contract.template.md",
]

SCHEMA_EXPECTATIONS = {
    "schemas/browser_session_trace.schema.json": [
        "trace_id",
        "source_session_ref",
        "captured_at",
        "browser_context",
        "page_timeline",
        "evidence_bundle_refs",
        "redaction_state",
        "handoff_targets",
    ],
    "schemas/dom_snapshot_evidence.schema.json": [
        "snapshot_id",
        "trace_id",
        "semantic_dom_ref",
        "semantic_owner",
        "captured_at",
        "redaction_state",
        "text_included_policy",
        "node_refs",
        "checksum",
    ],
    "schemas/screenshot_evidence.schema.json": [
        "screenshot_id",
        "trace_id",
        "viewport",
        "dimensions",
        "captured_at",
        "artifact_ref",
        "checksum",
        "redaction_overlays",
        "privacy_class",
        "binary_inline_allowed",
    ],
    "schemas/browser_event_evidence.schema.json": [
        "event_id",
        "trace_id",
        "event_type",
        "observed_at",
        "page_ref",
        "evidence_refs",
        "execution_effect",
        "event_payload_redacted",
        "owner_pack_id",
    ],
    "schemas/replay_manifest.schema.json": [
        "manifest_id",
        "trace_id",
        "ordered_evidence_refs",
        "preconditions",
        "expected_page_states",
        "redaction_policy_ref",
        "selector_drift_report_refs",
        "handoff_instructions",
        "execution_owner",
        "transport_owner",
        "external_action",
    ],
    "schemas/selector_drift_report.schema.json": [
        "drift_id",
        "original_selector",
        "semantic_element_ref",
        "observed_selector_candidates",
        "confidence",
        "drift_reason",
        "screenshot_evidence_refs",
        "dom_evidence_refs",
        "recommended_handoff",
        "semantic_owner",
    ],
    "schemas/redaction_review_receipt.schema.json": [
        "receipt_id",
        "trace_id",
        "redaction_state",
        "reviewer_ref",
        "sensitive_classes",
        "share_allowed",
        "blocked_fields",
        "evidence_refs",
    ],
}

OWNER_EXPECTED = {
    "browser_event_evidence",
    "browser_session_trace_contract",
    "dom_snapshot_evidence_bundle",
    "redaction_review_receipt",
    "replay_manifest_contract",
    "screenshot_evidence_bundle",
    "selector_drift_report",
}
NON_OWNER_EXPECTED = {
    "browser companion transport",
    "browser execution",
    "connector retrieval",
    "defaultspack audit and grants",
    "form submission",
    "observability metric storage",
    "semantic DOM interpretation",
}
OVERLAP_EXPECTED = {
    "browser_execution": "handoff_to_rumi_browser_automation_pack",
    "semantic_dom": "handoff_to_rumi_browser_element_pack",
    "browser_transport": "handoff_to_rumi_default_tools_pack",
    "audit_logs": "handoff_to_defaultspack",
    "form_submission": "handoff_to_rumi_browser_form_operator_pack",
    "session_replay_contract": "owned_by_rumi_browser_session_replay_pack",
    "redaction_review": "owned_by_rumi_browser_session_replay_pack",
    "tool_aliases": "prefer_explicit_pack_namespace",
}
PROMOTION_BLOCKERS = {
    "browser_execution_owned_by_rumi_browser_automation_pack",
    "contains_browser_session_trace_contracts_not_core_defaults",
    "no_runtime_enforcement_or_browser_driver",
    "replay_manifest_contract_requires_multi_pack_handoff",
    "requires_redaction_review_for_sensitive_browser_evidence",
    "semantic_dom_owned_by_rumi_browser_element_pack",
    "transport_owned_by_rumi_default_tools_pack",
}
PROMOTION_EVIDENCE = {
    "evidence_bundle_hash_and_ref_cases",
    "handoff_boundary_reviewed_with_browser_automation_and_element_packs",
    "redaction_policy_cases_cover_auth_payment_private_urls",
    "schema_contract_tests_pass",
    "selector_drift_examples_reviewed",
}
BLOCKED_BY_DEFAULT = {
    "call browser companion tools",
    "click browser elements",
    "open pages directly",
    "own browser transport",
    "share private URLs without redaction",
    "store raw screenshots inline",
    "submit forms",
    "trust unredacted DOM text",
    "type into pages",
}
FORBIDDEN_DIRS = {"api", "backend", "blocks", "domain", "functions", "routes", "scripts", "static", "stores", "tools", "transport", "ui", "webapp"}
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
    assert metadata["base_pack_promotion_eligible"] is False
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED

    shipped = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name not in {"ecosystem.json", "executables.v4.json"}
    }
    shipped -= V4_AUTHORITY_ARTIFACTS
    indexed = {item for values in metadata["asset_index"].values() for item in values}
    assert shipped == indexed == set(REQUIRED_ASSETS)

    asset_index = read_yaml(PACK_DIR / "asset_index.yaml")["asset_index"]
    indexed_file_assets = {item for values in asset_index["categories"].values() for item in values}
    assert indexed_file_assets == shipped
    assert asset_index["invariants"]["external_actions_are_handoffs"] is True
    assert asset_index["invariants"]["base_pack_promotion_eligible"] is False


def test_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(read_yaml(path), dict), path
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
    assert candidate.base_pack_promotion["eligible"] is False
    assert set(candidate.base_pack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.base_pack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "browser-safety"
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

    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    ledger = read_yaml(PACK_DIR / "ledgers/replay_evidence_ledger.schema.yaml")["evidence_ledger_schema"]

    assert {item["id"] for item in workflows["items"]} == {
        "failed_flow_replay_manifest",
        "handoff_to_browser_owner",
        "redacted_replay_bundle",
        "selector_drift_audit",
        "trace_capture_review",
    }
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert {item["id"] for item in quality["checks"]} >= {
        "asset_index_complete",
        "browser_execution_handoff_only",
        "hash_refs_not_inline_binaries",
        "observed_events_only",
        "redaction_before_share",
        "selector_drift_evidence_present",
        "transport_owner_named",
    }
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["external_effect"] == "handoff_packet_only"
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    for key, expected in OVERLAP_EXPECTED.items():
        assert handoff_policy["overlap_policy"][key] == expected
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"


def test_browser_session_replay_observed_only_and_redaction_contracts() -> None:
    event_schema = read_json(PACK_DIR / "schemas/browser_event_evidence.schema.json")
    replay_schema = read_json(PACK_DIR / "schemas/replay_manifest.schema.json")
    screenshot_schema = read_json(PACK_DIR / "schemas/screenshot_evidence.schema.json")
    redaction = read_yaml(PACK_DIR / "policies/session_replay_redaction.policy.yaml")["policy"]
    boundary = read_yaml(PACK_DIR / "policies/replay_safety_boundary.policy.yaml")["policy"]
    taxonomy = read_yaml(PACK_DIR / "catalog/replay_event_taxonomy.yaml")["replay_event_taxonomy"]

    forbidden = {"click", "type", "press", "scroll", "submit", "navigate"}
    assert event_schema["properties"]["execution_effect"]["const"] == "observation_only"
    assert not (forbidden & set(event_schema["properties"]["event_type"]["enum"]))
    assert forbidden <= set(taxonomy["forbidden_owned_actions"])
    assert replay_schema["properties"]["execution_owner"]["const"] == "rumi_browser_automation_pack"
    assert replay_schema["properties"]["transport_owner"]["const"] == "rumi_default_tools_pack"
    assert replay_schema["properties"]["external_action"]["const"] == "handoff_only"
    assert screenshot_schema["properties"]["binary_inline_allowed"]["const"] is False
    assert redaction["default"] == "block_or_redact_before_share"
    assert {"raw", "partially_redacted", "redacted", "blocked_sensitive"} <= set(redaction["states"])
    assert {"private_url", "payment_field", "typed_input_evidence"} <= set(redaction["sensitive_classes"])
    assert {"open_page", "click", "type", "submit_form", "call_browser_companion", "transport"} <= set(boundary["never_owns"])


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) >= 3
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)
    assert all(item["handoff_owner"] for item in examples)
    assert any("typed_input_evidence" in item["redaction"]["sensitive_classes"] for item in examples)
    assert any(item.get("selector_drift_report", {}).get("semantic_owner") == "rumi_browser_element_pack" for item in examples)
    assert any(item.get("replay_manifest", {}).get("external_action") == "handoff_only" for item in examples)

    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"

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
    for boundary in [
        "does not execute browser actions",
        "rumi_browser_automation_pack",
        "rumi_browser_element_pack",
        "rumi_default_tools_pack",
    ]:
        assert boundary in docs


def test_pack_body_has_no_credentials_or_runtime_surfaces() -> None:
    assert {path.name for path in PACK_DIR.iterdir() if path.is_dir()} & FORBIDDEN_DIRS == set()
    assert [path for path in PACK_DIR.rglob("*") if path.is_file() and path.suffix in FORBIDDEN_EXTENSIONS] == []

    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()] + [SETUP_PACK_JSON]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    generated_key_prefix = "s" + "k-"
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    for phrase in [generated_key_prefix, private_key_marker, "password=", "sample user request", "reviewer_ready_plan", "TODO"]:
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
