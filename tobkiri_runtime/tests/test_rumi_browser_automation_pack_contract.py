from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_browser_automation_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_browser_automation_pack_required_docs_assets_and_metadata() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/actions.browser_automation.yaml",
        "catalog/playbooks.browser_automation.yaml",
        "policies/browser_safety.yaml",
        "profiles/browser_automation_operator.profile.yaml",
        "prompts/browser_automation.system.md",
        "presets/browser_use_like_task.preset.yaml",
        "presets/authenticated_browser_task.preset.yaml",
        "presets/visual_regression_browser.preset.yaml",
        "examples/form_fill.example.yaml",
        "examples/web_qa.example.yaml",
        "examples/data_collection.example.yaml",
    ]
    missing = [path for path in required if not (PACK_DIR / path).is_file()]
    assert missing == []

    ecosystem = _read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_id"] == PACK_ID
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False


def test_browser_automation_yaml_files_parse_and_policy_routes_execution() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path

    safety = yaml.safe_load((PACK_DIR / "policies" / "browser_safety.yaml").read_text(encoding="utf-8"))
    assert safety["execution_owner"] == "rumi_default_tools_pack"
    assert safety["semantic_owner"] == "rumi_browser_element_pack"
    assert any(rule["id"] == "observe_before_act" for rule in safety["rules"])


def test_browser_automation_setup_pack_is_discoverable_and_boundary_scoped() -> None:
    setup = _read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]

    assert setup["target_pack_id"] == PACK_ID
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [
        {"pack_id": "defaultspack", "version": ">=2.0.0"},
        {"pack_id": "rumi_default_tools_pack", "version": ">=1.0.0"},
    ]
    assert candidate.overlap_policy["browser_execution"] == "do_not_override_rumi_default_tools_pack"
    assert candidate.overlap_policy["semantic_dom"] == "prefer_rumi_browser_element_pack_when_installed"
    assert candidate.base_pack_promotion["eligible"] is False


def test_browser_automation_docs_have_overlap_notes_and_no_secret_literals() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md", "prompts/browser_automation.system.md"]
    )
    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "rumi_default_tools_pack",
        "rumi_browser_element_pack",
        "evidence ledger",
    ]:
        assert expected in docs

    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()]
    checked.append(SETUP_PACK_JSON)
    offenders = [str(path.relative_to(ROOT)) for path in checked if secret_assignment.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
