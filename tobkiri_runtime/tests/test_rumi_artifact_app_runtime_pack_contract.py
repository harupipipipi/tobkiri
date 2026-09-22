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
PACK_ID = 'rumi_artifact_app_runtime_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"
PACK_METADATA_FILES = {
    "ecosystem.json",
    "rumi.pack.v3.json",
    "artifact-manifest.json",
    "executables.v4.json",
    "frontend/contributions/artifact-app-runtime.json",
}
REQUIRED_ASSETS = ['README.md', 'asset_index.json', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/renderer_capability_catalog.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'docs/security.md', 'examples/export_package.example.yaml', 'examples/mcp_approval_prompt.example.yaml', 'examples/sample_calculator_manifest.example.yaml', 'examples/version_rollback.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'policies/sandbox_renderer.policy.yaml', 'policies/tool_mcp_approval.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/artifact_runtime_reviewer.profile.yaml', 'prompts/artifact_runtime_reviewer.system.md', 'schemas/artifact_app_error.schema.json', 'schemas/artifact_app_manifest.schema.json', 'schemas/artifact_state_snapshot.schema.json', 'schemas/export_package.schema.json', 'schemas/renderer_sandbox_contract.schema.json', 'schemas/runtime_error_boundary.schema.json', 'schemas/share_package.schema.json', 'schemas/storage_version_selector.schema.json', 'schemas/tool_approval_prompt.schema.json', 'schemas/version_record.schema.json', 'templates/handoff.template.md', 'templates/review_report.template.md', 'templates/ui_contract.template.md']
SCHEMA_EXPECTATIONS = {'schemas/artifact_app_error.schema.json': ['error_id', 'artifact_id', 'error_class', 'safe_message', 'stack_redacted', 'recovery_handoff', 'correlation_id', 'details_redacted', 'raw_stack_included', 'handoff_packet_required'], 'schemas/artifact_app_manifest.schema.json': ['app_id', 'artifact_id', 'name', 'schema_version', 'entrypoint', 'content_ref', 'permissions', 'state_schema_id', 'version_id', 'network_default', 'renderer', 'storage_selector', 'approval_policy', 'export_share', 'error_boundary'], 'schemas/artifact_state_snapshot.schema.json': ['snapshot_id', 'artifact_id', 'version_id', 'state_digest', 'created_from_event_ids', 'storage_handoff'], 'schemas/export_package.schema.json': ['export_id', 'artifact_id', 'version_id', 'included_files', 'excluded_capabilities', 'review_state', 'workspace_handoff', 'package_digest', 'package_contract_only', 'file_persistence_owner', 'zip_creation_owner', 'execution_effect'], 'schemas/renderer_sandbox_contract.schema.json': ['sandbox_id', 'artifact_id', 'csp', 'allowed_origins', 'runtime_owner', 'host_execution', 'sandbox_tokens', 'untrusted_forbidden_token_pairs', 'remote_modules_allowed', 'same_origin_required', 'trusted_renderer_prefixes'], 'schemas/runtime_error_boundary.schema.json': ['boundary_id', 'artifact_id', 'error_class', 'safe_fallback', 'captured_event_ids', 'handoff_owner', 'error_envelope_schema_id', 'raw_stack_included', 'fallback_mode'], 'schemas/share_package.schema.json': ['share_id', 'artifact_id', 'version_id', 'visibility', 'permission_scope', 'checksum', 'workspace_handoff', 'share_contract_only', 'link_creation_owner', 'file_persistence_owner', 'token_creation_allowed'], 'schemas/storage_version_selector.schema.json': ['selector_id', 'artifact_id', 'selector_kind', 'pinned_version_id', 'checksum', 'storage_owner', 'path_policy', 'source_owner', 'source_ref', 'read_only', 'allowed_sources', 'client_supplied_path_trusted'], 'schemas/tool_approval_prompt.schema.json': ['approval_id', 'artifact_id', 'tool_ref', 'scope', 'approval_state', 'first_call', 'receipt', 'approval_required', 'requires_approval', 'approval_request_id', 'operation', 'risk_level', 'args_hash', 'expires_at', 'display_summary', 'redacted_arguments', 'client_supplied_approved_trusted', 'tool_call_id', 'payload', 'execution_owner', 'trusted_authority', 'server_issued_approval_token_required'], 'schemas/version_record.schema.json': ['version_id', 'artifact_id', 'parent_version_id', 'change_summary', 'rollback_allowed', 'source_snapshot_id']}
WORKFLOW_IDS = set(['manifest_validation', 'sandbox_render_contract', 'state_snapshot_versioning', 'tool_mcp_approval_gate', 'error_boundary_review', 'export_package_build'])
QUALITY_CHECK_IDS = set(['network_denied_by_default', 'first_tool_call_approval', 'client_approved_never_trusted', 'export_share_package_only', 'sandbox_owner_named', 'workspace_handoff_for_storage', 'rollback_parent_present', 'error_boundary_safe_fallback', 'no_direct_execution', 'asset_index_complete'])
OWNER_EXPECTED = set(['artifact_app_manifest', 'sandbox_renderer_contract', 'artifact_state_snapshot', 'artifact_version_selector', 'tool_mcp_approval_prompt', 'runtime_error_boundary', 'share_export_manifest', 'artifact_runtime_ui_contract'])
NON_OWNER_EXPECTED = set(['frontend design generation', 'file persistence', 'sandbox isolation runtime', 'MCP execution', 'API execution', 'media transforms', 'browser automation'])
OVERLAP_EXPECTED = {'frontend_design_generation': 'handoff_to_rumi_reference_ui_pack', 'file_persistence': 'handoff_to_defaultspack', 'sandbox_isolation_runtime': 'handoff_to_defaultspack', 'mcp_execution': 'handoff_to_defaultspack', 'api_execution': 'handoff_to_defaultspack', 'media_transform': 'handoff_to_defaultspack', 'browser_automation': 'handoff_to_rumi_default_tools_pack', 'defaultspack_artifact_store': 'do_not_override', 'defaultspack_chat_artifact_file': 'read_only_selector_only', 'defaultspack_share_links': 'do_not_override', 'defaultspack_tool_execution': 'do_not_override', 'defaultspack_mcp_execution': 'do_not_override', 'artifact_manifest_contract': 'owned_by_rumi_artifact_app_runtime_pack', 'tool_approval_prompt': 'owned_by_rumi_artifact_app_runtime_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['no_renderer_registry_runtime', 'no_per_artifact_storage_runtime', 'sandbox_execution_owned_elsewhere', 'mcp_api_execution_owned_elsewhere', 'approval_receipts_required_for_tool_calls', 'supports_all_ok_false_required', 'no_file_persistence_owner', 'no_media_transform_owner', 'no_mcp_api_execution_owner', 'client_supplied_approved_never_trusted', 'schema_contracts_only'])
PROMOTION_EVIDENCE = set(['sample_app_manifest_cases', 'tool_approval_denial_cases', 'version_rollback_cases', 'error_boundary_cases', 'export_manifest_cases', 'sandbox_token_cases', 'storage_selector_cases', 'share_export_checksum_cases'])
BLOCKED_BY_DEFAULT = set(['execute artifact code directly', 'call MCP tools before approval', 'allow network by default', 'persist files without workspace handoff', 'bypass sandbox runtime owner', 'trust client supplied approved flag', 'create share links directly', 'zip/export files directly', 'run media transforms', 'mutate defaultspack stores'])
HANDOFF_TARGETS = set(['defaultspack', 'rumi_default_tools_pack', 'rumi_reference_ui_pack'])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def available_setup_pack_ids() -> set[str]:
    selector = PackSelector(ROOT / "ecosystem")
    return {item.pack_id for item in selector.scan_candidates()}


def test_required_assets_and_ecosystem_contract() -> None:
    assert [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert ecosystem["host_execution"] is False
    metadata = ecosystem["metadata"]
    assert metadata["runtime_type"] == "declarative_setup_pack"
    assert metadata["network_policy"] == "none_by_default"
    assert metadata["executable_code"] is False
    assert metadata["declarative_only"] is True
    assert metadata["consumes_existing_sources_only"] is True
    assert metadata["output_effect"] == "draft_and_handoff_only"
    assert metadata["defaultspack_promotion_eligible"] is False
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    available = available_setup_pack_ids()
    assert HANDOFF_TARGETS <= available
    optional_integrations = {item["pack_id"]: item["reason"] for item in metadata["optional_integrations"]}
    assert HANDOFF_TARGETS <= set(optional_integrations)
    assert "UI" in optional_integrations["rumi_reference_ui_pack"]
    assert "sandbox" in optional_integrations["defaultspack"]
    assert "browser" in optional_integrations["rumi_default_tools_pack"]
    actual = {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.relative_to(PACK_DIR).as_posix() not in PACK_METADATA_FILES
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
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == 'medium'
    assert setup["compatibility"]["python"] == ">=3.9"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    issues = selector.validate_candidates(installed_packs={"defaultspack": {"version": "2.0.0"}}, platform_name="linux", python_version="3.11.0")
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []
    for key, value in OVERLAP_EXPECTED.items():
        assert candidate.overlap_policy[key] == value
    assert any(value.startswith("owned_by_") for value in candidate.overlap_policy.values())
    assert any("handoff" in value for value in candidate.overlap_policy.values())
    assert candidate.defaultspack_promotion["eligible"] is False
    assert set(candidate.defaultspack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'artifact-app-runtime'
    assert candidate.signing["verified"] is True


def test_schema_workflow_quality_and_policy_contracts() -> None:
    for rel_path, required in SCHEMA_EXPECTATIONS.items():
        schema = read_json(PACK_DIR / rel_path)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) >= set(required)
        assert set(required) <= set(schema["properties"])
    assert len(SCHEMA_EXPECTATIONS) >= 7

    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    taxonomy = read_yaml(PACK_DIR / "catalog/taxonomy.yaml")["taxonomy"]
    ledger = read_yaml(PACK_DIR / "ledgers/evidence_ledger.schema.yaml")["evidence_ledger_schema"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert all(set(item["handoffs"]) >= HANDOFF_TARGETS for item in workflows["items"])
    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["default_mode"] == "draft_and_handoff_only"
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    for key, expected in OVERLAP_EXPECTED.items():
        assert handoff_policy["overlap_policy"][key] == expected
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    matrix_resolution = {item["surface"]: item["resolution"] for item in handoff_matrix["items"]}
    for key, expected in OVERLAP_EXPECTED.items():
        if key in {"artifact_manifest_contract", "tool_approval_prompt", "tool_aliases"}:
            continue
        assert matrix_resolution[key] == expected
    assert HANDOFF_TARGETS <= set(taxonomy["handoff_targets"])
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) >= 4
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"
    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    profile = read_yaml(next((PACK_DIR / "profiles").glob("*.profile.yaml")))["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "Handoff", "Does Not Provide"]:
        assert expected in docs


def test_pack_body_has_no_credentials_or_skeleton_phrases() -> None:
    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()] + [SETUP_PACK_JSON]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    generated_key_prefix = "s" + "k-"
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    for phrase in [generated_key_prefix, private_key_marker, "auth token", "password=", "sample user request", "reviewer_ready_plan", "Complementary owner surface"]:
        assert phrase not in combined
    generated_key_pattern = r"s" + r"k-[A-Za-z0-9_-]{20,}"
    assert re.search(generated_key_pattern, combined) is None


def _tool_call_allowed(prompt: dict, manifest: dict) -> tuple[bool, str]:
    if manifest.get("network_default") != "deny":
        return False, "network_not_denied"
    if prompt.get("first_call") and prompt.get("approval_state") != "approved":
        return False, "first_call_requires_approval"
    if prompt.get("approval_state") == "denied":
        return False, "approval_denied"
    return True, "handoff_to_owner_pack"


def _select_rollback_version(versions: list[dict], current: str) -> str | None:
    by_id = {item["version_id"]: item for item in versions}
    current_record = by_id[current]
    parent = current_record.get("parent_version_id")
    if current_record.get("rollback_allowed") and parent in by_id:
        return parent
    return None


def test_artifact_runtime_approval_network_and_rollback_contract() -> None:
    manifest = {"artifact_id": "artifact_1", "network_default": "deny"}
    pending = {"first_call": True, "approval_state": "pending"}
    approved = {"first_call": True, "approval_state": "approved"}
    unsafe_manifest = {"artifact_id": "artifact_1", "network_default": "allow"}
    assert _tool_call_allowed(pending, manifest) == (False, "first_call_requires_approval")
    assert _tool_call_allowed(approved, manifest) == (True, "handoff_to_owner_pack")
    assert _tool_call_allowed(approved, unsafe_manifest) == (False, "network_not_denied")
    versions = [
        {"version_id": "v1", "parent_version_id": "", "rollback_allowed": False},
        {"version_id": "v2", "parent_version_id": "v1", "rollback_allowed": True},
    ]
    assert _select_rollback_version(versions, "v2") == "v1"
def test_artifact_runtime_subagent_acceptance_assets() -> None:
    approval = read_json(PACK_DIR / "schemas/tool_approval_prompt.schema.json")
    selector = read_json(PACK_DIR / "schemas/storage_version_selector.schema.json")
    share = read_json(PACK_DIR / "schemas/share_package.schema.json")
    error = read_json(PACK_DIR / "schemas/artifact_app_error.schema.json")
    policy = read_yaml(PACK_DIR / "policies/tool_mcp_approval.policy.yaml")["policy"]
    sandbox = read_yaml(PACK_DIR / "policies/sandbox_renderer.policy.yaml")["policy"]
    catalog = read_yaml(PACK_DIR / "catalog/renderer_capability_catalog.yaml")["renderer_capabilities"]
    required_approval = {"approval_required", "requires_approval", "approval_request_id", "operation", "risk_level", "args_hash", "expires_at", "display_summary", "redacted_arguments", "client_supplied_approved_trusted"}
    assert required_approval <= set(approval["required"])
    assert approval["properties"]["client_supplied_approved_trusted"]["const"] is False
    assert selector["properties"]["path_policy"]["enum"] == ["no_path_traversal", "trusted_workspace_only"]
    assert share["properties"]["workspace_handoff"]["properties"]["owner_pack"]["const"] == "defaultspack"
    assert error["properties"]["stack_redacted"]["const"] is True
    assert policy["client_supplied_approved"] == "never_trusted"
    assert "allow-scripts + allow-same-origin" in sandbox["untrusted_forbidden_sandbox_pairs"]
    assert "remote_module" in catalog["blocked"]


def test_phase2_hardened_runtime_boundaries() -> None:
    manifest = read_json(PACK_DIR / "schemas/artifact_app_manifest.schema.json")
    sandbox_schema = read_json(PACK_DIR / "schemas/renderer_sandbox_contract.schema.json")
    selector = read_json(PACK_DIR / "schemas/storage_version_selector.schema.json")
    approval = read_json(PACK_DIR / "schemas/tool_approval_prompt.schema.json")
    export = read_json(PACK_DIR / "schemas/export_package.schema.json")
    share = read_json(PACK_DIR / "schemas/share_package.schema.json")
    runtime_error = read_json(PACK_DIR / "schemas/runtime_error_boundary.schema.json")
    app_error = read_json(PACK_DIR / "schemas/artifact_app_error.schema.json")
    sandbox_policy = read_yaml(PACK_DIR / "policies/sandbox_renderer.policy.yaml")["policy"]
    renderer_catalog = read_yaml(PACK_DIR / "catalog/renderer_capability_catalog.yaml")["renderer_capabilities"]
    tool_policy = read_yaml(PACK_DIR / "policies/tool_mcp_approval.policy.yaml")["policy"]
    safety = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]

    assert manifest["properties"]["app_id"]["pattern"] == "^[A-Za-z0-9_.-]+$"
    assert manifest["properties"]["schema_version"]["const"] == "rumi.artifact_app_manifest.v1"
    assert manifest["properties"]["network_default"]["const"] == "deny"
    assert manifest["properties"]["renderer"]["properties"]["trusted"]["const"] is False
    assert manifest["properties"]["storage_selector"]["properties"]["read_only"]["const"] is True
    assert manifest["properties"]["approval_policy"]["properties"]["execution_owner"]["const"] == "defaultspack"
    assert manifest["properties"]["approval_policy"]["properties"]["client_supplied_approved_trusted"]["const"] is False
    assert manifest["properties"]["export_share"]["properties"]["package_contract_only"]["const"] is True

    assert sandbox_schema["properties"]["runtime_owner"]["const"] == "defaultspack"
    assert sandbox_schema["properties"]["host_execution"]["const"] is False
    assert sandbox_schema["properties"]["remote_modules_allowed"]["const"] is False
    assert sandbox_schema["properties"]["same_origin_required"]["const"] is True
    assert {"/static/renderers/", "/static/assets/renderers/", "/static/user_renderers/"} <= set(
        sandbox_schema["properties"]["trusted_renderer_prefixes"]["items"]["enum"]
    )
    assert sandbox_policy["remote_modules_allowed"] is False
    assert sandbox_policy["safe_default_tokens"] == ["allow-same-origin"]
    assert ["allow-scripts", "allow-same-origin"] == renderer_catalog["token_policy"]["forbidden_pairs"][0]

    assert selector["properties"]["read_only"]["const"] is True
    assert selector["properties"]["client_supplied_path_trusted"]["const"] is False
    assert set(selector["properties"]["source_owner"]["enum"]) == {"defaultspack"}
    assert set(selector["properties"]["allowed_sources"]["items"]["enum"]) == {
        "defaultspack_artifact_index",
        "defaultspack_chat_workspace",
        "trusted_workspace_record",
    }

    assert approval["properties"]["operation"]["pattern"] == "^tool\\.[A-Za-z0-9_.:-]+$"
    assert approval["properties"]["execution_owner"]["const"] == "defaultspack"
    assert approval["properties"]["trusted_authority"]["const"] == "server_issued_approval_token"
    assert approval["properties"]["server_issued_approval_token_required"]["const"] is True
    assert approval["properties"]["client_supplied_approved_trusted"]["const"] is False
    assert tool_policy["trusted_authority"] == "server_issued_approval_token"
    assert tool_policy["execution_owner"] == "defaultspack"
    assert "approved" in tool_policy["forbidden_client_fields"]

    assert export["properties"]["package_contract_only"]["const"] is True
    assert export["properties"]["execution_effect"]["const"] == "none"
    assert export["properties"]["file_persistence_owner"]["const"] == "defaultspack"
    assert export["properties"]["zip_creation_owner"]["const"] == "defaultspack"
    assert set(export["properties"]["included_files"]["items"]["required"]) >= {"path", "source_ref", "version_id", "checksum"}
    assert share["properties"]["share_contract_only"]["const"] is True
    assert share["properties"]["link_creation_owner"]["const"] == "defaultspack"
    assert share["properties"]["token_creation_allowed"]["const"] is False
    assert runtime_error["properties"]["raw_stack_included"]["const"] is False
    assert app_error["properties"]["stack_redacted"]["const"] is True
    assert app_error["properties"]["raw_stack_included"]["const"] is False
    assert app_error["properties"]["handoff_packet_required"]["const"] is True

    for forbidden in ("blocks", "functions", "tools", "routes", "backend"):
        assert not (PACK_DIR / forbidden).exists(), forbidden
    assert "trust client supplied approved flag" in safety["blocked_by_default"]
    assert safety["owner_execution_policy"]["defaultspack_tool_execution"] == "handoff_only"
    assert safety["owner_execution_policy"]["defaultspack_artifact_store"] == "read_only_selector_only"
