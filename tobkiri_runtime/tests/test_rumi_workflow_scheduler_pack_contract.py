from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_workflow_scheduler_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return loaded


def test_workflow_scheduler_pack_required_docs_and_assets_exist_and_parse() -> None:
    required_paths = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/schedule_contracts.yaml",
        "catalog/workflow_routes.yaml",
        "catalog/delivery_handoffs.yaml",
        "catalog/scheduler_schema.json",
        "policies/retry_policy.yaml",
        "profiles/scheduler_designer.profile.yaml",
        "prompts/scheduler_designer.system.md",
        "prompts/schedule_review.system.md",
        "presets/recurring_followup.preset.yaml",
        "presets/monitor_with_stop_conditions.preset.yaml",
        "presets/release_check_wakeup.preset.yaml",
        "presets/delivery_handoff_digest.preset.yaml",
        "examples/daily_followup.example.yaml",
        "examples/monitor_with_retry.example.yaml",
        "examples/release_wakeup.example.yaml",
    ]

    missing = [path for path in required_paths if not (PACK_DIR / path).is_file()]
    assert missing == []

    for path in PACK_DIR.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    for path in PACK_DIR.rglob("*.yaml"):
        _read_yaml(path)

    ecosystem = _read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_id"] == PACK_ID
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["implements_automations"] is False


def test_workflow_scheduler_setup_metadata_is_discoverable_and_not_all_ok() -> None:
    setup = _read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}

    assert setup["pack_id"] == PACK_ID
    assert setup["target_pack_id"] == PACK_ID
    assert setup["recommended"] is False
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert setup["conflicts_with"] == []
    assert setup["overlap_policy"]["defaultspack_scheduler"] == "route_hint_only_do_not_override"
    assert setup["overlap_policy"]["app_automation_tool"] == "delegate_execution_when_available"
    assert setup["base_pack_promotion"]["eligible"] is False

    candidate = candidates[PACK_ID]
    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.all_ok_eligible is False
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.marketplace["id"] == "rumi.workflow_scheduler_pack"
    assert candidate.base_pack_promotion["eligible"] is False

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_workflow_scheduler_catalogs_cover_scheduler_contracts() -> None:
    schedules = _read_yaml(PACK_DIR / "catalog" / "schedule_contracts.yaml")
    routes = _read_yaml(PACK_DIR / "catalog" / "workflow_routes.yaml")
    handoffs = _read_yaml(PACK_DIR / "catalog" / "delivery_handoffs.yaml")
    retry = _read_yaml(PACK_DIR / "policies" / "retry_policy.yaml")
    profile = _read_yaml(PACK_DIR / "profiles" / "scheduler_designer.profile.yaml")

    kinds = {item["kind_id"] for item in schedules["schedule_kinds"]}
    assert {
        "cron_like",
        "interval",
        "one_shot_wakeup",
        "recurring_followup",
        "monitor",
    } <= kinds
    assert schedules["default_posture"]["implements_automations"] is False
    assert schedules["default_posture"]["network_default"] == "deny"
    assert "stop_conditions" in schedules["required_contract_fields"]

    route_ids = {route["route_id"] for route in routes["routes"]}
    assert {
        "app_automation_tool",
        "defaultspack_scheduler",
        "rumi_agent_services_pack",
        "rumi_connector_gateway_pack",
        "rumi_devops_release_pack",
    } <= route_ids
    assert routes["conflict_resolution"]["defaultspack"]["rule"] == "do_not_override"

    handoff_ids = {handoff["handoff_id"] for handoff in handoffs["handoff_types"]}
    assert {"local_thread_wakeup", "chat_digest", "connector_message", "release_check"} <= handoff_ids
    assert handoffs["default_posture"]["delivery_execution"] == "owner_pack_only"

    assert retry["stop_conditions"]["required"] is True
    assert retry["escalation"]["never_send_external_message_without_owner"] is True

    assert profile["policy"]["local_first"] is True
    assert profile["policy"]["network_default"] == "deny"
    assert profile["policy"]["implements_automations"] is False
    assert profile["policy"]["require_owner_route"] is True


def test_workflow_scheduler_pack_has_no_secrets_and_docs_explain_overlap() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in [
            "README.md",
            "docs/architecture.md",
            "docs/interfaces.md",
            "docs/operations.md",
            "prompts/scheduler_designer.system.md",
            "prompts/schedule_review.system.md",
        ]
    )

    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "app automation",
        "rumi_agent_services_pack",
        "rumi_connector_gateway_pack",
        "rumi_devops_release_pack",
        "does not implement",
        "No executable",
        "network",
        "stop conditions",
    ]:
        assert expected in docs

    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    )
    checked_files = [
        path
        for root in (PACK_DIR, SETUP_PACK_JSON.parent)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".yaml", ".md"}
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in checked_files
        if secret_assignment.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
