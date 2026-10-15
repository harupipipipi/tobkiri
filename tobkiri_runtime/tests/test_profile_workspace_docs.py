from __future__ import annotations

from pathlib import Path


DOCS = [
    "profile_workspace.md",
    "flow_spec.md",
    "defaultspack_authoring.md",
    "provider_authoring.md",
    "prompt_authoring.md",
    "tool_authoring.md",
    "permissions_policy.md",
]


def test_profile_workspace_docs_exist_and_cover_required_topics():
    docs_dir = Path(__file__).resolve().parents[1] / "docs"
    for name in DOCS:
        path = docs_dir / name
        assert path.is_file(), name
        content = path.read_text(encoding="utf-8")
        assert "profile" in content.lower() or name != "profile_workspace.md"

    profile_doc = (docs_dir / "profile_workspace.md").read_text(encoding="utf-8")
    for phrase in ["workspaces/<profile_id>", "state/", "activation/", "artifacts/", "snapshots/", "audit/events.jsonl", "not Profile authorities"]:
        assert phrase in profile_doc
    for phrase in [
        "resolve_runtime_database_path",
        "resolve_runtime_user_data_dir",
        "ChatStore",
        "MemoryStore",
        "Attachments",
        "must never fall back",
    ]:
        assert phrase in profile_doc

    permissions_doc = (docs_dir / "permissions_policy.md").read_text(encoding="utf-8")
    assert "defaults only" in permissions_doc
    assert "approval" in permissions_doc
