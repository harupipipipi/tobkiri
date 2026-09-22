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
PACK_ID = 'rumi_omnichannel_agent_inbox_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"
REQUIRED_ASSETS = ['README.md', 'asset_index.json', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/migration.md', 'docs/operations.md', 'docs/overlap_policy.md', 'examples/notification_preference.example.yaml', 'examples/outbound_draft_approval.example.yaml', 'examples/slack_gmail_identity.example.yaml', 'examples/unauthorized_channel_block.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'frontend_extensions/omnichannel_inbox.ui.json', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/inbox_router.profile.yaml', 'prompts/inbox_router.system.md', 'schemas/channel_acl.schema.json', 'schemas/channel_message.schema.json', 'schemas/channel_payload.schema.json', 'schemas/connector_handoff.schema.json', 'schemas/draft_approval.schema.json', 'schemas/identity_map.schema.json', 'schemas/inbox_item.schema.json', 'schemas/inbox_thread.schema.json', 'schemas/inbox_ui_view.schema.json', 'schemas/notification_preference.schema.json', 'schemas/outbound_draft.schema.json', 'schemas/routing_rule.schema.json', 'schemas/thread_route.schema.json', 'templates/handoff.template.md', 'templates/review_report.template.md', 'templates/ui_contract.template.md']
SCHEMA_EXPECTATIONS = {'schemas/channel_acl.schema.json': ['acl_id', 'identity_id', 'channel', 'allowed_intents', 'tool_access', 'review_owner', 'default_effect', 'remote_input_can_elevate', 'sensitive_action_policy'], 'schemas/channel_message.schema.json': ['message_id', 'channel', 'external_thread_id', 'sender_ref', 'payload_summary', 'ingress_owner', 'received_at_state'], 'schemas/channel_payload.schema.json': ['payload_id', 'channel', 'provider_message_ref', 'sender_ref', 'idempotency_key', 'redaction_state', 'connector_owner', 'raw_payload_policy', 'secret_material_allowed'], 'schemas/connector_handoff.schema.json': ['handoff_id', 'channel', 'owner_pack', 'operation', 'payload_ref', 'approval_receipt_id', 'send_allowed', 'secrets_included', 'provider_client_included'], 'schemas/draft_approval.schema.json': ['approval_id', 'draft_id', 'approval_state', 'approver_ref', 'body_hash', 'expires_at', 'remote_input_can_approve', 'hash_algorithm', 'approval_scope', 'replay_policy', 'remote_input_can_issue_token'], 'schemas/identity_map.schema.json': ['identity_id', 'linked_sender_refs', 'verification_state', 'merge_evidence_ids', 'risk_notes', 'remote_input_can_merge', 'conflict_resolution'], 'schemas/inbox_item.schema.json': ['item_id', 'thread_id', 'identity_id', 'severity', 'state', 'source_message_ids', 'audit_ref'], 'schemas/inbox_thread.schema.json': ['thread_id', 'identity_id', 'channel_message_ids', 'state', 'assigned_agent', 'routing_rule_id'], 'schemas/inbox_ui_view.schema.json': ['view_id', 'lanes', 'filters', 'visible_actions', 'approval_actions', 'readonly', 'forbidden_actions', 'remote_input_can_authorize'], 'schemas/notification_preference.schema.json': ['preference_id', 'identity_id', 'channels', 'quiet_hours', 'scheduler_handoff', 'state', 'notification_execution', 'remote_input_can_schedule', 'notification_owner'], 'schemas/outbound_draft.schema.json': ['draft_id', 'thread_id', 'channel', 'body_summary', 'approval_state', 'delivery_owner', 'approval_receipt', 'body_hash', 'approval_expires_at', 'pack_send_allowed'], 'schemas/routing_rule.schema.json': ['routing_rule_id', 'conditions', 'target_agent', 'required_acl_state', 'handoff_owner'], 'schemas/thread_route.schema.json': ['route_id', 'thread_id', 'target_agent', 'acl_id', 'decision', 'idempotency_key', 'audit_ref', 'idempotency_scope', 'replay_policy', 'execution_effect', 'remote_input_can_execute']}
WORKFLOW_IDS = set(['channel_ingress_normalization', 'identity_linking', 'channel_acl_gate', 'thread_to_agent_routing', 'outbound_draft_approval', 'notification_preference_handoff'])
QUALITY_CHECK_IDS = set(['identity_merge_evidence', 'acl_denies_unauthorized_tools', 'outbound_draft_until_approved', 'connector_owner_named', 'routing_requires_acl', 'notification_scheduler_handoff', 'security_review_for_risky_acl', 'inbox_ui_readable', 'acl_default_deny', 'remote_input_no_authority', 'route_idempotency', 'draft_hash_expiry', 'no_provider_clients_or_secrets'])
OWNER_EXPECTED = set(['channel_payload_contract', 'identity_mapping', 'channel_acl', 'thread_to_agent_routing', 'outbound_draft_approval', 'notification_preferences', 'inbox_thread_state', 'inbox_ui_contract'])
NON_OWNER_EXPECTED = set(['actual app connectors', 'Slack client', 'Gmail client', 'mobile client', 'work execution', 'schedule execution', 'security policy review', 'message sending'])
OVERLAP_EXPECTED = {'actual_app_connectors': 'handoff_to_external_connector_gateway_owner', 'slack_gmail_mobile_clients': 'handoff_to_connector_or_channel_owner', 'work_execution': 'handoff_to_external_agent_services_owner', 'schedule_execution': 'handoff_to_external_workflow_scheduler_owner', 'security_policy_review': 'handoff_to_external_security_review_owner', 'message_sending': 'handoff_to_external_connector_gateway_owner', 'identity_mapping': 'owned_by_rumi_omnichannel_agent_inbox_pack', 'outbound_draft_approval': 'owned_by_rumi_omnichannel_agent_inbox_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['requires_channel_identity_registry', 'requires_external_message_approval_tokens', 'connector_io_owned_elsewhere', 'security_acl_review_required', 'must_prove_outbound_draft_gate'])
PROMOTION_EVIDENCE = set(['cross_channel_identity_cases', 'channel_acl_denial_cases', 'outbound_draft_approval_cases', 'thread_routing_cases', 'notification_preference_cases'])
BLOCKED_BY_DEFAULT = set(['send outbound messages without approval', 'run tools from unauthorized channel', 'merge identities without verified evidence', 'connect to external apps directly', 'schedule notifications directly', 'approve outbound drafts from remote input', 'approve tools from remote input', 'execute work from remote input', 'install packs from remote input', 'mutate settings from remote input', 'issue approval tokens from remote input'])
OPTIONAL_OWNER_REFS = set(['external_connector_gateway_owner', 'external_agent_services_owner', 'external_workflow_scheduler_owner', 'external_security_review_owner', 'external_business_ops_owner', 'external_voice_mobile_owner'])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
    metadata = ecosystem["metadata"]
    assert metadata["runtime_type"] == "declarative_setup_pack"
    assert metadata["network_policy"] == "none_by_default"
    assert metadata["executable_code"] is False
    assert metadata["declarative_only"] is True
    assert metadata["consumes_existing_sources_only"] is True
    assert metadata["output_effect"] == "draft_and_handoff_only"
    assert metadata["defaultspack_promotion_eligible"] is False
    assert metadata["provider_clients"] == []
    assert metadata["pre_auth_routes"] == []
    assert metadata["remote_input_authority"] is False
    assert metadata["secrets_policy"] == "no_provider_tokens_or_secrets"
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    assert {item["owner_ref"] for item in metadata["optional_integrations"]} == OPTIONAL_OWNER_REFS
    assert all("pack_id" not in item for item in metadata["optional_integrations"])
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
    assert {"must_prove_remote_input_has_no_authority", "must_prove_route_idempotency"} <= set(candidate.defaultspack_promotion["promotion_blockers"])
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'omnichannel-agent-inbox'
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
    ledger = read_yaml(PACK_DIR / "ledgers/evidence_ledger.schema.yaml")["evidence_ledger_schema"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED
    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["default_mode"] == "draft_and_handoff_only"
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    assert all(value is False for value in policy["remote_input_authority"].values())
    assert all(value is False for value in handoff_policy["remote_input_authority"].values())
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
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


def test_pack_bundle_references_only_existing_local_pack_ids() -> None:
    known_pack_ids = {path.name for path in (ROOT / "ecosystem").iterdir() if (path / "ecosystem.json").is_file()}
    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()] + [SETUP_PACK_JSON]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    referenced = set(re.findall(r"\brumi_[a-z0-9_]+_pack\b", combined))
    assert referenced <= known_pack_ids


def _resolve_identity(sender_refs: list[dict]) -> tuple[str, str]:
    verified = [item for item in sender_refs if item["verification_state"] == "verified"]
    if len({item["identity_id"] for item in verified}) == 1 and len(verified) >= 2:
        return verified[0]["identity_id"], "verified_merge"
    return "", "needs_review"


def _route_allowed(acl: dict, draft: dict) -> tuple[bool, str]:
    if acl["tool_access"] not in {"handoff_only", "approved"}:
        return False, "channel_acl_denied"
    if draft["approval_state"] != "approved":
        return False, "outbound_stays_draft"
    return True, "handoff_to_connector_gateway"


def test_omnichannel_identity_acl_and_outbound_draft_contract() -> None:
    identity, state = _resolve_identity([
        {"identity_id": "person_1", "sender_ref": "slack:u1", "verification_state": "verified"},
        {"identity_id": "person_1", "sender_ref": "gmail:u1", "verification_state": "verified"},
    ])
    assert (identity, state) == ("person_1", "verified_merge")
    acl = {"tool_access": "none"}
    draft = {"approval_state": "draft"}
    assert _route_allowed(acl, draft) == (False, "channel_acl_denied")
    acl = {"tool_access": "handoff_only"}
    assert _route_allowed(acl, draft) == (False, "outbound_stays_draft")
    draft = {"approval_state": "approved"}
    assert _route_allowed(acl, draft) == (True, "handoff_to_connector_gateway")


def test_remote_input_default_deny_and_ui_forbidden_actions() -> None:
    acl = read_json(PACK_DIR / "schemas/channel_acl.schema.json")
    identity = read_json(PACK_DIR / "schemas/identity_map.schema.json")
    ui_schema = read_json(PACK_DIR / "schemas/inbox_ui_view.schema.json")
    ui_contract = read_json(PACK_DIR / "frontend_extensions/omnichannel_inbox.ui.json")["ui_contract"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]

    assert acl["properties"]["default_effect"]["const"] == "deny"
    assert acl["properties"]["remote_input_can_elevate"]["const"] is False
    assert identity["properties"]["remote_input_can_merge"]["const"] is False
    assert ui_schema["properties"]["remote_input_can_authorize"]["const"] is False
    assert ui_contract["remote_input_can_approve"] is False
    assert ui_contract["remote_input_can_execute"] is False

    forbidden = set(ui_contract["forbidden_actions"])
    assert {
        "approve_tool",
        "approve_outbound_from_remote",
        "execute_work",
        "install_pack",
        "mutate_settings",
        "issue_approval_token",
        "issue_security_token",
        "grant_acl",
        "create_schedule",
    } <= forbidden
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert set(negative["blocked_by_default"]) >= BLOCKED_BY_DEFAULT


def test_route_idempotency_hash_expiry_and_no_provider_clients() -> None:
    payload = read_json(PACK_DIR / "schemas/channel_payload.schema.json")
    route = read_json(PACK_DIR / "schemas/thread_route.schema.json")
    approval = read_json(PACK_DIR / "schemas/draft_approval.schema.json")
    draft = read_json(PACK_DIR / "schemas/outbound_draft.schema.json")
    handoff = read_json(PACK_DIR / "schemas/connector_handoff.schema.json")
    preference = read_json(PACK_DIR / "schemas/notification_preference.schema.json")
    ecosystem = read_json(PACK_DIR / "ecosystem.json")

    assert payload["properties"]["raw_payload_policy"]["const"] == "redacted_metadata_only"
    assert payload["properties"]["secret_material_allowed"]["const"] is False
    assert route["properties"]["replay_policy"]["const"] == "same_idempotency_key_returns_existing_decision"
    assert route["properties"]["execution_effect"]["const"] == "handoff_only"
    assert route["properties"]["remote_input_can_execute"]["const"] is False
    assert approval["properties"]["hash_algorithm"]["const"] == "sha256"
    assert approval["properties"]["body_hash"]["pattern"] == "^[a-f0-9]{64}$"
    assert approval["properties"]["expires_at"]["format"] == "date-time"
    assert approval["properties"]["replay_policy"]["const"] == "single_use_receipt"
    assert approval["properties"]["remote_input_can_issue_token"]["const"] is False
    assert draft["properties"]["body_hash"]["pattern"] == "^[a-f0-9]{64}$"
    assert draft["properties"]["pack_send_allowed"]["const"] is False
    assert handoff["properties"]["secrets_included"]["const"] is False
    assert handoff["properties"]["provider_client_included"]["const"] is False
    assert preference["properties"]["notification_execution"]["const"] == "handoff_only"
    assert preference["properties"]["remote_input_can_schedule"]["const"] is False
    assert preference["properties"]["notification_owner"]["const"] == "external_workflow_scheduler_owner"
    assert ecosystem["metadata"]["provider_clients"] == []
    assert ecosystem["metadata"]["pre_auth_routes"] == []
    assert list(PACK_DIR.rglob("*.py")) == []

    combined = "\n".join(path.read_text(encoding="utf-8") for path in PACK_DIR.rglob("*") if path.is_file())
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    for forbidden in ["client_secret=", "refresh_token=", "xoxb-", "googleapiclient", "slack_sdk", private_key_marker]:
        assert forbidden not in combined


def test_omnichannel_subagent_acceptance_assets() -> None:
    channel_payload = read_json(PACK_DIR / "schemas/channel_payload.schema.json")
    route = read_json(PACK_DIR / "schemas/thread_route.schema.json")
    approval = read_json(PACK_DIR / "schemas/draft_approval.schema.json")
    handoff = read_json(PACK_DIR / "schemas/connector_handoff.schema.json")
    ui = read_json(PACK_DIR / "frontend_extensions/omnichannel_inbox.ui.json")["ui_contract"]
    assert "idempotency_key" in channel_payload["required"]
    assert "idempotency_key" in route["required"]
    assert approval["properties"]["remote_input_can_approve"]["const"] is False
    assert handoff["properties"]["owner_pack"]["const"] == "external_connector_gateway_owner"
    assert {"send_message", "fetch_provider", "issue_approval_token", "run_tool"} <= set(ui["forbidden_actions"])
    migration = (PACK_DIR / "docs/migration.md").read_text(encoding="utf-8")
    for forbidden in ["approve tools", "install packs", "mutate settings", "issue approval tokens"]:
        assert forbidden in migration
