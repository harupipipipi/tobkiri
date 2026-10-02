from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_security_review_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return loaded


def test_security_review_pack_required_docs_and_assets_exist_and_parse() -> None:
    required_paths = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/review_controls.yaml",
        "catalog/risk_taxonomy.yaml",
        "catalog/finding_schema.json",
        "profiles/security_reviewer.profile.yaml",
        "prompts/security_reviewer.system.md",
        "prompts/release_signoff.system.md",
        "presets/threat_model_review.preset.yaml",
        "presets/permission_grant_review.preset.yaml",
        "presets/mcp_browser_risk_review.preset.yaml",
        "presets/dependency_release_signoff.preset.yaml",
        "examples/pack_security_review.example.yaml",
        "examples/release_signoff.example.yaml",
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
    assert ecosystem["required_network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["grant_override"] is False


def test_security_review_pack_setup_metadata_is_discoverable_and_review_only() -> None:
    setup = _read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}

    assert setup["pack_id"] == PACK_ID
    assert setup["target_pack_id"] == PACK_ID
    assert setup["recommended"] is False
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "low"
    assert setup["conflicts_with"] == []
    assert setup["overlap_policy"]["defaultspack_grants"] == "review_only_do_not_override"
    assert setup["overlap_policy"]["approval_decisions"] == "defer_to_runtime_and_owner_pack"
    assert setup["base_pack_promotion"]["eligible"] is False

    candidate = candidates[PACK_ID]
    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.all_ok_eligible is False
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.marketplace["id"] == "rumi.security_review_pack"
    assert candidate.base_pack_promotion["eligible"] is False

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_security_review_pack_catalogs_cover_required_review_domains() -> None:
    controls = _read_yaml(PACK_DIR / "catalog" / "review_controls.yaml")
    taxonomy = _read_yaml(PACK_DIR / "catalog" / "risk_taxonomy.yaml")
    profile = _read_yaml(PACK_DIR / "profiles" / "security_reviewer.profile.yaml")

    families = {family["family_id"] for family in controls["control_families"]}
    assert {
        "threat_modeling",
        "secret_scanning_review",
        "permission_grant_review",
        "mcp_browser_risk",
        "dependency_review",
        "release_security_signoff",
    } <= families
    assert controls["default_posture"]["network_default"] == "deny"
    assert controls["default_posture"]["override_grants"] is False

    categories = {category["category_id"] for category in taxonomy["risk_categories"]}
    assert {
        "secrets",
        "permissions_and_grants",
        "mcp_connectors",
        "browser_automation",
        "dependencies",
        "privacy",
        "release_signoff",
    } <= categories

    assert profile["policy"]["local_first"] is True
    assert profile["policy"]["network_default"] == "deny"
    assert profile["policy"]["grant_override_allowed"] is False
    assert profile["policy"]["approval_decision_owner"] == "defaultspack"


def test_security_review_pack_has_no_secrets_and_docs_explain_overlap() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in [
            "README.md",
            "docs/interfaces.md",
            "docs/operations.md",
            "prompts/security_reviewer.system.md",
            "prompts/release_signoff.system.md",
        ]
    )

    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "grant",
        "approval",
        "MCP",
        "browser",
        "does not override",
        "No executable",
        "network",
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
