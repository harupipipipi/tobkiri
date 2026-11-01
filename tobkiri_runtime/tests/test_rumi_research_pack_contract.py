from __future__ import annotations



import json
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


PACK_ID = "rumi_research_pack"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_rumi_research_pack_required_docs_assets_and_json_are_valid() -> None:
    repo_root = _repo_root()
    pack_root = repo_root / "ecosystem" / PACK_ID

    required_files = [
        pack_root / "README.md",
        pack_root / "ecosystem.json",
        pack_root / "docs" / "README.md",
        pack_root / "docs" / "architecture.md",
        pack_root / "docs" / "interfaces.md",
        pack_root / "docs" / "operations.md",
        pack_root / "catalog" / "capabilities.research.json",
        pack_root / "catalog" / "evidence_schema.research.yaml",
        pack_root / "catalog" / "source_quality.research.yaml",
        pack_root / "catalog" / "workflows.research.yaml",
        pack_root / "catalog" / "citation_styles.research.yaml",
        pack_root / "profiles" / "deep_researcher.profile.yaml",
        pack_root / "profiles" / "evidence_reviewer.profile.yaml",
        pack_root / "profiles" / "local_research_synthesizer.profile.yaml",
        pack_root / "presets" / "deep_research_report.preset.yaml",
        pack_root / "presets" / "genspark_research_brief.preset.yaml",
        pack_root / "presets" / "citation_audit.preset.yaml",
        pack_root / "presets" / "local_only_research.preset.yaml",
        pack_root / "prompts" / "deep_research.system.md",
        pack_root / "prompts" / "evidence_review.system.md",
        pack_root / "prompts" / "local_synthesis.system.md",
        pack_root / "examples" / "local_market_scan.example.yaml",
        pack_root / "examples" / "citation_audit.example.yaml",
        pack_root / "examples" / "decision_brief.example.yaml",
        repo_root / "ecosystem" / "setup_pack" / PACK_ID / "pack.json",
    ]
    for path in required_files:
        assert path.is_file(), f"missing required research pack asset: {path}"
        assert path.read_text(encoding="utf-8").strip(), f"empty research pack asset: {path}"

    for path in sorted(pack_root.rglob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
    json.loads(
        (repo_root / "ecosystem" / "setup_pack" / PACK_ID / "pack.json").read_text(
            encoding="utf-8"
        )
    )

    for path in sorted(pack_root.rglob("*.yaml")):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None


def test_rumi_research_pack_is_discoverable_by_setup_selector() -> None:
    repo_root = _repo_root()
    selector = PackSelector(repo_root / "ecosystem")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}

    assert PACK_ID in candidates
    candidate = candidates[PACK_ID]
    assert candidate.display_name == "Rumi Research Pack"
    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.risk_level == "low"
    assert candidate.all_ok_eligible is True
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.conflicts_with == []
    assert candidate.marketplace["id"] == "rumi.research_pack"
    assert candidate.signing["mode"] == "repository_reviewed"

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_rumi_research_pack_overlap_and_base_pack_promotion_metadata() -> None:
    repo_root = _repo_root()
    selector = PackSelector(repo_root / "ecosystem")
    candidate = {
        candidate.pack_id: candidate for candidate in selector.scan_candidates()
    }[PACK_ID]

    assert candidate.base_pack_promotion["eligible"] is False
    assert "must not become the default runtime pack" in candidate.base_pack_promotion["reason"]

    pack_notes = candidate.overlap_policy["pack_notes"]
    assert pack_notes["defaultspack"]["relationship"] == "depends_on"
    assert pack_notes["defaultspack"]["conflict"] == "none"
    assert pack_notes["rumi_workspace_pack"]["relationship"] == "complementary"
    assert "workspace owns editable artifact/export contracts" in pack_notes["rumi_workspace_pack"]["overlap"]
    assert pack_notes["rumi_agent_services_pack"]["relationship"] == "complementary"
    assert "agent services owns execution orchestration" in pack_notes["rumi_agent_services_pack"]["overlap"]
    assert candidate.overlap_policy["tool_aliases"] == "prefer_explicit_pack_namespace"


def test_rumi_research_pack_has_no_secret_like_payloads() -> None:
    repo_root = _repo_root()
    pack_root = repo_root / "ecosystem" / PACK_ID
    setup_pack_root = repo_root / "ecosystem" / "setup_pack" / PACK_ID
    suspicious_values = (
        "sk-",
        "xoxb-",
        "ghp_",
        "BEGIN PRIVATE KEY",
        "aws_secret_access_key",
        "private_token:",
    )

    for root in (pack_root, setup_pack_root):
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            assert not any(value in text for value in suspicious_values), path

    ecosystem = json.loads((pack_root / "ecosystem.json").read_text(encoding="utf-8"))
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
