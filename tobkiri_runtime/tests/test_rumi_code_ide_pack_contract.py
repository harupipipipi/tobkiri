from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract


REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = REPO_ROOT / "tobkiri_runtime" / "ecosystem" / "rumi_code_ide_pack"
SETUP_PACK = (
    REPO_ROOT
    / "tobkiri_runtime"
    / "ecosystem"
    / "setup_pack"
    / "rumi_code_ide_pack"
    / "pack.json"
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_rumi_code_ide_pack_has_required_docs_and_declarative_assets():
    required_paths = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "profiles/code_cli_ide.profile.yaml",
        "profiles/code_review_terminal.profile.yaml",
        "profiles/local_first_pair_programmer.profile.yaml",
        "prompts/code_ide_agent.system.md",
        "prompts/code_review_terminal.system.md",
        "prompts/command_recipe_runner.system.md",
        "presets/claude_code_style_workspace.preset.yaml",
        "presets/cline_style_ide_pairing.preset.yaml",
        "presets/codex_patch_loop.preset.yaml",
        "presets/gemini_cli_style_discovery.preset.yaml",
        "presets/local_first_strict.preset.yaml",
        "examples/bugfix_session.example.yaml",
        "examples/refactor_session.example.yaml",
        "examples/review_session.example.yaml",
        "command_recipes/code_cli_recipes.yaml",
        "tool_scopes/code_ide_tool_scope.yaml",
        "metadata/overlap_conflicts.yaml",
    ]

    missing = [path for path in required_paths if not (PACK_ROOT / path).is_file()]
    assert not missing

    ecosystem = _read_json(PACK_ROOT / "ecosystem.json")
    assert ecosystem["pack_id"] == "rumi_code_ide_pack"
    assert ecosystem["pack_identity"] == "rumi:ecosystem/rumi_code_ide_pack"
    assert ecosystem["components"] == {}
    assert ecosystem["load_order"] == []


def test_rumi_code_ide_pack_setup_metadata_is_optional_and_dependency_scoped():
    ecosystem = _read_json(PACK_ROOT / "ecosystem.json")
    setup = _read_json(SETUP_PACK)
    manifest = _read_json(PACK_ROOT / "pack.v4.json")

    assert setup["pack_id"] == "rumi_code_ide_pack"
    assert setup["target_pack_id"] == ecosystem["pack_id"]
    assert setup["version"] == ecosystem["version"]
    assert setup["recommended"] is False
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"

    setup_deps = {item["pack_id"]: item["version"] for item in setup["depends_on"]}
    v4_deps = manifest["requirements"]["pack_dependencies"]

    assert setup_deps == {
        "defaultspack": ">=2.0.0",
        "rumi_default_tools_pack": ">=1.0.0",
    }
    assert v4_deps == {"rumi_default_tools_pack": ">=1.0.0"}
    assert "rumi_local_agent_pack" not in v4_deps
    assert ecosystem["metadata"]["legacy_annotations"]["optional_integrations"] == [
        {
            "pack_id": "rumi_local_agent_pack",
            "reason": "Can coexist with starter local-agent presets; this pack is narrower and IDE/CLI specific.",
            "version": ">=1.0.0",
        }
    ]
    assert manifest["requirements"]["network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert manifest["requirements"]["secrets"] == []


def test_rumi_code_ide_pack_documents_no_secrets_and_overlap_notes():
    docs = "\n".join(
        _read(PACK_ROOT / path)
        for path in [
            "README.md",
            "docs/interfaces.md",
            "docs/operations.md",
            "tool_scopes/code_ide_tool_scope.yaml",
            "metadata/overlap_conflicts.yaml",
        ]
    )

    for expected in [
        "Required Secrets",
        "None",
        "defaultspack",
        "rumi_default_tools_pack",
        "rumi_local_agent_pack",
        "overlap",
        "conflict",
    ]:
        assert expected in docs

    secret_assignment = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    )
    pack_text = "\n".join(path.read_text(encoding="utf-8") for path in PACK_ROOT.rglob("*") if path.is_file())
    assert not secret_assignment.search(pack_text)
