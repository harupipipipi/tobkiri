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
PACK_ID = 'rumi_telephony_delegate_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"

REQUIRED_ASSETS = ['README.md', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/interfaces.md', 'docs/operations.md', 'examples/appointment_reschedule_script.example.yaml', 'examples/customer_callback_mock.example.yaml', 'examples/never_call_blocked_case.example.yaml', 'examples/transcript_redaction_case.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/telephony_delegate_reviewer.profile.yaml', 'prompts/call_script_reviewer.system.md', 'schemas/call_session.schema.json', 'schemas/call_task.schema.json', 'schemas/dial_approval.schema.json', 'schemas/escalation_record.schema.json', 'schemas/never_call_entry.schema.json', 'schemas/pre_call_script.schema.json', 'schemas/transcript_redaction.schema.json', 'templates/artifact_card.template.md', 'templates/handoff.template.md', 'templates/review_report.template.md']
SCHEMA_EXPECTATIONS = {'schemas/call_task.schema.json': ['call_task_id', 'phone_target_alias', 'purpose', 'allowed_intent', 'approval_state', 'pre_call_script_id', 'never_call_checked', 'human_approval_receipt'], 'schemas/pre_call_script.schema.json': ['pre_call_script_id', 'opening_disclosure', 'consent_question', 'allowed_topics', 'forbidden_topics', 'takeover_triggers', 'script_hash'], 'schemas/dial_approval.schema.json': ['approval_id', 'call_task_id', 'approved_number_alias', 'approved_script_id', 'approval_state', 'approver_role', 'expires_at', 'audit_evidence_ids'], 'schemas/call_session.schema.json': ['session_id', 'call_task_id', 'state', 'consent_state', 'takeover_required', 'escalation_reasons', 'transcript_redaction_id'], 'schemas/transcript_redaction.schema.json': ['transcript_id', 'redaction_rules', 'redacted_segments', 'review_state', 'pii_classes', 'handoff_owner'], 'schemas/never_call_entry.schema.json': ['entry_id', 'phone_target_alias', 'reason', 'effective_until', 'source_evidence_id', 'enforcement_state'], 'schemas/escalation_record.schema.json': ['escalation_id', 'call_task_id', 'trigger', 'takeover_owner', 'abort_required', 'reason', 'evidence_ids']}
WORKFLOW_IDS = set(['pre_call_script_review', 'never_call_screen', 'mock_dial_approval', 'call_session_handoff', 'transcript_redaction_review', 'escalation_takeover'])
QUALITY_CHECK_IDS = set(['script_present', 'consent_disclosure_present', 'never_call_checked', 'approval_state_approved', 'approved_number_matches', 'disallowed_intent_aborts', 'consent_decline_aborts', 'transcript_pii_redacted', 'takeover_owner_present', 'takeover_abort_required'])
OWNER_EXPECTED = set(['call_task_contract', 'pre_call_script', 'dial_approval_gate', 'consent_disclosure_script', 'call_session_state', 'takeover_escalation', 'transcript_redaction_contract', 'never_call_list', 'mock_dial_readiness'])
NON_OWNER_EXPECTED = set(['actual dialing', 'ASR/TTS runtime', 'contact lookup', 'calendar mutation', 'payment or purchase execution', 'external connector writes', 'emergency services'])
OVERLAP_EXPECTED = {'actual_dialing': 'blocked_handoff_to_defaultspack_tool_runtime', 'asr_tts_runtime': 'handoff_to_defaultspack_tool_runtime', 'media_transcript_runtime': 'handoff_to_defaultspack_tool_runtime', 'contact_or_calendar_lookup': 'handoff_to_defaultspack_tool_runtime', 'meeting_recap': 'handoff_to_rumi_meeting_intelligence_pack', 'external_business_action': 'handoff_to_rumi_operations_company_pack', 'real_world_action_risk_review': 'handoff_to_rumi_operations_company_pack', 'telephony_contract': 'owned_by_rumi_telephony_delegate_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['no_actual_dialing_runtime', 'requires_external_real_world_action_approval_class', 'requires_never_call_policy', 'requires_redactable_transcript_storage', 'must_pass_disallowed_intent_abort_cases'])
PROMOTION_EVIDENCE = set(['mock_dial_approval_cases', 'never_call_block_cases', 'transcript_redaction_cases', 'takeover_escalation_cases', 'operations_company_takeover_acceptance_cases', 'provider_handoff_acceptance_cases'])
BLOCKED_BY_DEFAULT = set(['dial without human approval', 'call numbers on the never-call list', 'continue after consent is declined', 'continue after disallowed intent is detected', 'perform payment or purchase execution', 'handle emergency services', 'mask actual dialing as a mock handoff', 'store unredacted transcript PII in a handoff packet', 'use raw phone numbers instead of target aliases'])
NEGATIVE_CASE_IDS = set(['pending_approval_negative', 'never_call_negative', 'consent_declined_negative', 'disallowed_intent_negative', 'unredacted_transcript_negative', 'takeover_abort_negative'])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_ecosystem_contract() -> None:
    missing = [path for path in REQUIRED_ASSETS if not (PACK_DIR / path).is_file()]
    assert missing == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["host_execution"] is False
    assert ecosystem["metadata"]["runtime_type"] == "declarative_setup_pack"
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["declarative_only"] is True
    assert ecosystem["metadata"]["consumes_existing_sources_only"] is True
    assert ecosystem["metadata"]["output_effect"] == "draft_and_handoff_only"
    assert ecosystem["metadata"]["base_pack_promotion_eligible"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(ecosystem["metadata"]["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    available = {item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()}
    assert {item["pack_id"] for item in ecosystem["metadata"]["optional_integrations"]} <= available

    metadata_indexed = {item for values in ecosystem["metadata"]["asset_index"].values() for item in values}
    actual = {
        str(path.relative_to(PACK_DIR))
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name not in {"ecosystem.json", "executables.v4.json"}
    }
    actual -= V4_AUTHORITY_ARTIFACTS
    assert metadata_indexed == actual
    assert set(REQUIRED_ASSETS) == actual

    asset_index = read_yaml(PACK_DIR / "asset_index.yaml")["asset_index"]
    indexed_file_assets = {item for values in asset_index["categories"].values() for item in values}
    assert indexed_file_assets == actual
    assert asset_index["invariants"] == {
        "required_secrets": [],
        "required_network": [],
        "executable_code": False,
        "supports_all_ok": False,
        "external_actions_are_handoffs": True,
        "base_pack_promotion_eligible": False,
    }


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path
    for path in PACK_DIR.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]

    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == 'high'
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
    owned = {key: value for key, value in candidate.overlap_policy.items() if value.startswith("owned_by_")}
    handed = {key: value for key, value in candidate.overlap_policy.items() if "handoff" in value}
    assert owned
    assert handed
    assert candidate.overlap_policy["tool_aliases"] == "prefer_explicit_pack_namespace"

    assert candidate.base_pack_promotion["eligible"] is False
    assert set(candidate.base_pack_promotion["promotion_blockers"]) >= PROMOTION_BLOCKERS
    assert set(candidate.base_pack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'telephony-delegation'
    assert candidate.signing["mode"] == "repository_reviewed"
    assert candidate.signing["verified"] is True


def test_schema_required_fields_are_domain_specific() -> None:
    available = {item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()}
    for rel_path, expected_required in SCHEMA_EXPECTATIONS.items():
        schema = read_json(PACK_DIR / rel_path)
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) >= set(expected_required)
        assert set(expected_required) <= set(schema["properties"])
    assert len(SCHEMA_EXPECTATIONS) >= 6

    call_task = read_json(PACK_DIR / "schemas/call_task.schema.json")
    approval_receipt = call_task["properties"]["human_approval_receipt"]
    assert {"approval_scope", "explicit_human_approval"} <= set(approval_receipt["required"])
    assert approval_receipt["properties"]["approval_scope"]["const"] == "single_provider_handoff_packet"
    assert approval_receipt["properties"]["explicit_human_approval"]["const"] is True

    pre_call_script = read_json(PACK_DIR / "schemas/pre_call_script.schema.json")
    assert pre_call_script["properties"]["consent_decline_instruction"]["const"] == "abort_and_escalate"
    trigger_item = pre_call_script["properties"]["takeover_triggers"]["items"]
    assert "abort_required" in trigger_item["required"]
    assert "disallowed_intent" in trigger_item["properties"]["trigger"]["enum"]
    assert set(trigger_item["properties"]["handoff_owner"]["enum"]) - {"human_user"} <= available

    dial_approval = read_json(PACK_DIR / "schemas/dial_approval.schema.json")
    human_confirmation = dial_approval["properties"]["human_confirmation"]
    assert {"consent_disclosure_reviewed", "never_call_checked"} <= set(human_confirmation["required"])
    assert human_confirmation["properties"]["consent_disclosure_reviewed"]["const"] is True
    assert human_confirmation["properties"]["never_call_checked"]["const"] is True

    call_session = read_json(PACK_DIR / "schemas/call_session.schema.json")
    declined_rules = [
        rule for rule in call_session["allOf"]
        if rule["if"]["properties"].get("consent_state", {}).get("const") == "declined"
    ]
    assert declined_rules
    assert declined_rules[0]["then"]["properties"]["state"]["const"] == "aborted"
    assert declined_rules[0]["then"]["properties"]["takeover_required"]["const"] is True

    redaction = read_json(PACK_DIR / "schemas/transcript_redaction.schema.json")
    segment_pii_enum = redaction["properties"]["redacted_segments"]["items"]["properties"]["pii_class"]["enum"]
    assert {"phone", "email", "payment", "address", "account_id"} <= set(segment_pii_enum)
    assert redaction["properties"]["handoff_owner"]["const"] in available

    escalation = read_json(PACK_DIR / "schemas/escalation_record.schema.json")
    abort_triggers = escalation["allOf"][0]["if"]["properties"]["trigger"]["enum"]
    assert {"consent_declined", "disallowed_intent", "never_call_active"} <= set(abort_triggers)
    assert escalation["allOf"][0]["then"]["properties"]["abort_required"]["const"] is True
    assert set(escalation["properties"]["takeover_owner"]["enum"]) - {"human_user"} <= available


def test_workflows_quality_policy_and_handoffs_are_scoped() -> None:
    workflows = read_yaml(PACK_DIR / "catalog/workflows.yaml")["workflows"]
    quality = read_yaml(PACK_DIR / "catalog/quality_matrix.yaml")["quality_matrix"]
    policy = read_yaml(PACK_DIR / "policies/safety.policy.yaml")["policy"]
    handoff_policy = read_yaml(PACK_DIR / "policies/handoff.policy.yaml")["handoff_policy"]
    handoff_matrix = read_yaml(PACK_DIR / "catalog/handoff_matrix.yaml")["handoff_matrix"]
    ledger = read_yaml(PACK_DIR / "ledgers/evidence_ledger.schema.yaml")["evidence_ledger_schema"]
    checklist = read_yaml(PACK_DIR / "checklists/review.checklist.yaml")["review_checklist"]
    template = (PACK_DIR / "templates/handoff.template.md").read_text(encoding="utf-8")

    assert {item["id"] for item in workflows["items"]} == WORKFLOW_IDS
    assert workflows["default_execution"] == "no_runtime_action"
    assert all(item["execution"] == "declarative_only" for item in workflows["items"])
    assert all("blocking_gates" in item and item["blocking_gates"] for item in workflows["items"])
    assert set(workflows["ownership"]["owned"]) >= OWNER_EXPECTED
    assert set(workflows["ownership"]["handoff"]) >= NON_OWNER_EXPECTED

    assert {item["id"] for item in quality["checks"]} >= QUALITY_CHECK_IDS
    assert quality["minimum_pass"] == "all_blocking_checks"
    assert "boundary_violation" in quality["fail_states"]
    assert set(policy["blocked_by_default"]) >= BLOCKED_BY_DEFAULT
    assert policy["default_mode"] == "draft_and_handoff_only"
    assert "external_connector_action" in policy["requires_human_review_before"]
    assert handoff_policy["default"] == "do_not_execute_adjacent_runtime_action"
    assert handoff_matrix["pack_boundary_rule"] == "owner_surface_wins_then_explicit_handoff"
    assert len(handoff_matrix["items"]) >= 3
    assert ledger["completion_rules"]["every_record_has_evidence"] is True
    assert ledger["completion_rules"]["external_actions_are_handoffs"] is True
    assert checklist["minimum_pass"] == "all_blocking_items"
    assert all(item["blocking"] is True for item in checklist["items"])
    assert "Evidence" in template and "Handoff" in template and "Human review required" in template


def test_examples_fixtures_presets_profile_and_docs_boundaries() -> None:
    examples = [read_yaml(path)["example"] for path in sorted((PACK_DIR / "examples").glob("*.example.yaml"))]
    assert len(examples) >= 4
    assert all(item["expected_result"].endswith("handoff_packet") for item in examples)
    assert all("external_action" in item["must_not"] for item in examples)

    fixture = read_yaml(PACK_DIR / "fixtures/contract_fixture.yaml")["contract_fixture"]
    negative = read_yaml(PACK_DIR / "fixtures/negative_cases.yaml")["negative_cases"]
    assert len(fixture["positive_cases"]) >= 1
    assert len(negative["cases"]) >= 1
    assert {item["id"] for item in negative["cases"]} >= NEGATIVE_CASE_IDS
    assert {"human_approval_explicit", "no_external_action"} <= set(fixture["minimum_expected"])
    assert negative["expected_behavior"] == "block_or_handoff_without_external_action"

    presets = [read_yaml(path)["preset"] for path in sorted((PACK_DIR / "presets").glob("*.preset.yaml"))]
    assert {item["id"] for item in presets} == {"safe_default", "handoff_review", "quality_gate"}
    profile_path = next((PACK_DIR / "profiles").glob("*.profile.yaml"))
    profile = read_yaml(profile_path)["profile"]
    assert profile["pack_id"] == PACK_ID
    assert profile["review_contract"]["external_actions"] == "handoff_only"

    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "Handoff", "evidence", "Does Not Provide"]:
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
    assert '"host_execution": true' not in combined


def _mock_dial_gate(call_task: dict, script: dict, never_call_aliases: set[str]) -> tuple[bool, str]:
    if call_task["phone_target_alias"] in never_call_aliases:
        return False, "never_call_block"
    if call_task["approval_state"] != "approved":
        return False, "approval_not_approved"
    if not call_task.get("pre_call_script_id") or not script.get("pre_call_script_id"):
        return False, "script_missing"
    if call_task["pre_call_script_id"] != script["pre_call_script_id"]:
        return False, "script_mismatch"
    if call_task.get("allowed_intent") not in {
        "appointment_reschedule",
        "customer_callback",
        "information_collection",
        "status_check",
        "support_followup",
    }:
        return False, "disallowed_intent"
    return True, "provider_handoff_packet"


def _redact_transcript(text: str, pii_classes: set[str]) -> str:
    result = text
    if "payment" in pii_classes:
        result = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_PAYMENT]", result)
    if "email" in pii_classes:
        result = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", result)
    if "phone" in pii_classes:
        result = re.sub(r"\+?\d[\d .()\-]{7,}\d", "[REDACTED_PHONE]", result)
    return result


def test_mock_dial_gate_and_redaction_contract() -> None:
    call_task = {
        "call_task_id": "call_001",
        "phone_target_alias": "alias_customer_1",
        "purpose": "reschedule appointment",
        "allowed_intent": "appointment_reschedule",
        "approval_state": "approved",
        "pre_call_script_id": "script_001",
        "never_call_checked": True,
        "human_approval_receipt": {
            "approved_by": "user",
            "approved_at": "2026-06-04T00:00:00Z",
            "approved_number_alias": "alias_customer_1",
            "approved_script_id": "script_001",
        },
    }
    script = {"pre_call_script_id": "script_001"}
    assert _mock_dial_gate(call_task, script, set()) == (True, "provider_handoff_packet")
    pending = dict(call_task, approval_state="pending_human")
    assert _mock_dial_gate(pending, script, set()) == (False, "approval_not_approved")
    assert _mock_dial_gate(call_task, script, {"alias_customer_1"}) == (False, "never_call_block")
    bad_intent = dict(call_task, allowed_intent="payment_collection")
    assert _mock_dial_gate(bad_intent, script, set()) == (False, "disallowed_intent")

    redacted = _redact_transcript("Call me at +1 415-555-1212 or a.user@example.com, card 4242 4242 4242 4242", {"phone", "email", "payment"})
    assert "+1 415" not in redacted
    assert "a.user@example.com" not in redacted
    assert "4242 4242" not in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PAYMENT]" in redacted
