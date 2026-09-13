from __future__ import annotations



import json
from pathlib import Path

import yaml

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


PACK_ID = "rumi_memory_knowledge_pack"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_rumi_memory_knowledge_pack_required_docs_assets_and_json_are_valid() -> None:
    repo_root = _repo_root()
    pack_root = repo_root / "ecosystem" / PACK_ID
    ecosystem_json = pack_root / "ecosystem.json"

    required_files = [
        pack_root / "README.md",
        pack_root / "ecosystem.json",
        pack_root / "docs" / "README.md",
        pack_root / "docs" / "architecture.md",
        pack_root / "docs" / "interfaces.md",
        pack_root / "docs" / "operations.md",
        pack_root / "catalog" / "capabilities.memory.json",
        pack_root / "catalog" / "recall_workflows.memory.yaml",
        pack_root / "catalog" / "surfaces.memory.json",
        pack_root / "specs" / "memory_objects.schema.yaml",
        pack_root / "specs" / "skill_learning_proposal.schema.yaml",
        pack_root / "policies" / "memory_hygiene.policy.yaml",
        pack_root / "policies" / "recall_evidence.policy.yaml",
        pack_root / "policies" / "skill_learning.policy.yaml",
        pack_root / "profiles" / "memory_curator.profile.yaml",
        pack_root / "profiles" / "evidence_recall_reviewer.profile.yaml",
        pack_root / "profiles" / "project_knowledge_steward.profile.yaml",
        pack_root / "presets" / "evidence_backed_recall.preset.yaml",
        pack_root / "presets" / "memory_hygiene_review.preset.yaml",
        pack_root / "presets" / "project_knowledge_refresh.preset.yaml",
        pack_root / "presets" / "skill_learning_proposals.preset.yaml",
        pack_root / "prompts" / "memory_curator.system.md",
        pack_root / "prompts" / "evidence_backed_recall.system.md",
        pack_root / "prompts" / "project_knowledge.system.md",
        pack_root / "examples" / "session_recall.example.yaml",
        pack_root / "examples" / "project_knowledge_refresh.example.yaml",
        pack_root / "examples" / "skill_learning_proposal.example.yaml",
        repo_root / "ecosystem" / "setup_pack" / PACK_ID / "pack.json",
    ]
    for path in required_files:
        assert path.is_file(), f"missing required memory pack asset: {path}"
        assert path.read_text(encoding="utf-8").strip(), f"empty memory pack asset: {path}"

    for path in sorted(pack_root.rglob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
    ecosystem = json.loads(ecosystem_json.read_text(encoding="utf-8"))
    json.loads(
        (repo_root / "ecosystem" / "setup_pack" / PACK_ID / "pack.json").read_text(
            encoding="utf-8"
        )
    )

    for path in sorted(pack_root.rglob("*.yaml")):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None

    provides = ecosystem["components"]["knowledge_specs"]["connectivity"]["provides"]
    assert "rumi.memory.skill_learning_proposal_schema" in provides


def test_rumi_memory_knowledge_pack_is_discoverable_by_setup_selector() -> None:
    repo_root = _repo_root()
    selector = PackSelector(repo_root / "ecosystem")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}

    assert PACK_ID in candidates
    candidate = candidates[PACK_ID]
    assert candidate.display_name == "Rumi Memory Knowledge Pack"
    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.risk_level == "low"
    assert candidate.all_ok_eligible is True
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.conflicts_with == []
    assert candidate.marketplace["id"] == "rumi.memory_knowledge_pack"
    assert candidate.signing["mode"] == "repository_reviewed"

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_rumi_memory_knowledge_pack_overlap_and_base_pack_promotion_metadata() -> None:
    repo_root = _repo_root()
    selector = PackSelector(repo_root / "ecosystem")
    candidate = {
        candidate.pack_id: candidate for candidate in selector.scan_candidates()
    }[PACK_ID]

    assert candidate.base_pack_promotion["eligible"] is False
    assert "must not become the default runtime pack" in candidate.base_pack_promotion["reason"]
    assert candidate.overlap_policy["runtime_memory_writes"] == "forbidden_by_this_pack"

    pack_notes = candidate.overlap_policy["pack_notes"]
    assert pack_notes["defaultspack"]["relationship"] == "depends_on"
    assert "defaultspack remains the runtime owner" in pack_notes["defaultspack"]["conflict"]
    assert pack_notes["rumi_agent_services_pack"]["relationship"] == "complementary"
    assert "does not execute jobs" in pack_notes["rumi_agent_services_pack"]["overlap"]
    assert pack_notes["rumi_research_pack"]["relationship"] == "complementary"
    assert "research evidence cards" in pack_notes["rumi_research_pack"]["conflict"]
    assert pack_notes["rumi_local_agent_pack"]["relationship"] == "complementary"
    assert "existing runtime memory stores" in pack_notes["existing_memory_surfaces"]["overlap"]
    assert candidate.overlap_policy["tool_aliases"] == "prefer_explicit_pack_namespace"


def test_rumi_memory_knowledge_pack_has_no_secret_like_payloads() -> None:
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
    assert ecosystem["metadata"]["runtime_memory_write_policy"] == "defines_contracts_only"
