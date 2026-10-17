from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_connector_gateway_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_connector_gateway_required_assets_and_metadata() -> None:
    required = [
        "README.md", "docs/README.md", "docs/architecture.md", "docs/interfaces.md", "docs/operations.md", "ecosystem.json",
        "catalog/connectors.gateway.yaml", "catalog/handoff_schema.gateway.yaml", "policies/connector_scope_policy.yaml",
        "profiles/connector_gateway.profile.yaml", "prompts/connector_gateway.system.md",
        "presets/slack_gmail_drive_gateway.preset.yaml", "presets/github_issue_pr_gateway.preset.yaml",
        "examples/slack_to_workspace.example.yaml", "examples/gmail_followup.example.yaml", "examples/github_pr_triage.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    available = {item.pack_id for item in PackSelector(ROOT / "ecosystem").scan_candidates()}
    assert {item["pack_id"] for item in ecosystem["metadata"]["optional_integrations"]} <= available
    assert "rumi_local_agent_pack" not in {
        item["pack_id"] for item in ecosystem["metadata"]["optional_integrations"]
    }


def test_connector_gateway_yaml_parses_and_routes_to_owners() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    policy = yaml.safe_load((PACK_DIR / "policies" / "connector_scope_policy.yaml").read_text(encoding="utf-8"))
    decisions = {rule["id"]: rule["decision"] for rule in policy["rules"]}
    assert decisions["unsupported_server_routes_to_mcp_gateway"] == "handoff_to_defaultspack_mcp_registry"
    assert decisions["recurring_connector_task_needs_schedule_owner"] == "handoff_to_defaultspack_scheduler"


def test_connector_gateway_setup_pack_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["connector_execution"] == "do_not_override_installed_connector_tools"
    assert candidate.overlap_policy["mcp_gateway"] == "mcp_servers_use_defaultspack_mcp_registry"
    assert candidate.overlap_policy["workflow_delivery"] == "handoff_schedules_to_defaultspack_scheduler"
    assert candidate.base_pack_promotion["eligible"] is False


def test_connector_gateway_docs_no_secrets_and_boundary_notes() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md"]
    )
    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "tobkiri.profile-content.local-agent.v1",
        "not an installable Pack",
        "scheduler",
        "untrusted",
    ]:
        assert expected in docs
    example = yaml.safe_load(
        (PACK_DIR / "examples" / "github_pr_triage.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert (
        example["expected_handoff"]["delivery_target"]
        == "tobkiri.profile-content.local-agent.v1"
    )
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
