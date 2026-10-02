from __future__ import annotations



import json
from pathlib import Path

from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract


PACK_ID = "rumi_workspace_pack"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_rumi_workspace_pack_required_docs_and_json_are_valid() -> None:
    repo_root = _repo_root()
    pack_root = repo_root / "ecosystem" / PACK_ID

    required_files = [
        pack_root / "README.md",
        pack_root / "docs" / "README.md",
        pack_root / "docs" / "architecture.md",
        pack_root / "docs" / "interfaces.md",
        pack_root / "docs" / "operations.md",
    ]
    for path in required_files:
        assert path.is_file(), f"missing required pack doc: {path}"
        assert path.read_text(encoding="utf-8").strip(), f"empty required pack doc: {path}"

    json_paths = [
        pack_root / "ecosystem.json",
        pack_root / "catalog" / "capabilities.workspace.json",
        pack_root / "catalog" / "tools.workspace.json",
        repo_root / "ecosystem" / "setup_pack" / PACK_ID / "pack.json",
    ]
    for path in json_paths:
        json.loads(path.read_text(encoding="utf-8"))


def test_rumi_workspace_pack_is_discoverable_by_setup_selector() -> None:
    repo_root = _repo_root()
    selector = PackSelector(repo_root / "ecosystem")
    candidates = {candidate.pack_id: candidate for candidate in selector.scan_candidates()}

    assert PACK_ID in candidates
    candidate = candidates[PACK_ID]
    assert candidate.display_name == "Rumi Workspace Pack"
    assert candidate.pack_identity == f"rumi:ecosystem/{PACK_ID}"
    assert candidate.risk_level == "low"
    assert candidate.all_ok_eligible is True
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.conflicts_with == []
    assert candidate.overlap_policy["tool_aliases"] == "prefer_explicit_pack_namespace"
    assert candidate.base_pack_promotion["eligible"] is False
    assert candidate.marketplace["id"] == "rumi.workspace_pack"
    assert candidate.signing["mode"] == "repository_reviewed"

    issues = selector.validate_candidates(
        installed_packs={"defaultspack": {"version": "2.0.0"}},
        platform_name="linux",
        python_version="3.11.0",
    )
    assert [issue for issue in issues if issue["pack_id"] == PACK_ID] == []


def test_rumi_workspace_pack_has_no_secret_like_payloads() -> None:
    repo_root = _repo_root()
    pack_root = repo_root / "ecosystem" / PACK_ID
    setup_pack_root = repo_root / "ecosystem" / "setup_pack" / PACK_ID
    suspicious_values = ("sk-", "xoxb-", "ghp_", "BEGIN PRIVATE KEY")

    for root in (pack_root, setup_pack_root):
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            assert not any(value in text for value in suspicious_values), path

    ecosystem = json.loads((pack_root / "ecosystem.json").read_text(encoding="utf-8"))
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
