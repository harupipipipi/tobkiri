from __future__ import annotations

import json
from pathlib import Path

import yaml

from core_runtime.profile_workspace import ProfileWorkspaceManager
from core_runtime.profile_resource_snapshot import ProfileResourceSnapshotManager


def _write_pack(root: Path) -> None:
    pack = root / "defaultspack"
    (pack / "flows").mkdir(parents=True)
    (pack / "graphs").mkdir()
    (pack / "extensions" / "prompts" / "default_chat").mkdir(parents=True)
    (pack / "flows" / "chat_turn.flow.yaml").write_text("flow_id: defaultspack.chat_turn\nsteps: []\n", encoding="utf-8")
    (pack / "flows" / "custom.flow.yaml").write_text("flow_id: defaultspack.custom_flow\nsteps: []\n", encoding="utf-8")
    (pack / "graphs" / "startup.graph.yaml").write_text(
        yaml.safe_dump({"graph_id": "defaultspack.startup", "nodes": [{"id": "agent", "ref": "defaultspack.agent", "block": "blocks.agent"}]}),
        encoding="utf-8",
    )
    (pack / "graphs" / "defaultspack_alt.graph.yaml").write_text(
        yaml.safe_dump(
            {
                "graph_id": "defaultspack.alt",
                "nodes": [{"id": "prompt", "ref": "defaultspack.prompt"}],
                "blocks": [{"id": "prompt_loader", "ref": "blocks.prompt.load_effective"}],
            }
        ),
        encoding="utf-8",
    )
    (pack / "extensions" / "prompts" / "default_chat" / "manifest.json").write_text(
        json.dumps(
            {
                "id": "default_chat",
                "category": "prompt",
                "config": {"template_file": "prompt.md"},
            }
        ),
        encoding="utf-8",
    )
    (pack / "extensions" / "prompts" / "default_chat" / "prompt.md").write_text(
        "You are Rumi.\n",
        encoding="utf-8",
    )
    (pack / "ecosystem.json").write_text(json.dumps({"pack_id": "defaultspack", "enabled": True}), encoding="utf-8")


def test_defaultspack_snapshot_writes_manifest_lock(tmp_path: Path):
    ecosystem_root = tmp_path / "ecosystem"
    _write_pack(ecosystem_root)
    ProfileWorkspaceManager(tmp_path / "user_data").initialize_profile_workspace({"profile_id": "default-profile"})

    manifest = ProfileResourceSnapshotManager(
        tmp_path / "user_data",
        ecosystem_dir=str(ecosystem_root),
    ).snapshot_default_resources(
        "default-profile",
        base_pack="defaultspack",
        graph_id="defaultspack.startup",
        flow_ids=["chat_turn"],
    )

    manifest_path = tmp_path / "user_data" / "workspaces" / "default-profile" / "snapshots" / "defaultspack" / "manifest.lock.json"
    assert manifest_path.is_file()
    assert manifest["items"][0]["type"] == "flow"


def test_snapshot_records_source_hashes(tmp_path: Path):
    ecosystem_root = tmp_path / "ecosystem"
    _write_pack(ecosystem_root)

    manifest = ProfileResourceSnapshotManager(
        tmp_path / "user_data",
        ecosystem_dir=str(ecosystem_root),
    ).snapshot_default_resources("p1", base_pack="defaultspack", flow_ids=["chat_turn"])

    assert len(manifest["items"][0]["sha256"]) == 64
    assert manifest["items"][0]["source"] == "flows/chat_turn.flow.yaml"


def test_snapshot_includes_default_flow_graph_refs_and_prompt(tmp_path: Path):
    ecosystem_root = tmp_path / "ecosystem"
    _write_pack(ecosystem_root)

    manifest = ProfileResourceSnapshotManager(
        tmp_path / "user_data",
        ecosystem_dir=str(ecosystem_root),
    ).snapshot_default_resources(
        "p1",
        base_pack="defaultspack",
        graph_id="defaultspack.startup",
        graph_ids=["defaultspack.startup", "defaultspack.alt"],
        flow_ids=["chat_turn", "defaultspack.custom_flow"],
        prompt_ids=["defaultspack.default_chat"],
    )

    sources = {item["source"] for item in manifest["items"]}
    assert "flows/chat_turn.flow.yaml" in sources
    assert "flows/custom.flow.yaml" in sources
    assert "extensions/prompts/default_chat/manifest.json" in sources
    assert "extensions/prompts/default_chat/prompt.md" in sources
    assert manifest["requested_flow_ids"] == ["chat_turn", "defaultspack.custom_flow"]
    assert manifest["requested_prompt_ids"] == ["defaultspack.default_chat"]
    assert manifest["graph_ids"] == ["defaultspack.startup", "defaultspack.alt"]
    assert manifest["graph_refs"]["nodes"] == ["defaultspack.agent", "defaultspack.prompt"]
    assert manifest["graph_refs"]["blocks"] == ["blocks.agent", "blocks.prompt.load_effective"]


def test_snapshot_does_not_follow_traversal_or_symlink_sources(tmp_path: Path):
    ecosystem_root = tmp_path / "ecosystem"
    _write_pack(ecosystem_root)
    outside = tmp_path / "outside.yaml"
    outside.write_text("flow_id: outside\nsecret: true\n", encoding="utf-8")

    manager = ProfileResourceSnapshotManager(
        tmp_path / "user_data",
        ecosystem_dir=str(ecosystem_root),
    )
    manifest = manager.snapshot_default_resources(
        "p1",
        base_pack="defaultspack",
        flow_ids=["../../outside.yaml"],
    )

    assert manifest["items"] == []
    snapshot_root = (
        tmp_path
        / "user_data"
        / "workspaces"
        / "p1"
        / "snapshots"
        / "defaultspack"
    )
    assert not any(
        path.is_file() and path.read_bytes() == outside.read_bytes()
        for path in snapshot_root.rglob("*")
    )
