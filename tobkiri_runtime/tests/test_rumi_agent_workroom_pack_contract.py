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
PACK_ID = 'rumi_agent_workroom_pack'
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"
DEFAULTSPACK_RUN_STATUS_MODEL = ROOT / "ecosystem" / "defaultspack" / "domain" / "agent_runtime" / "models.py"
REQUIRED_ASSETS = ['README.md', 'asset_index.json', 'asset_index.yaml', 'catalog/handoff_matrix.yaml', 'catalog/quality_matrix.yaml', 'catalog/taxonomy.yaml', 'catalog/workflows.yaml', 'checklists/review.checklist.yaml', 'docs/README.md', 'docs/architecture.md', 'docs/compatibility.md', 'docs/interfaces.md', 'docs/operations.md', 'examples/checkpoint_resume.example.yaml', 'examples/redirect_running_plan.example.yaml', 'examples/replay_divergence.example.yaml', 'examples/run_board_review.example.yaml', 'fixtures/contract_fixture.yaml', 'fixtures/negative_cases.yaml', 'frontend_extensions/run_board.ui.json', 'ledgers/evidence_ledger.schema.yaml', 'policies/handoff.policy.yaml', 'policies/safety.policy.yaml', 'presets/handoff_review.preset.yaml', 'presets/quality_gate.preset.yaml', 'presets/safe_default.preset.yaml', 'profiles/workroom_operator.profile.yaml', 'prompts/workroom_orchestrator.system.md', 'schemas/agent_run.schema.json', 'schemas/checkpoint.schema.json', 'schemas/control_request.schema.json', 'schemas/handoff_envelope.schema.json', 'schemas/intervention.schema.json', 'schemas/progress_event.schema.json', 'schemas/replay_index.schema.json', 'schemas/run_board_view.schema.json', 'schemas/run_event.schema.json', 'schemas/task_plan.schema.json', 'schemas/workroom_session.schema.json', 'templates/handoff.template.md', 'templates/review_report.template.md', 'templates/ui_contract.template.md']
SCHEMA_EXPECTATIONS = {'schemas/agent_run.schema.json': ['run_id', 'workroom_id', 'session_key', 'parent_run_id', 'root_run_id', 'owner_pack_id', 'executor_ref', 'task_plan_id', 'transcript_id', 'current_transcript_id', 'checkpoint_refs', 'replay_index_refs', 'status', 'redaction_state'], 'schemas/checkpoint.schema.json': ['checkpoint_id', 'session_id', 'event_sequence', 'state_digest', 'resume_policy', 'sealed'], 'schemas/control_request.schema.json': ['control_request_id', 'run_id', 'kind', 'idempotency_key', 'idempotency_scope', 'duplicate_policy', 'precondition_status', 'requested_by', 'requested_at', 'reason', 'state', 'audit_ref'], 'schemas/handoff_envelope.schema.json': ['handoff_id', 'source_pack_id', 'owner_pack_id', 'target_pack_id', 'target_capability', 'surface', 'reason', 'request_ref', 'payload_ref', 'payload_schema_ref', 'evidence_refs', 'approval_required', 'handoff_state', 'audit_ref'], 'schemas/intervention.schema.json': ['intervention_id', 'session_id', 'kind', 'target_sequence', 'instruction', 'approval_receipt', 'effect'], 'schemas/progress_event.schema.json': ['progress_id', 'session_id', 'step_id', 'state', 'summary', 'evidence_ids', 'next_safe_action'], 'schemas/replay_index.schema.json': ['replay_id', 'session_id', 'event_ids', 'checkpoint_ids', 'determinism_state', 'divergence_notes'], 'schemas/run_board_view.schema.json': ['view_id', 'session_id', 'lanes', 'visible_event_types', 'approval_actions', 'forbidden_runtime_actions', 'actions_effect', 'readonly'], 'schemas/run_event.schema.json': ['event_id', 'session_id', 'sequence', 'event_type', 'payload', 'created_at', 'source'], 'schemas/task_plan.schema.json': ['plan_id', 'session_id', 'steps', 'revision', 'approval_state', 'source_event_ids'], 'schemas/workroom_session.schema.json': ['session_id', 'goal_id', 'run_owner', 'status', 'event_log_id', 'human_intervention_policy']}
WORKFLOW_IDS = set(['workroom_initialization', 'task_plan_progression', 'checkpoint_resume', 'interrupt_redirect_cancel', 'deterministic_replay', 'run_board_review_packet'])
QUALITY_CHECK_IDS = set(['append_only_event_log', 'checkpoint_resume_review', 'intervention_future_only', 'no_tool_execution', 'deterministic_replay', 'run_board_readability', 'handoff_owner_named', 'defaultspack_not_promoted'])
OWNER_EXPECTED = set(['agent_workroom_session', 'run_event_log', 'task_plan_contract', 'progress_event_contract', 'checkpoint_resume_contract', 'interrupt_redirect_cancel_contract', 'deterministic_replay_index', 'run_board_ui_contract'])
NON_OWNER_EXPECTED = set(['tool execution', 'browser action', 'desktop action', 'schedule execution', 'file persistence', 'metrics collection', 'subagent PR management', 'model routing'])
OVERLAP_EXPECTED = {'tool_execution': 'handoff_to_rumi_default_tools_pack', 'agent_service_choreography': 'handoff_to_rumi_operations_company_pack', 'browser_action': 'handoff_to_rumi_default_tools_pack', 'desktop_action': 'handoff_to_rumi_default_tools_pack', 'schedule_execution': 'handoff_to_defaultspack', 'file_persistence': 'handoff_to_defaultspack', 'metrics_collection': 'handoff_to_defaultspack', 'subagent_pr_management': 'handoff_to_rumi_operations_company_pack', 'model_routing': 'handoff_to_rumi_model_catalog_pack', 'workroom_state_contract': 'owned_by_rumi_agent_workroom_pack', 'replay_index': 'owned_by_rumi_agent_workroom_pack', 'tool_aliases': 'prefer_explicit_pack_namespace'}
PROMOTION_BLOCKERS = set(['no_durable_run_event_bus', 'no_signed_interrupt_tokens', 'tool_execution_owned_elsewhere', 'metrics_owned_by_defaultspack', 'file_persistence_owned_by_defaultspack'])
PROMOTION_EVIDENCE = set(['deterministic_replay_cases', 'checkpoint_resume_cases', 'interrupt_redirect_cancel_cases', 'run_board_review_cases', 'handoff_acceptance_by_defaultspack', 'handoff_acceptance_by_operations_company_default_tools', 'handoff_acceptance_by_default_tools_pack'])
BLOCKED_BY_DEFAULT = set(['execute tools from a workroom event', 'mutate browser or desktop state', 'schedule future work directly', 'write run files directly', 'rewrite replay history after checkpoint'])
AGENT_RUN_STORE_FIELDS = set(['run_id', 'execution_id', 'session_key', 'conversation_id', 'agent_id', 'task', 'status', 'model', 'system_prompt_id', 'system_prompt_hash', 'runtime_profile_key', 'runtime_profile_json', 'capability_graph_json', 'created_at', 'updated_at', 'started_at', 'completed_at', 'parent_run_id', 'root_run_id', 'current_transcript_id', 'compaction_count', 'heartbeat_at', 'error', 'result_json', 'execution_json'])
HANDOFF_TARGETS = set(['defaultspack', 'rumi_default_tools_pack', 'rumi_operations_company_pack', 'rumi_model_catalog_pack'])
RUN_BOARD_FORBIDDEN_ACTIONS = set(['run_terminal', 'execute_tool', 'invoke_tool', 'call_tool', 'open_browser', 'browser_action', 'desktop_action', 'restore_files', 'write_files', 'delete_files', 'create_schedule', 'schedule_run', 'collect_metrics', 'export_metrics', 'push_pr', 'merge_pr', 'create_subagent_pr', 'call_api_route', '/api'])
RUN_BOARD_REQUEST_ACTIONS = set(['request_interrupt', 'request_pause', 'request_resume', 'request_redirect', 'request_cancel', 'open_handoff_packet', 'approve_handoff_request', 'request_human_review', 'copy_review_summary'])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_defaultspack_run_status_values() -> set[str]:
    text = DEFAULTSPACK_RUN_STATUS_MODEL.read_text(encoding="utf-8")
    enum_body = text.split("class RunStatus", 1)[1].split("def json_dumps", 1)[0]
    return set(re.findall(r'=\s*"([^"]+)"', enum_body))


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
    assert set(metadata["owner_surfaces"]) >= OWNER_EXPECTED
    assert set(metadata["non_owner_surfaces"]) >= NON_OWNER_EXPECTED
    available = available_setup_pack_ids()
    assert HANDOFF_TARGETS <= available
    optional_integrations = {item["pack_id"]: item["reason"] for item in metadata["optional_integrations"]}
    assert HANDOFF_TARGETS <= set(optional_integrations)
    assert "tool execution" in optional_integrations["rumi_default_tools_pack"]
    assert "metrics" in optional_integrations["defaultspack"]
    assert "choreography" in optional_integrations["rumi_operations_company_pack"]
    actual = {
        str(path.relative_to(PACK_DIR))
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
    assert set(candidate.defaultspack_promotion["promotion_evidence_required"]) >= PROMOTION_EVIDENCE
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == 'agent-workroom'
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
        if key in {"workroom_state_contract", "replay_index", "tool_aliases"}:
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
    actual_secret_patterns = [
        r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{12,}['\"]",
        r"ghp_[A-Za-z0-9]{20,}",
        r"xox[baprs]-[A-Za-z0-9-]{20,}",
        r"AIza[0-9A-Za-z_-]{20,}",
        r"ya29\.[0-9A-Za-z_-]+",
    ]
    for pattern in actual_secret_patterns:
        assert re.search(pattern, combined) is None


def _reduce_workroom_events(events: list[dict]) -> dict:
    state = {"status": "draft", "steps": {}, "cancelled_after": None, "redirects": []}
    for event in sorted(events, key=lambda item: item["sequence"]):
        if state["cancelled_after"] is not None and event["sequence"] > state["cancelled_after"]:
            continue
        if event["event_type"] == "plan_created":
            state["status"] = "active"
            for step in event["payload"].get("steps", []):
                state["steps"][step] = "todo"
        elif event["event_type"] == "progress":
            state["steps"][event["payload"]["step_id"]] = event["payload"]["state"]
        elif event["event_type"] == "redirect":
            state["redirects"].append(event["payload"]["summary"])
        elif event["event_type"] == "cancel":
            state["status"] = "cancelled"
            state["cancelled_after"] = event["sequence"]
    return state


def test_agent_workroom_replay_redirect_and_cancel_contract() -> None:
    events = [
        {"sequence": 0, "event_type": "plan_created", "payload": {"steps": ["inspect", "patch"]}},
        {"sequence": 1, "event_type": "progress", "payload": {"step_id": "inspect", "state": "done"}},
        {"sequence": 2, "event_type": "redirect", "payload": {"summary": "replace future patch with review"}},
        {"sequence": 3, "event_type": "cancel", "payload": {"summary": "stop future actions"}},
        {"sequence": 4, "event_type": "progress", "payload": {"step_id": "patch", "state": "done"}},
    ]
    assert _reduce_workroom_events(events) == _reduce_workroom_events(list(reversed(events)))
    state = _reduce_workroom_events(events)
    assert state["status"] == "cancelled"
    assert state["steps"]["inspect"] == "done"
    assert state["steps"]["patch"] == "todo"
    assert state["redirects"] == ["replace future patch with review"]


def test_agent_run_store_compatibility_contract() -> None:
    run_schema = read_json(PACK_DIR / "schemas/agent_run.schema.json")
    run_status_values = read_defaultspack_run_status_values()
    assert "AgentRunStore" in run_schema["description"]
    assert "second run database" in run_schema["description"]
    assert AGENT_RUN_STORE_FIELDS <= set(run_schema["properties"])
    assert set(run_schema["properties"]["status"]["enum"]) == run_status_values
    assert "waiting" not in run_schema["properties"]["status"]["enum"]
    assert "succeeded" not in run_schema["properties"]["status"]["enum"]
    assert run_schema["properties"]["agent_run_store_ref"]["const"] == "defaultspack.domain.agent_runtime.AgentRunStore"
    assert {"run_id", "session_key", "parent_run_id", "root_run_id", "current_transcript_id"} <= set(run_schema["required"])


def test_control_requests_are_idempotent_contract() -> None:
    control_schema = read_json(PACK_DIR / "schemas/control_request.schema.json")
    assert {"interrupt", "redirect", "cancel", "pause", "resume"} <= set(control_schema["properties"]["kind"]["enum"])
    assert {"idempotency_key", "idempotency_scope", "duplicate_policy", "requested_at"} <= set(control_schema["required"])
    assert control_schema["properties"]["idempotency_key"]["minLength"] >= 8
    assert control_schema["properties"]["idempotency_scope"]["const"] == "run_id_kind_requested_by"
    assert control_schema["properties"]["duplicate_policy"]["const"] == "return_existing_control_request"
    assert {"running", "waiting_approval", "waiting_user_input", "paused", "resumable"} <= set(control_schema["properties"]["precondition_status"]["items"]["enum"])


def test_handoff_envelope_targets_adjacent_owners() -> None:
    handoff_schema = read_json(PACK_DIR / "schemas/handoff_envelope.schema.json")
    assert handoff_schema["properties"]["source_pack_id"]["const"] == PACK_ID
    assert HANDOFF_TARGETS <= set(handoff_schema["properties"]["target_pack_id"]["enum"])
    assert {"target_pack_id", "target_capability", "request_ref", "payload_schema_ref", "evidence_refs", "handoff_state"} <= set(handoff_schema["required"])
    assert handoff_schema["properties"]["approval_required"]["const"] is True
    assert handoff_schema["properties"]["evidence_refs"]["minItems"] >= 1
    assert "tool_execution" in handoff_schema["properties"]["surface"]["enum"]
    assert "agent_service_choreography" in handoff_schema["properties"]["surface"]["enum"]


def test_run_board_ui_remains_request_only() -> None:
    view_schema = read_json(PACK_DIR / "schemas/run_board_view.schema.json")
    ui = read_json(PACK_DIR / "frontend_extensions/run_board.ui.json")["ui_contract"]
    assert view_schema["properties"]["readonly"]["const"] is True
    assert view_schema["properties"]["actions_effect"]["const"] == "control_request_or_handoff_packet_only"
    assert RUN_BOARD_REQUEST_ACTIONS >= set(ui["actions"])
    assert set(ui["forbidden_actions"]) >= RUN_BOARD_FORBIDDEN_ACTIONS
    assert set(view_schema["properties"]["forbidden_runtime_actions"]["items"]["enum"]) >= RUN_BOARD_FORBIDDEN_ACTIONS
    assert ui["readonly_by_default"] is True
    assert ui["actions_effect"] == "control_request_or_handoff_packet_only"
    assert not any(action in RUN_BOARD_FORBIDDEN_ACTIONS for action in ui["actions"])
    assert not any("/api" in action for action in ui["actions"])


def test_no_runtime_api_or_tool_execution_ownership() -> None:
    runtime_owner_dirs = {"api", "backend", "blocks", "domain", "functions", "tools", "transport"}
    assert {path.name for path in PACK_DIR.iterdir() if path.is_dir()} & runtime_owner_dirs == set()
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    setup = read_json(SETUP_PACK_JSON)
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []
    assert ecosystem["host_execution"] is False
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["declarative_only"] is True
    assert setup["overlap_policy"]["tool_execution"] == "handoff_to_rumi_default_tools_pack"
    assert setup["overlap_policy"]["agent_service_choreography"] == "handoff_to_rumi_operations_company_pack"
    for forbidden_key in ["api_routes", "routes", "tools", "functions", "blocks"]:
        assert forbidden_key not in ecosystem
        assert forbidden_key not in ecosystem["metadata"]
    assert "tool execution" in ecosystem["metadata"]["non_owner_surfaces"]
    assert "tool_execution" not in ecosystem["metadata"]["owner_surfaces"]


def test_agent_workroom_subagent_acceptance_assets() -> None:
    run_schema = read_json(PACK_DIR / "schemas/agent_run.schema.json")
    control_schema = read_json(PACK_DIR / "schemas/control_request.schema.json")
    handoff_schema = read_json(PACK_DIR / "schemas/handoff_envelope.schema.json")
    ui = read_json(PACK_DIR / "frontend_extensions/run_board.ui.json")["ui_contract"]
    assert {"run_id", "workroom_id", "parent_run_id", "root_run_id", "owner_pack_id", "executor_ref", "task_plan_id", "transcript_id", "checkpoint_refs", "replay_index_refs", "redaction_state"} <= set(run_schema["required"])
    assert {"interrupt", "redirect", "cancel", "pause", "resume"} <= set(control_schema["properties"]["kind"]["enum"])
    assert {"idempotency_key", "idempotency_scope", "duplicate_policy"} <= set(control_schema["required"])
    assert handoff_schema["properties"]["approval_required"]["const"] is True
    assert HANDOFF_TARGETS <= set(handoff_schema["properties"]["target_pack_id"]["enum"])
    assert ui["readonly_by_default"] is True
    assert RUN_BOARD_FORBIDDEN_ACTIONS <= set(ui["forbidden_actions"])
    setup = read_json(SETUP_PACK_JSON)
    assert "must_prove_agent_run_store_compatibility" in setup["defaultspack_promotion"]["promotion_blockers"]
