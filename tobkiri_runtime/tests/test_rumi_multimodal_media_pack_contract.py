from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_multimodal_media_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_multimodal_media_pack_required_docs_assets_and_metadata() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/media_capabilities.yaml",
        "catalog/artifact_schema.media.yaml",
        "catalog/workflows.media.yaml",
        "policies/media_rights_privacy.yaml",
        "profiles/multimodal_media_agent.profile.yaml",
        "profiles/visual_qa_agent.profile.yaml",
        "prompts/multimodal_media.system.md",
        "prompts/visual_qa.system.md",
        "presets/screenshot_qa.preset.yaml",
        "presets/image_asset_brief.preset.yaml",
        "presets/ocr_notes.preset.yaml",
        "examples/screenshot_review.example.yaml",
        "examples/image_brief.example.yaml",
        "examples/ocr_notes.example.yaml",
    ]
    missing = [path for path in required if not (PACK_DIR / path).is_file()]
    assert missing == []

    ecosystem = _read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_id"] == PACK_ID
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False


def test_multimodal_media_yaml_files_parse_and_require_asset_ledger() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path

    schema = yaml.safe_load((PACK_DIR / "catalog" / "artifact_schema.media.yaml").read_text(encoding="utf-8"))
    assert schema["schema_id"] == "rumi_multimodal_media.asset_ledger"
    assert "rights_context" in schema["required_fields"]
    assert "privacy_context" in schema["required_fields"]

    policy = yaml.safe_load((PACK_DIR / "policies" / "media_rights_privacy.yaml").read_text(encoding="utf-8"))
    assert any(rule["id"] == "handoff_requires_ledger" for rule in policy["rules"])


def test_multimodal_media_setup_pack_is_discoverable_and_boundary_scoped() -> None:
    setup = _read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]

    assert setup["target_pack_id"] == PACK_ID
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["workspace_artifacts"] == "handoff_final_exports_to_rumi_workspace_pack"
    assert candidate.overlap_policy["generated_media"] == "require_asset_ledger_before_delivery"
    assert candidate.base_pack_promotion["eligible"] is False


def test_multimodal_media_docs_have_overlap_notes_and_no_secret_literals() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md", "prompts/multimodal_media.system.md"]
    )
    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "rumi_workspace_pack",
        "asset ledger",
        "privacy",
    ]:
        assert expected in docs

    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [path for path in PACK_DIR.rglob("*") if path.is_file()]
    checked.append(SETUP_PACK_JSON)
    offenders = [str(path.relative_to(ROOT)) for path in checked if secret_assignment.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
