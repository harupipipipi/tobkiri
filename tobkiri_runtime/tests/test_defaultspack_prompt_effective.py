from __future__ import annotations

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.prompt.load_effective import run  # noqa: E402


def _workspace(
    tmp_path: Path,
    *,
    profile_id: str = "p1",
    prompt_id: str = "default_chat",
    base_pack: str = "defaultspack",
) -> dict:
    root = tmp_path / "profiles" / "p1"
    prompts_dir = root / "prompts"
    snapshots_dir = root / "ecosystem" / "snapshots"
    prompts_dir.mkdir(parents=True)
    snapshots_dir.mkdir(parents=True)
    profile_file = root / "profile.yaml"
    profile_file.write_text(
        yaml.safe_dump(
            {
                "profile_id": profile_id,
                "base_pack": base_pack,
                "system_prompt_id": prompt_id,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return {
        "profile_file": str(profile_file),
        "prompts_dir": str(prompts_dir),
        "snapshots_dir": str(snapshots_dir),
    }


def test_effective_prompt_prefers_profile_override_over_snapshot(tmp_path: Path):
    workspace = _workspace(tmp_path)
    Path(workspace["prompts_dir"], "default_chat.system.md").write_text("profile override\n", encoding="utf-8")
    snapshot_prompt = Path(workspace["snapshots_dir"], "defaultspack", "prompts", "default_chat")
    snapshot_prompt.mkdir(parents=True)
    (snapshot_prompt / "prompt.md").write_text("snapshot prompt\n", encoding="utf-8")

    result = run({"profile_id": "p1", "workspace": workspace}, {})

    assert result["data"]["content"] == "profile override\n"
    assert result["data"]["source_type"] == "profile_override"


def test_effective_prompt_uses_snapshot_before_pack_default(tmp_path: Path):
    workspace = _workspace(tmp_path)
    snapshot_prompt = Path(workspace["snapshots_dir"], "defaultspack", "prompts", "default_chat")
    snapshot_prompt.mkdir(parents=True)
    (snapshot_prompt / "prompt.md").write_text("snapshot prompt\n", encoding="utf-8")

    result = run({"profile_id": "p1", "workspace": workspace}, {})

    assert result["data"]["content"] == "snapshot prompt\n"
    assert result["data"]["source_type"] == "profile_snapshot"


def test_effective_prompt_falls_back_to_defaultspack_prompt_component(tmp_path: Path):
    workspace = _workspace(tmp_path)

    result = run({"profile_id": "p1", "workspace": workspace}, {})

    assert "default chat assistant" in result["data"]["content"]
    assert result["data"]["source_type"] == "pack_default"


def test_effective_prompt_can_resolve_sibling_pack_prompt_from_profile_id(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    trusted_packs = {"defaultspack", "rumi_operations_team_pack"}
    monkeypatch.setattr(
        "core_runtime.resolved_profile_scope.persisted_resolved_profile",
        lambda: SimpleNamespace(
            profile_id="defaultspack.mimo_coding_company",
            effective_pack_set=tuple(sorted(trusted_packs)),
        ),
    )
    monkeypatch.setattr(
        "domain.prompt.resolver.prompt_pack_is_trusted",
        lambda pack_id: str(pack_id) in trusted_packs,
    )
    monkeypatch.setattr(
        "domain.prompt.effective.is_trusted_prompt_pack",
        lambda pack_id: (str(pack_id) in trusted_packs, None),
    )
    workspace = _workspace(
        tmp_path,
        profile_id="defaultspack.mimo_coding_company",
        prompt_id="mimo_coding_company",
        base_pack="rumi_operations_team_pack",
    )

    result = run(
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "workspace": workspace,
            "system_prompt_id": "mimo_coding_company",
        },
        {},
    )

    assert "MiMo Coding Company" in result["data"]["content"]
    assert result["data"]["source_type"] == "pack_default"
    assert result["data"]["source"] == "rumi_operations_team_pack.mimo_coding_company"
