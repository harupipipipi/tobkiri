from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from core_runtime.setup_pack import SetupPackManager
from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_voice_mobile_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _asset_index_paths(ecosystem: dict) -> set[str]:
    index = ecosystem["metadata"]["asset_index"]
    result: set[str] = set()
    for value in index.values():
        result.update(value)
    return result


def _meaningful_pack_assets() -> set[str]:
    return {
        path.relative_to(PACK_DIR).as_posix()
        for path in PACK_DIR.rglob("*")
        if path.is_file()
        and path.name not in {"ecosystem.json", "executables.v4.json"}
    }


def test_pack_required_assets_metadata_and_schema_validity() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/voice_mobile_workflows.yaml",
        "policies/voice_mobile_safety.policy.yaml",
        "policies/transcription_notification_consent.policy.yaml",
        "specs/intent_taxonomy.yaml",
        "specs/handoff_receipt.schema.yaml",
        "checklists/mobile_action_safety_checklist.yaml",
        "templates/mobile_handoff_receipt.template.yaml",
        "profiles/voice_mobile_operator.profile.yaml",
        "prompts/voice_mobile_operator.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/voice_memo_task.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []

    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert ecosystem["vocabulary"]["types"]
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert ecosystem["metadata"]["registers_tools"] is False
    assert _asset_index_paths(ecosystem) == (
        _meaningful_pack_assets() - V4_AUTHORITY_ARTIFACTS
    )


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_validates_dependencies() -> None:
    setup = read_json(SETUP_PACK_JSON)
    selector = PackSelector(ROOT / "ecosystem")
    candidate = {item.pack_id: item for item in selector.scan_candidates()}[PACK_ID]

    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == []
    assert candidate.overlap_policy["connector_delivery"] == "handoff_to_rumi_connector_gateway_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert "Voice Mobile" in candidate.defaultspack_promotion["reason"]
    assert "no_voice_capture_runtime" in candidate.defaultspack_promotion["promotion_blockers"]
    assert "handoff_receipt_schema_cases" in candidate.defaultspack_promotion["promotion_evidence_required"]
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "voice-mobile"
    assert candidate.signing["verified"] is True

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="macos",
        python_version="3.13.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_setup_pack_manager_installs_pack_without_selection_dependencies(tmp_path: Path) -> None:
    manager = SetupPackManager(
        root=ROOT / "ecosystem" / "setup_pack",
        selection_file=tmp_path / "setup_pack_selection.json",
        ecosystem_dir=ROOT / "ecosystem",
    )
    result = manager.install(PACK_ID)
    assert result["success"] is True
    assert result["installed_setup_pack_ids"] == [PACK_ID]
    assert result["installed_target_pack_ids"] == [PACK_ID]
    assert result["active_setup_pack_id"] == PACK_ID
    assert result["active_target_pack_id"] == PACK_ID
    assert result["skipped_all_ok_setup_pack_ids"] == [PACK_ID]


def test_voice_mobile_assets_have_real_semantics() -> None:
    taxonomy = yaml.safe_load((PACK_DIR / "specs/intent_taxonomy.yaml").read_text(encoding="utf-8"))
    consent = yaml.safe_load((PACK_DIR / "policies/transcription_notification_consent.policy.yaml").read_text(encoding="utf-8"))
    checklist = yaml.safe_load((PACK_DIR / "checklists/mobile_action_safety_checklist.yaml").read_text(encoding="utf-8"))
    receipt = yaml.safe_load((PACK_DIR / "specs/handoff_receipt.schema.yaml").read_text(encoding="utf-8"))
    template = yaml.safe_load((PACK_DIR / "templates/mobile_handoff_receipt.template.yaml").read_text(encoding="utf-8"))

    assert {"voice_memo_to_task", "notification_brief", "scheduled_briefing", "speech_device_action"} <= set(taxonomy["intent_classes"])
    assert taxonomy["intent_classes"]["speech_device_action"]["risk"] == "high"
    assert "transcript_required_for_voice_commands" in [rule["id"] for rule in taxonomy["classification_rules"]]
    assert consent["consent_surfaces"]["transcription"]["required"] is True
    assert consent["consent_surfaces"]["notification_delivery"]["evidence"] == ["channel", "recipient", "delivery_window", "quiet_hours"]
    assert "destructive_or_external_send_flagged" in checklist["required_items"]
    assert checklist["approval_rules"]["device_or_desktop_action"] == "explicit_confirmation_required"
    assert "consent_refs" in receipt["required_fields"]
    assert "no_transport_credentials_recorded" in receipt["quality_gates"]
    assert "no_delivery_tokens" in template["template_rules"]


def test_pack_docs_no_placeholders_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join(
        (PACK_DIR / path).read_text(encoding="utf-8")
        for path in ["README.md", "docs/interfaces.md", "docs/operations.md"]
    )
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence", "consent"]:
        assert expected in docs

    all_text = "\n".join(p.read_text(encoding="utf-8") for p in PACK_DIR.rglob("*") if p.is_file())
    for forbidden in ["Example workflow", "sample user request", "reviewer_ready_plan", "placeholder"]:
        assert forbidden not in all_text

    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
