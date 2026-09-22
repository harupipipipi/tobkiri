from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_mcp_gateway_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_mcp_gateway_pack_required_docs_and_assets_exist() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/connector_catalog.yaml",
        "catalog/namespace_routes.yaml",
        "catalog/marketplace_registry.yaml",
        "policies/unsupported_server_safety.yaml",
        "profiles/mcp_gateway.profile.yaml",
        "prompts/mcp_gateway_router.system.md",
        "templates/prompts/unknown_server_triage.prompt.template.md",
        "templates/resources/mcp_server_card.resource.template.yaml",
        "examples/register_unknown_server.example.yaml",
    ]

    missing = [path for path in required if not (PACK_DIR / path).is_file()]
    assert missing == []

    ecosystem = _read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_id"] == PACK_ID
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_connector_code"] is True


def test_mcp_gateway_setup_pack_is_discoverable_and_not_all_ok() -> None:
    setup = _read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}

    assert setup["pack_id"] == PACK_ID
    assert setup["target_pack_id"] == PACK_ID
    assert setup["recommended"] is False
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "critical"
    assert setup["overlap_policy"]["defaultspack_tool_mcp"] == "do_not_override"
    assert setup["base_pack_promotion"]["eligible"] is False

    candidate = candidates[PACK_ID]
    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.all_ok_eligible is False
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.marketplace["id"] == "rumi.mcp_gateway_pack"

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_mcp_gateway_yaml_policy_routes_unknown_servers_safely() -> None:
    catalog = _read_yaml(PACK_DIR / "catalog" / "connector_catalog.yaml")
    routes = _read_yaml(PACK_DIR / "catalog" / "namespace_routes.yaml")
    safety = _read_yaml(PACK_DIR / "policies" / "unsupported_server_safety.yaml")
    profile = _read_yaml(PACK_DIR / "profiles" / "mcp_gateway.profile.yaml")

    assert catalog["default_posture"]["executable_connector_code"] is False
    assert catalog["default_posture"]["network_default"] == "deny"
    assert catalog["default_posture"]["connection_owner"] == "defaultspack.tool.mcp_connect"
    assert {item["category_id"] for item in catalog["categories"]} >= {
        "filesystem",
        "source_control",
        "browser_web",
        "productivity",
        "database",
        "ai_tooling",
    }

    assert routes["explicit_namespace_required"] is True
    assert routes["unknown_server_namespace"]["pattern"] == "mcp_gateway.<server_slug>"
    assert routes["tool_name_routes"]["discovered_defaultspack_tool"]["pattern"] == "mcp__<server_id>__<tool_name>"
    assert routes["conflict_resolution"]["defaultspack_mcp_interfaces"]["rule"] == "do_not_override"

    assert safety["default_decision"] == "deny"
    assert safety["server_classes"]["unknown"]["connect_requires_approval"] is True
    assert safety["approval_requirements"]["connection"]["owner"] == "defaultspack"
    assert safety["approval_requirements"]["execution"]["client_supplied_approved_flags_trusted"] is False

    assert profile["policy"]["allow_executable_connector_code"] is False
    assert profile["tool_routing"]["connect_interface"] == "defaultspack.tool.mcp_connect"


def test_mcp_gateway_docs_explain_defaultspack_overlap_and_no_secrets() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in [
            "README.md",
            "docs/interfaces.md",
            "docs/operations.md",
            "prompts/mcp_gateway_router.system.md",
            "templates/prompts/unknown_server_triage.prompt.template.md",
        ]
    )

    for expected in [
        "defaults.tool.mcp_connect",
        "defaultspack.tool.mcp_connect",
        "defaults.tool.mcp_list",
        "defaultspack.tool.mcp_list",
        "explicit namespace",
        "approval",
        "Required Secrets",
        "None",
        "No standalone MCP client",
        "consumer-bound",
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
    offenders = [str(path.relative_to(ROOT)) for path in checked_files if secret_assignment.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
